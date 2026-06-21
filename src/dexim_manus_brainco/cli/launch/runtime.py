"""Node runtime state tracking.

Defines ``NodeRuntime`` -- the per-node runtime state used by
``NodeLauncher``, ``StatusMonitor``, and the TUI.
"""

from __future__ import annotations

import collections
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dexim.core.messages import StatusInfo

from ..session_loader import NodeSpec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STDOUT_TAIL_LINES: int = 30
_HEARTBEAT_TIMEOUT_S: float = 5.0


# ---------------------------------------------------------------------------
# NodeRuntime
# ---------------------------------------------------------------------------


@dataclass
class NodeRuntime:
    """Runtime state for a single launched node.

    Attributes:
        spec: The resolved node specification.
        process: The subprocess handle (``None`` before launch).
        status: Last known status string (``"UNKNOWN"`` initially).
        is_recording: Whether the node reports it is recording.
        last_heartbeat: Monotonic timestamp of the last status message.
        pid: OS process ID (populated after launch).
        launched_at: Wall-clock time when the node was launched.
        info: Latest status info reported by the node.
        ps_healthy: Whether the OS process is alive per ``psutil``.
        stdout_tail: Ring buffer of recent stdout lines.
    """

    spec: NodeSpec
    process: subprocess.Popen[str] | None = None
    status: str = "UNKNOWN"
    is_recording: bool = False
    last_heartbeat: float = 0.0
    pid: int = 0
    launched_at: datetime | None = None
    info: StatusInfo = field(default_factory=StatusInfo)
    ps_healthy: bool = True
    stdout_tail: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=_STDOUT_TAIL_LINES)
    )

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """``True`` if the node has reported recently AND the OS process
        is alive per ``psutil``."""
        if self.status == "UNKNOWN":
            return True  # Not yet heard from -- not unhealthy
        if not self.ps_healthy:
            return False
        return (time.monotonic() - self.last_heartbeat) < _HEARTBEAT_TIMEOUT_S

    @property
    def uptime_sec(self) -> float:
        """Seconds since the node was launched (0 if not launched)."""
        if self.launched_at is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.launched_at).total_seconds()

    @property
    def is_running(self) -> bool:
        """``True`` if the subprocess is still alive."""
        if self.process is None:
            return False
        return self.process.poll() is None
