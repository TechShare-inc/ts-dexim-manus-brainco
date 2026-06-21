"""Node subprocess launcher.

Spawns subprocesses for each node specification, captures stdout,
and tracks process lifecycle.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command_builder import CommandBuilder
from .runtime import NodeRuntime
from ..session_loader import NodeSpec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_LOG_BYTES: int = 5 * 1024 * 1024  # 5 MB per file
_MAX_LOG_BACKUPS: int = 3
_EARLY_EXIT_CHECK_DELAY_S: float = 2.0


# ---------------------------------------------------------------------------
# NodeLauncher
# ---------------------------------------------------------------------------


class NodeLauncher:
    """Spawns and tracks node subprocesses.

    Args:
        log_dir: Directory for rotating debug log files.
        command_builder: Command-line builder for node subprocesses.
    """

    def __init__(
        self,
        log_dir: str = "./logs",
        command_builder: CommandBuilder | None = None,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._command_builder = command_builder or CommandBuilder()

        # Subprocess logger
        self._logger = logging.getLogger("dexim.launch.subprocess")
        self._logger.propagate = False

        # Tracked state
        self._nodes: list[NodeRuntime] = []
        self._node_map: dict[str, NodeRuntime] = {}
        self._reader_threads: list[threading.Thread] = []
        self._shutdown = threading.Event()

        # Callbacks
        self._on_event: Any = None
        self._on_launched: Any = None

        self._setup_logging()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def nodes(self) -> list[NodeRuntime]:
        """Ordered list of launched node runtimes."""
        return self._nodes

    @property
    def node_map(self) -> dict[str, NodeRuntime]:
        """Mapping of ``node_id`` to ``NodeRuntime``."""
        return self._node_map

    def set_callbacks(
        self,
        on_event: Any = None,
        on_launched: Any = None,
    ) -> None:
        """Set optional callbacks.

        Args:
            on_event: ``fn(level: str, msg: str, node: str = "")``
            on_launched: ``fn(runtime: NodeRuntime)`` called after each launch.
        """
        self._on_event = on_event
        self._on_launched = on_launched

    def launch(self, spec: NodeSpec) -> NodeRuntime | None:
        """Launch a single node subprocess.

        Args:
            spec: The resolved node specification.

        Returns:
            ``NodeRuntime`` on success, ``None`` if the CLI is not found.
        """
        cmd = self._command_builder.build(spec)
        self._logger.info("[%s] Launching: %s", spec.device_name, " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            )
        except FileNotFoundError:
            if self._on_event:
                self._on_event(
                    "error",
                    msg=(
                        f"Could not launch '{spec.device_name}': "
                        "dexim CLI not found. Is the package installed?"
                    ),
                    node=spec.device_name,
                )
            return None

        runtime = NodeRuntime(
            spec=spec,
            process=proc,
            pid=proc.pid,
            launched_at=datetime.now(timezone.utc),
        )
        self._nodes.append(runtime)
        self._node_map[spec.expected_node_id] = runtime

        if self._on_event:
            self._on_event(
                "info",
                msg=f"Launched (PID {proc.pid})",
                node=spec.device_name,
            )

        # Start stdout reader thread
        t = threading.Thread(
            target=self._read_stdout,
            args=(runtime,),
            daemon=True,
        )
        t.start()
        self._reader_threads.append(t)

        # Check for immediate exit
        self._check_early_exit(runtime)

        if self._on_launched:
            self._on_launched(runtime)

        return runtime

    def shutdown(self) -> None:
        """Signal shutdown and join reader threads."""
        self._shutdown.set()
        for t in self._reader_threads:
            t.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Internal: logging
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure rotating file handler for subprocess debug logs."""
        from logging.handlers import RotatingFileHandler

        self._log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = self._log_dir / f"dexim-launch-{ts}.log"

        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_MAX_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.DEBUG)

    # ------------------------------------------------------------------
    # Internal: stdout reader
    # ------------------------------------------------------------------

    def _read_stdout(self, runtime: NodeRuntime) -> None:
        """Read stdout lines from the subprocess into the ring buffer."""
        if runtime.process is None or runtime.process.stdout is None:
            return

        for line in runtime.process.stdout:
            line = line.rstrip("\n\r")
            runtime.stdout_tail.append(line)
            self._logger.debug("[%s] %s", runtime.spec.device_name, line)

    # ------------------------------------------------------------------
    # Internal: early exit check
    # ------------------------------------------------------------------

    def _check_early_exit(self, runtime: NodeRuntime) -> None:
        """Check if the subprocess exited almost immediately."""
        time.sleep(_EARLY_EXIT_CHECK_DELAY_S)
        if runtime.process is not None:
            rc = runtime.process.poll()
            if rc is not None:
                runtime.ps_healthy = False
                if self._on_event:
                    tail = "\n".join(list(runtime.stdout_tail))
                    self._on_event(
                        "error",
                        msg=(
                            f"Exited immediately (code {rc}). " f"Last stdout:\n{tail}"
                        ),
                        node=runtime.spec.device_name,
                    )
