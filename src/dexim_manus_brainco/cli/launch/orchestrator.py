"""Launch orchestrator -- coordinates node lifecycle for a session.

Replaces the monolithic ``LaunchManager`` with a clean composition of
focused components: ``StatusMonitor``, ``NodeLauncher``, ``ControlBus``.

Critical ordering: the status monitor is started **before** any nodes
are launched so that ``STATUS_INITIALIZED`` and ``STATUS_STANDBY``
messages are captured immediately.  This fixes the root cause of the
"always UNKNOWN" status bug.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from dexim.core.messages import (
    CTRL_SHUTDOWN,
    STATUS_ERROR,
    STATUS_STARTED,
)

from .command_builder import CommandBuilder
from .control_bus import ControlBus
from .node_launcher import NodeLauncher
from .runtime import NodeRuntime
from .status_monitor import StatusMonitor
from ..session_loader import NodeSpec, SessionPlan

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Input node (manus) health gate
_INPUT_READY_TIMEOUT_S: float = 120.0
_INPUT_HEALTH_POLL_INTERVAL_S: float = 0.25

# Node auto-restart
_MAX_NODE_RESTARTS: int = 3
_NODE_RESTART_BACKOFF_BASE: float = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_diagnostics(runtime: NodeRuntime) -> str:
    """Build a human-readable diagnostics string for a stalled node.

    Args:
        runtime: The node runtime to diagnose.

    Returns:
        A multi-line diagnostics string.
    """
    lines = [
        f"  Status: {runtime.status}",
        f"  Last heartbeat: {runtime.last_heartbeat:.1f}s ago",
        f"  OS process healthy: {runtime.ps_healthy}",
        f"  Process exit code: {runtime.process.poll() if runtime.process else 'N/A'}",
    ]
    if runtime.stdout_tail:
        lines.append("  Recent stdout:")
        for line in list(runtime.stdout_tail)[-5:]:
            lines.append(f"    {line}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LaunchOrchestrator
# ---------------------------------------------------------------------------


class LaunchOrchestrator:
    """Coordinates node lifecycle for a multi-node session.

    Orchestrates the three concerns:

    1. **Status monitoring** (started first -- captures all messages)
    2. **Node launching** (priority-ordered subprocess spawning)
    3. **Control broadcasting** (lifecycle commands via ZMQ PUB)

    Args:
        plan: The resolved session plan.
        log_dir: Directory for rotating debug log files.
    """

    def __init__(
        self,
        plan: SessionPlan,
        log_dir: str = "./logs",
    ) -> None:
        self._plan = plan

        # Components
        self._command_builder = CommandBuilder()
        self._control_bus = ControlBus()
        self._launcher = NodeLauncher(
            log_dir=log_dir,
            command_builder=self._command_builder,
        )
        self._status_monitor = StatusMonitor()

        # Wire callbacks
        self._launcher.set_callbacks(on_event=self._emit_event)
        self._control_bus.on_event = self._emit_event

        # Shutdown coordination
        self._shutdown_event = threading.Event()

        # Launch result
        self._launch_error: Exception | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def nodes(self) -> list[NodeRuntime]:
        """Ordered list of launched node runtimes."""
        return self._launcher.nodes

    @property
    def plan(self) -> SessionPlan:
        """The session plan."""
        return self._plan

    @property
    def is_complete(self) -> bool:
        """``True`` if all planned nodes are running and healthy."""
        if self._launch_error is not None:
            return False
        expected = len(self._plan.nodes)
        if expected == 0:
            return True
        alive_healthy = sum(1 for n in self.nodes if n.is_running and n.is_healthy)
        return alive_healthy == expected

    @property
    def launch_error(self) -> Exception | None:
        """Non-None if launch failed (e.g. input node not ready)."""
        return self._launch_error

    def start(self) -> None:
        """Initialise and launch all nodes.

        The status monitor is started FIRST so that initial status
        messages from newly spawned nodes are captured immediately.
        Input nodes (manus, priority 0) are launched and waited on
        before lower-priority nodes.
        """
        # 1. Start status monitor FIRST -- before any subprocess exists
        self._status_monitor.set_callbacks(
            on_state_change=self._on_node_state_change,
            on_event=self._emit_event,
        )
        self._status_monitor.set_nodes(self._launcher.node_map, self._launcher.nodes)
        self._status_monitor.start()

        self._emit_event(
            "info",
            msg=f"Session '{self._plan.name}' "
            f"starting with {len(self._plan.nodes)} node(s)",
        )

        # 2. Launch nodes in priority order
        self._launch_all_nodes()

    def stop(self) -> None:
        """Graceful shutdown: broadcast SHUTDOWN, kill subprocesses, close ZMQ."""
        self._emit_event("info", msg="Shutting down session...")
        self._shutdown_event.set()

        # Broadcast SHUTDOWN
        self._control_bus.send(CTRL_SHUTDOWN)
        time.sleep(0.5)

        # Terminate subprocesses
        for node in self._launcher.nodes:
            if node.process is not None and node.process.poll() is None:
                node.process.terminate()
        time.sleep(1.0)
        for node in self._launcher.nodes:
            if node.process is not None and node.process.poll() is None:
                node.process.kill()
                node.process.wait()

        # Stop components
        self._launcher.shutdown()
        self._status_monitor.stop()
        self._control_bus.close()

        self._emit_event("success", msg="Session shut down.")

    def send_command(self, command: str, payload: bytes | None = None) -> None:
        """Broadcast a control command to all nodes.

        Args:
            command: Control command string (e.g. ``"START"``).
            payload: Optional payload frame.
        """
        self._control_bus.send(command, payload)
        self._emit_event(
            "info",
            msg=f"Command broadcast: {command}",
            level_override="success",
        )

    def is_shutting_down(self) -> bool:
        """``True`` if shutdown has been initiated."""
        return self._shutdown_event.is_set()

    # ------------------------------------------------------------------
    # Internal: node launching
    # ------------------------------------------------------------------

    def _launch_all_nodes(self) -> None:
        """Spawn all nodes in priority order.

        Input nodes (manus, priority 0) are launched first and waited
        on to reach ``STATUS_STARTED`` before downstream nodes start.
        """
        ordered = sorted(
            self._plan.nodes,
            key=lambda s: (s.priority, s.device_name),
        )

        input_nodes = [s for s in ordered if s.priority == 0]
        other_nodes = [s for s in ordered if s.priority != 0]

        # Phase 1: launch input nodes
        for spec in input_nodes:
            self._launcher.launch(spec)

        # Wait for input nodes to be ready
        for spec in input_nodes:
            runtime = self._launcher.node_map.get(spec.expected_node_id)
            if runtime is None:
                continue
            ready = self._wait_for_input_node_ready(runtime)
            if not ready:
                diagnostics = _build_diagnostics(runtime)
                self._emit_event(
                    "error",
                    msg=(
                        f"Input node '{spec.device_name}' failed to become "
                        f"ready within {_INPUT_READY_TIMEOUT_S:.0f}s. "
                        f"Aborting launch.\n{diagnostics}"
                    ),
                    node=spec.device_name,
                )
                self._launch_error = RuntimeError(
                    f"Input node '{spec.device_name}' is not ready. "
                    f"Aborting launch."
                )
                return

        # Phase 2: launch remaining nodes
        for spec in other_nodes:
            self._launcher.launch(spec)

    def _wait_for_input_node_ready(self, runtime: NodeRuntime) -> bool:
        """Block until *runtime* reaches STARTED or timeout.

        The status monitor is already running (started in ``start()``),
        so ZMQ status messages are being processed and ``runtime.status``
        and ``runtime.last_heartbeat`` are updated in real time.

        Args:
            runtime: The input node runtime to wait on.

        Returns:
            ``True`` if the node reached STARTED, ``False`` otherwise.
        """
        deadline = time.monotonic() + _INPUT_READY_TIMEOUT_S

        while time.monotonic() < deadline:
            if self._shutdown_event.is_set():
                return False

            # OS process must still be alive
            proc = runtime.process
            if proc is not None and proc.poll() is not None:
                return False

            # Must have received at least one ZMQ heartbeat
            if runtime.last_heartbeat <= 0:
                time.sleep(_INPUT_HEALTH_POLL_INTERVAL_S)
                continue

            # Check if STARTED
            if runtime.status == STATUS_STARTED:
                return True

            time.sleep(_INPUT_HEALTH_POLL_INTERVAL_S)

        return False

    # ------------------------------------------------------------------
    # Internal: event emission
    # ------------------------------------------------------------------

    #: Optional callback for structured log events.
    #: Signature: ``fn(level: str, msg: str, node: str = "")``
    _on_event_callback: Any = None

    def set_event_callback(self, fn: Any) -> None:
        """Set an optional callback for structured log events.

        Args:
            fn: ``fn(level: str, msg: str, node: str = "")``
        """
        self._on_event_callback = fn

    def _emit_event(
        self,
        level: str,
        msg: str,
        node: str = "",
        level_override: str = "",
    ) -> None:
        """Emit a structured log event.

        Args:
            level: Event level (``"info"``, ``"error"``, ``"success"``, …).
            msg: Human-readable message.
            node: Optional node identifier.
            level_override: Optional display-level override.
        """
        if self._on_event_callback is not None:
            try:
                self._on_event_callback(
                    level=level_override or level,
                    msg=msg,
                    node=node,
                )
            except Exception:
                pass

    def _on_node_state_change(
        self,
        runtime: NodeRuntime,
        old_status: str,
        new_status: str,
    ) -> None:
        """Handle node state transitions.

        Args:
            runtime: The node whose state changed.
            old_status: Previous status string.
            new_status: New status string.
        """
        self._emit_event(
            "info",
            msg=f"{old_status} → {new_status}",
            node=runtime.spec.device_name,
        )

        # Attempt restart on ERROR or DEAD (silent process death)
        if new_status in (STATUS_ERROR, "DEAD"):
            self._restart_node(runtime)

    # ------------------------------------------------------------------
    # Internal: node restart
    # ------------------------------------------------------------------

    def _restart_node(self, runtime: NodeRuntime) -> None:
        """Attempt to restart a failed node (non-input nodes only).

        Input nodes (manus, priority 0) are not auto-restarted because
        restart would require re-doing the priority launch sequence.

        Args:
            runtime: The node runtime to restart.
        """
        # Only restart non-input nodes
        if runtime.spec.priority == 0:
            self._emit_event(
                "warn",
                msg=(
                    f"Input node '{runtime.spec.device_name}' died — "
                    f"manual restart required"
                ),
                node=runtime.spec.device_name,
            )
            return

        if runtime.restart_count >= _MAX_NODE_RESTARTS:
            self._emit_event(
                "error",
                msg=(
                    f"Node '{runtime.spec.device_name}' reached max "
                    f"restarts ({_MAX_NODE_RESTARTS})"
                ),
                node=runtime.spec.device_name,
            )
            return

        self._emit_event(
            "warn",
            msg=f"Restarting node '{runtime.spec.device_name}' "
            f"(attempt {runtime.restart_count + 1}/{_MAX_NODE_RESTARTS})",
            node=runtime.spec.device_name,
        )

        # Terminate old process
        if runtime.process is not None and runtime.process.poll() is None:
            runtime.process.terminate()
            try:
                runtime.process.wait(timeout=3.0)
            except Exception:
                runtime.process.kill()
                runtime.process.wait()

        # Exponential backoff
        delay = _NODE_RESTART_BACKOFF_BASE**runtime.restart_count
        time.sleep(delay)

        # Reset tracking state and re-spawn
        runtime.status = "UNKNOWN"
        runtime.last_heartbeat = time.monotonic()
        runtime.restart_count += 1

        self._launcher.launch(runtime.spec)
