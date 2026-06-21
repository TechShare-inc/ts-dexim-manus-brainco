"""Status monitoring via ZMQ PULL socket.

Runs a background thread that continuously polls the status plane and
updates ``NodeRuntime`` instances.  Started BEFORE any nodes are
launched so that initial status messages are captured immediately.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import zmq
from dexim.core.messages import (
    STATUS_HEALTHY,
    STATUS_PULL_ENDPOINT,
    StatusInfo,
)

from .runtime import NodeRuntime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_INTERVAL_MS: int = 100
_PROCESS_STATS_INTERVAL_S: float = 2.0
_HEARTBEAT_PERIODIC_LOG_INTERVAL: int = 30


# ---------------------------------------------------------------------------
# StatusMonitor
# ---------------------------------------------------------------------------


class StatusMonitor:
    """Polls ZMQ PULL for node status messages in a background thread.

    The monitor thread must be started BEFORE nodes are launched so
    that their initial ``STATUS_INITIALIZED`` and ``STATUS_STANDBY``
    messages are captured immediately.

    Args:
        endpoint: ZMQ endpoint to bind (default: ``tcp://0.0.0.0:5551``).
        poll_interval_ms: Milliseconds between poll cycles.
        process_stats_interval_s: Seconds between OS process stat refreshes.
    """

    def __init__(
        self,
        endpoint: str = STATUS_PULL_ENDPOINT,
        poll_interval_ms: int = _POLL_INTERVAL_MS,
        process_stats_interval_s: float = _PROCESS_STATS_INTERVAL_S,
    ) -> None:
        self._endpoint = endpoint
        self._poll_interval_ms = poll_interval_ms
        self._process_stats_interval_s = process_stats_interval_s

        # ZMQ
        self._ctx: zmq.Context | None = None
        self._pull: zmq.Socket | None = None
        self._poller: zmq.Poller | None = None

        # Thread
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()

        # Node registry (set by orchestrator before start)
        self._node_map: dict[str, NodeRuntime] = {}
        self._nodes: list[NodeRuntime] = []

        # Process stats throttle
        self._last_process_stats_ts: float = 0.0

        # Callbacks
        self._on_state_change: Any = None
        self._on_event: Any = None

        self._init_zmq()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_nodes(
        self,
        node_map: dict[str, NodeRuntime],
        nodes: list[NodeRuntime],
    ) -> None:
        """Register the node tracking structures.

        Must be called before ``start()``.

        Args:
            node_map: Mapping of ``node_id`` to ``NodeRuntime``.
            nodes: Ordered list of all ``NodeRuntime`` instances.
        """
        self._node_map = node_map
        self._nodes = nodes

    def set_callbacks(
        self,
        on_state_change: Any = None,
        on_event: Any = None,
    ) -> None:
        """Set optional callbacks.

        Args:
            on_state_change: ``fn(node: NodeRuntime, old_status: str, new_status: str)``
            on_event: ``fn(level: str, msg: str, node: str = "")``
        """
        self._on_state_change = on_state_change
        self._on_event = on_event

    def start(self) -> None:
        """Start the background status polling thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="status-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown and join the polling thread."""
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._cleanup_zmq()

    # ------------------------------------------------------------------
    # Internal: ZMQ
    # ------------------------------------------------------------------

    def _init_zmq(self) -> None:
        """Create ZMQ context and bind PULL socket."""
        self._ctx = zmq.Context.instance()

        self._pull = self._ctx.socket(zmq.PULL)
        bind_addr = self._endpoint.replace("localhost", "0.0.0.0")
        if bind_addr.startswith("tcp://*"):
            bind_addr = bind_addr.replace("*", "0.0.0.0")
        self._pull.bind(bind_addr)

        self._poller = zmq.Poller()
        self._poller.register(self._pull, zmq.POLLIN)

    def _cleanup_zmq(self) -> None:
        """Close ZMQ sockets."""
        if self._pull is not None:
            try:
                self._pull.close(linger=0)
            except Exception:
                pass
        self._pull = None
        self._poller = None
        self._ctx = None

    # ------------------------------------------------------------------
    # Internal: polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Main loop: poll PULL, update runtimes, refresh OS stats."""
        while not self._shutdown.is_set():
            try:
                self._poll_once()
            except Exception:
                pass

            now = time.monotonic()
            if now - self._last_process_stats_ts >= self._process_stats_interval_s:
                self._refresh_process_stats()
                self._check_process_liveness()
                self._last_process_stats_ts = now

            time.sleep(self._poll_interval_ms / 1000.0)

    def _poll_once(self) -> None:
        """Receive and process one batch of status messages."""
        if self._poller is None or self._pull is None:
            return

        events = dict(self._poller.poll(timeout=self._poll_interval_ms))
        if self._pull not in events:
            return

        while True:
            try:
                payload = self._pull.recv(flags=zmq.NOBLOCK)
            except zmq.ZMQError:
                break

            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            node_id = msg.get("node_id", "")
            status = msg.get("status", "UNKNOWN")
            is_recording = msg.get("is_recording", False)
            info_dict = msg.get("info", {})

            runtime = self._node_map.get(node_id)
            if runtime is None:
                continue

            old_status = runtime.status
            runtime.is_recording = is_recording
            runtime.last_heartbeat = time.monotonic()

            # Deserialize and store StatusInfo
            if isinstance(info_dict, dict):
                runtime.info = StatusInfo.from_flat_dict(info_dict)
                runtime.info.pid = runtime.pid

            # HEALTHY is a heartbeat, not a state change
            is_state_change = False
            if status != STATUS_HEALTHY:
                runtime.status = status
                is_state_change = status != old_status

            # Notify state change callback
            if is_state_change and self._on_state_change is not None:
                try:
                    self._on_state_change(runtime, old_status, status)
                except Exception:
                    pass

            # Log event on state change or periodically for HEALTHY
            if is_state_change or (
                status == STATUS_HEALTHY
                and int(runtime.last_heartbeat) % _HEARTBEAT_PERIODIC_LOG_INTERVAL == 0
            ):
                if self._on_event is not None:
                    extra = ""
                    if info_dict:
                        key_fields = []
                        for k in (
                            "interface_mode",
                            "data_rate_hz",
                            "hardware_connected",
                        ):
                            v = info_dict.get(k)
                            if v is not None and v != "":
                                key_fields.append(f"{k}={v}")
                        if key_fields:
                            extra = " | " + " ".join(key_fields)
                    try:
                        self._on_event(
                            "info",
                            msg=f"{status}{extra}",
                            node=runtime.spec.device_name,
                        )
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Internal: OS process stats
    # ------------------------------------------------------------------

    def _refresh_process_stats(self) -> None:
        """Update PID-based memory, CPU, and health stats via ``psutil``."""
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            return

        for runtime in self._nodes:
            if runtime.pid <= 0:
                continue
            was_healthy = runtime.ps_healthy
            try:
                proc = psutil.Process(runtime.pid)
                proc_status = proc.status()
                runtime.ps_healthy = (
                    proc.is_running() and proc_status != psutil.STATUS_ZOMBIE
                )
                runtime.info.memory_mb = proc.memory_info().rss / (1024.0 * 1024.0)
                runtime.info.cpu_percent = proc.cpu_percent(interval=0.0)
            except psutil.NoSuchProcess:
                runtime.ps_healthy = False
            except psutil.AccessDenied:
                pass
            except Exception:
                pass

            if was_healthy and not runtime.ps_healthy:
                # Fire state-change to trigger orchestrator restart logic
                if self._on_state_change is not None:
                    try:
                        self._on_state_change(runtime, runtime.status, "DEAD")
                    except Exception:
                        pass
                if self._on_event is not None:
                    try:
                        rc = runtime.process.poll() if runtime.process else None
                        msg = f"OS process died (exit code: {rc})"
                        self._on_event("error", msg=msg, node=runtime.spec.device_name)
                    except Exception:
                        pass

    def _check_process_liveness(self) -> None:
        """Detect silently-died processes and mark them unhealthy."""
        for runtime in self._nodes:
            if runtime.process is not None:
                rc = runtime.process.poll()
                if rc is not None:
                    runtime.ps_healthy = False
