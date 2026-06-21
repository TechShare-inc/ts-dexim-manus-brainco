"""Interactive 4-pane TUI for the ``dexim launch`` command.

Layout::

    +------------------------------------------------------------------+
    |  BANNER                                                          |
    |  DDDDDD  EEEEEEE X   X III M     M                               |
    |  D     D E       X X   I  MM   MM                               |
    |  D     D EEEE     X    I  M M M M                               |
    |  D     D E       X X   I  M  M  M                               |
    |  DDDDDD  EEEEEEE X   X III M     M                               |
    |                                                                  |
    |  MANUS & BRAINCO                                                 |
    +---------------------------+--------------------------------------+
    |  MENU                     |  STATUS                              |
    |                           |                                      |
    |  Session info             |  Per-node health table               |
    |  Lifecycle commands       |  State / Uptime / Recording          |
    |  (START/STOP/PAUSE/...)   |                                      |
    |                           |                                      |
    |  q quit                   |                                      |
    +---------------------------+--------------------------------------+
    |  LOG  (session events)                                           |
    |  [12:03:01] Session "single-hand-teleop-mock" started            |
    |  [12:03:02] + brainco_left_mock  Launched (PID 12345)            |
    +------------------------------------------------------------------+

Follows the same ``readchar`` + ``rich.live.Live`` pattern as ``ctrl_ui.py``.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import readchar
from dexim.cli.common import get_console
from dexim.core.messages import (
    CTRL_DISCARD_REC,
    CTRL_PAUSE,
    CTRL_PAUSE_PUB,
    CTRL_SET_TASK,
    CTRL_SHUTDOWN,
    CTRL_STANDBY,
    CTRL_START,
    CTRL_START_PUB,
    CTRL_START_REC,
    CTRL_STOP,
    CTRL_STOP_PUB,
    CTRL_STOP_REC,
    STATUS_ERROR,
    STATUS_INITIALIZED,
    STATUS_PAUSED,
    STATUS_STARTED,
    STATUS_STARTING,
)
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .launch.orchestrator import LaunchOrchestrator
from .log_server import LogStore
from .menu_item import MenuItem, _RAW_CMD_SENTINEL
from .session_loader import SessionPlan

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Callback: takes a command string (e.g. "START") and returns nothing.
CommandFn = Callable[[str], None]

# ---------------------------------------------------------------------------
# Menu definition
# ---------------------------------------------------------------------------


#: High-level teleoperation commands.
TELEOP_MENU: list[MenuItem] = [
    MenuItem("START", CTRL_START),
    MenuItem("STOP", CTRL_STOP),
    MenuItem("PAUSE", CTRL_PAUSE),
    MenuItem("STANDBY", CTRL_STANDBY),
    MenuItem("SHUTDOWN", CTRL_SHUTDOWN, is_dangerous=True),
    MenuItem("START_REC", CTRL_START_REC),
    MenuItem("STOP_REC", CTRL_STOP_REC),
    MenuItem("DISCARD_REC", CTRL_DISCARD_REC, is_dangerous=True),
]

#: Low-level / raw control-plane commands.
RAW_MENU: list[MenuItem] = [
    MenuItem("START_PUB", CTRL_START_PUB, section="Raw"),
    MenuItem("PAUSE_PUB", CTRL_PAUSE_PUB, section="Raw"),
    MenuItem("STOP_PUB", CTRL_STOP_PUB, section="Raw"),
    MenuItem("SET_TASK", CTRL_SET_TASK, section="Raw", needs_text_input=True),
    MenuItem("RAW COMMAND", _RAW_CMD_SENTINEL, section="Raw", needs_text_input=True),
]

#: Flat list of all menu items (combined, preserving section order).
MENU_ITEMS: list[MenuItem] = TELEOP_MENU + RAW_MENU

# ---------------------------------------------------------------------------
# TUI session state
# ---------------------------------------------------------------------------


@dataclass
class LaunchSessionState:
    """Shared mutable state for the TUI.

    Attributes:
        selected_idx: Currently highlighted menu row index.
        last_result: Rich-markup status line shown below the menu.
        phase: ``"active"`` while running, ``"done"`` after quit.
        send_event: Set by keyboard thread on Enter.
        quit_event: Set on q / Esc / Ctrl+C.
        lock: Guards mutable fields.
    """

    selected_idx: int = 0
    last_result: str = "[muted]Session running. Select a command.[/]"
    phase: str = "active"
    send_event: threading.Event = field(default_factory=threading.Event)
    quit_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # Log ring buffer (shared with LogStore for TUI display)
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=300))


# ---------------------------------------------------------------------------
# Keyboard reader thread
# ---------------------------------------------------------------------------


def _keyboard_thread(state: LaunchSessionState, num_items: int) -> None:
    """Daemon thread: read keys and update shared state.

    Args:
        state: Shared session state.
        num_items: Number of menu items (for index clamping).
    """

    def _run() -> None:
        if sys.platform == "win32":
            import msvcrt

            def _key_available() -> bool:
                return msvcrt.kbhit()

        else:
            import select as _sel

            def _key_available() -> bool:
                r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                return bool(r)

        while not state.quit_event.is_set():
            if not _key_available():
                time.sleep(0.02)
                continue

            try:
                key = readchar.readkey()
            except Exception:
                state.quit_event.set()
                break

            if key in (readchar.key.CTRL_C, readchar.key.ESC, "q", "Q"):
                state.quit_event.set()
                break

            if key == readchar.key.UP:
                with state.lock:
                    state.selected_idx = max(0, state.selected_idx - 1)

            elif key == readchar.key.DOWN:
                with state.lock:
                    state.selected_idx = min(num_items - 1, state.selected_idx + 1)

            elif key in (readchar.key.ENTER, "\r", "\n"):
                state.send_event.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Status colour helper
# ---------------------------------------------------------------------------


def _status_style(status: str, healthy: bool) -> str:
    """Return a Rich style string for a node status.

    Args:
        status: Status string from the node.
        healthy: Whether the node has a recent heartbeat.

    Returns:
        A Rich markup style (e.g. ``"bold green"``).
    """
    if not healthy:
        return "bold red"
    if status == STATUS_STARTED:
        return "bold green"
    if status in (STATUS_INITIALIZED, STATUS_STARTING):
        return "bold yellow"
    if status in (STATUS_PAUSED,):
        return "bold cyan"
    if status == STATUS_ERROR:
        return "bold red"
    if status == "SHUTTING_DOWN":
        return "dim"
    return "white"


def _format_uptime(sec: float) -> str:
    """Format seconds into a human-readable uptime string.

    Args:
        sec: Seconds.

    Returns:
        String like ``"1:23"`` or ``"0:05"``.
    """
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


def _render_menu(
    state: LaunchSessionState,
    manager: LaunchOrchestrator,
    plan: SessionPlan,
    http_url: str,
) -> Panel:
    """Build the left Menu pane.

    Args:
        state: TUI state.
        manager: Launch orchestrator (for node counts).
        plan: Session plan (for name/description).
        http_url: HTTP log viewer URL.

    Returns:
        A Rich Panel.
    """
    with state.lock:
        selected = state.selected_idx

    alive = sum(1 for n in manager.nodes if n.is_running)
    total = len(manager.nodes)

    lines: list[Text] = []

    # Session header
    lines.append(Text.from_markup(f"[brand]Session:[/] [key]{plan.name}[/]"))
    lines.append(Text.from_markup(f"[muted]Nodes:[/] {alive}/{total} running"))
    if plan.description:
        lines.append(Text.from_markup(f"[muted]{plan.description}[/]"))
    lines.append(Text(""))

    # HTTP link
    lines.append(Text.from_markup(f"[muted]Log:[/] [link={http_url}]{http_url}[/]"))
    lines.append(Text(""))

    # Session-incomplete warning
    if not manager.is_complete:
        lines.append(
            Text.from_markup(
                "[on #440] ⚠ Session incomplete — some nodes failed to launch or are unhealthy [/]"
            )
        )
        if manager.launch_error is not None:
            lines.append(Text.from_markup(f"[error]   {manager.launch_error}[/]"))
        lines.append(Text(""))

    # High-Level Teleop commands
    lines.append(Text.from_markup("[muted]-- Teleop --[/]"))
    teleop_start = 0
    for idx, item in enumerate(MENU_ITEMS):
        if item.section == "Raw":
            # Section break before raw commands
            lines.append(Text(""))
            lines.append(Text.from_markup("[muted]-- Raw Commands --[/]"))
            teleop_start = -1  # sentinel to skip re-printing header
        if item.section == "Raw" and teleop_start != -1:
            pass  # already printed header

        prefix = ">" if idx == selected else " "
        label = item.label
        if item.is_dangerous:
            label = f"[error]{label}[/]"
        elif item.section == "Raw":
            label = f"[muted]{label}[/]"
        else:
            label = f"[key]{label}[/]"

        if idx == selected:
            lines.append(Text.from_markup(f"[bold on dark_blue]{prefix} {label}[/]"))
        else:
            lines.append(Text.from_markup(f" {prefix} {label}"))

    lines.append(Text(""))
    lines.append(Text.from_markup(f"  {state.last_result}"))
    lines.append(Text(""))
    lines.append(
        Text.from_markup(
            "[muted]q[/] quit  [muted]Up/Down[/] select  [muted]Enter[/] send"
        )
    )

    return Panel(
        Group(*lines),
        title=Text("Menu", style="brand"),
        border_style="brand",
        padding=(0, 1),
    )


def _render_status(manager: LaunchOrchestrator) -> Panel:
    """Build the right Status pane showing per-node health.

    Args:
        manager: Launch orchestrator with node runtimes.

    Returns:
        A Rich Panel containing a health table.
    """
    table = Table(
        show_header=True,
        header_style="table.header",
        border_style="brand.dim",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Node", style="key", min_width=12, no_wrap=True)
    table.add_column("State", min_width=12)
    table.add_column("Mode", min_width=8)
    table.add_column("HW", min_width=4, justify="center")
    table.add_column("Data", min_width=8)
    table.add_column("Uptime", min_width=6, justify="right")
    table.add_column("Rec", min_width=4, justify="center")
    table.add_column("PS", min_width=4, justify="center")
    table.add_column("OK", min_width=4, justify="center")

    for node in manager.nodes:
        healthy = node.is_healthy
        state_style = _status_style(node.status, healthy)
        rec = "[success]R[/]" if node.is_recording else "[muted]-[/]"
        ok = "[success]Y[/]" if healthy else "[error]N[/]"

        # Mode: interface mode badge.
        mode = node.info.interface_mode or "--"
        if mode in ("mock", "sim"):
            mode_display = f"[bold yellow]{mode}[/]"
        elif mode in ("real", "serial", "dds", "usb", "realsense"):
            mode_display = f"[bold green]{mode}[/]"
        else:
            mode_display = f"[muted]{mode}[/]"

        # HW: hardware connection indicator.
        hw = node.info.hardware_connected
        if hw is True:
            hw_display = "[success]Y[/]"
        elif hw is False:
            hw_display = "[error]N[/]"
        else:
            hw_display = "[muted]-[/]"

        # Data: data age or rate.
        data = "--"
        if node.info.data_rate_hz > 0:
            data = f"{node.info.data_rate_hz:.0f}Hz"
        elif node.info.data_age_sec is not None and node.info.data_age_sec > 0:
            data = f"{node.info.data_age_sec * 1000:.0f}ms"

        table.add_row(
            node.spec.device_name,
            f"[{state_style}]{node.status}[/]",
            mode_display,
            hw_display,
            data,
            _format_uptime(node.uptime_sec),
            rec,
            "[success]Y[/]" if node.ps_healthy else "[error]N[/]",
            ok,
        )

    # If no nodes launched yet
    if not manager.nodes:
        table.add_row(
            "[muted](no nodes)[/]",
            "[muted]--[/]",
            "[muted]--[/]",
            "[muted]-[/]",
            "[muted]--[/]",
            "[muted]--[/]",
            "[muted]-[/]",
            "[muted]-[/]",
            "[muted]-[/]",
        )

    return Panel(
        table,
        title=Text("Status", style="brand"),
        border_style="brand",
        padding=(0, 1),
    )


def _render_log(state: LaunchSessionState) -> Panel:
    """Build the bottom Log pane from the ring buffer.

    Args:
        state: TUI state with log_lines deque.

    Returns:
        A Rich Panel containing the last N log lines.
    """
    with state.lock:
        lines = list(state.log_lines)

    if not lines:
        text = Text("[muted]Waiting for events...[/]")
    else:
        # Show last ~20 lines (includes subprocess stdout)
        visible = lines[-20:]
        text = Text("\n").join(Text.from_markup(line) for line in visible)

    return Panel(
        text,
        title=Text("Session Log", style="brand"),
        border_style="brand.dim",
        padding=(0, 1),
    )


def _render_banner() -> Panel:
    """Build the top Banner pane with ASCII art title.

    Returns:
        A Rich Panel with a large "DEXIM" ASCII art header
        and "MANUS & BRAINCO" subtitle.
    """
    ascii_art = Text(
        """
██████  ███████ ██   ██ ██ ███    ███
██   ██ ██       ██ ██  ██ ████  ████
██   ██ █████     ███   ██ ██ ████ ██
██   ██ ██       ██ ██  ██ ██  ██  ██
██████  ███████ ██   ██ ██ ██      ██
""",
        style="brand",
    )

    subtitle = Text("MANUS & BRAINCO", style="brand.dim", justify="center")

    return Panel(
        Group(ascii_art, subtitle),
        border_style="brand",
        padding=(0, 2),
    )


def _render_countdown_overlay(manager: LaunchOrchestrator) -> Panel | None:
    """Build a popup-styled countdown overlay panel.

    Returns ``None`` when no node is currently counting down.

    Args:
        manager: Launch orchestrator with node runtimes.

    Returns:
        A Rich Panel, or None.
    """
    counting_down = [
        n
        for n in manager.nodes
        if n.status == STATUS_STARTING and n.info.countdown_remaining is not None
    ]
    if not counting_down:
        return None

    lines: list[Text] = []
    lines.append(Text("TELEOP STARTING", style="bold yellow", justify="center"))
    lines.append(Text(""))

    for node in counting_down:
        remaining = node.info.countdown_remaining
        name = node.spec.device_name
        sec = max(0, int(remaining))
        # Large countdown digit for visual pop.
        lines.append(
            Text(f"  {name}:  {sec}s", style="bold bright_yellow", justify="center")
        )

    lines.append(Text(""))
    lines.append(Text("Press CTRL_STOP to abort", style="muted", justify="center"))

    return Panel(
        Group(*lines),
        title=Text("[bold yellow] COUNTDOWN ", justify="center"),
        border_style="bold yellow",
        padding=(0, 4),
    )


def _build_layout(
    state: LaunchSessionState,
    manager: LaunchOrchestrator,
    plan: SessionPlan,
    http_url: str,
) -> Layout:
    """Assemble the full 4-pane layout with banner (and countdown overlay).

    When one or more nodes are in STARTING state with an active
    countdown, a popup-styled overlay is inserted between the banner
    and the main content area.

    Args:
        state: TUI state.
        manager: Launch orchestrator.
        plan: Session plan.
        http_url: HTTP log viewer URL.

    Returns:
        A Rich Layout tree.
    """
    countdown_panel = _render_countdown_overlay(manager)

    layout = Layout()
    if countdown_panel is not None:
        layout.split_column(
            Layout(name="banner", size=9),
            Layout(name="countdown", size=7),
            Layout(name="top", ratio=3),
            Layout(name="bottom", ratio=2),
        )
        layout["countdown"].update(countdown_panel)
    else:
        layout.split_column(
            Layout(name="banner", size=9),
            Layout(name="top", ratio=3),
            Layout(name="bottom", ratio=2),
        )

    layout["top"].split_row(
        Layout(name="menu", ratio=1),
        Layout(name="status", ratio=2),
    )

    layout["banner"].update(_render_banner())
    layout["menu"].update(_render_menu(state, manager, plan, http_url))
    layout["status"].update(_render_status(manager))
    layout["bottom"].update(_render_log(state))

    return layout


# ---------------------------------------------------------------------------
# Log sync: feed LogStore events into the TUI ring buffer
# ---------------------------------------------------------------------------


def _start_log_sync(log_store: LogStore, state: LaunchSessionState) -> threading.Thread:
    """Start a daemon thread that copies LogStore events into the TUI buffer.

    The thread polls ``log_store.tail()`` periodically and appends
    formatted lines to ``state.log_lines``.  Tracks the last seen event
    by ``id()`` rather than count, because the ring buffer wraps and
    ``tail()`` returns a fixed-size window.

    Args:
        log_store: The shared log store.
        state: TUI state.

    Returns:
        The daemon thread (already started).
    """
    last_event_id: int | None = None

    def _sync() -> None:
        nonlocal last_event_id
        while not state.quit_event.is_set():
            time.sleep(0.1)
            events = log_store.tail(50)
            if not events:
                continue

            # Find the slice of new events since the last one we processed.
            # Use id() because tail() returns references to the same dict
            # objects that live in LogStore's deque -- identity is stable
            # across calls.
            if last_event_id is not None:
                start = 0
                for i, ev in enumerate(events):
                    if id(ev) == last_event_id:
                        start = i + 1
                        break
                new_events = events[start:]
            else:
                new_events = list(events)

            if not new_events:
                continue

            last_event_id = id(new_events[-1])

            with state.lock:
                for ev in new_events:
                    ts = ev.get("ts", "")
                    level = ev.get("level", "info")
                    node = ev.get("node", "")
                    msg = ev.get("msg", "")

                    # Build a coloured line -- debug lines are dimmed
                    level_styles = {
                        "info": "key",
                        "success": "success",
                        "warn": "warning",
                        "error": "error",
                        "debug": "muted dim",
                    }
                    lvl_style = level_styles.get(level, "key")

                    if node:
                        line = (
                            f"[muted]{ts}[/] "
                            f"[{lvl_style}]{level:<7}[/] "
                            f"[brand]{node:<20}[/] "
                            f"{msg}"
                        )
                    else:
                        line = f"[muted]{ts}[/] [{lvl_style}]{level:<7}[/] {msg}"
                    state.log_lines.append(line)

    t = threading.Thread(target=_sync, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Main TUI entry point
# ---------------------------------------------------------------------------


def run_launch_ui(
    manager: LaunchOrchestrator,
    plan: SessionPlan,
    log_store: LogStore,
    http_url: str,
) -> None:
    """Start the interactive launch TUI session.

    Blocks until the user quits (q / Esc / Ctrl+C).

    Args:
        manager: The launch orchestrator (nodes already started).
        plan: The session plan.
        log_store: Shared log store for event display.
        http_url: HTTP log viewer URL shown in the menu pane.
    """
    from rich.prompt import Confirm, Prompt

    console = get_console()
    state = LaunchSessionState()

    # Start keyboard thread
    _keyboard_thread(state, len(MENU_ITEMS))

    # Start log sync from LogStore -> TUI buffer
    _start_log_sync(log_store, state)

    layout = _build_layout(state, manager, plan, http_url)

    with Live(
        layout,
        console=console,
        refresh_per_second=10,
        screen=True,
    ) as live:
        while not state.quit_event.is_set():
            if state.send_event.is_set():
                state.send_event.clear()

                with state.lock:
                    item = MENU_ITEMS[state.selected_idx]

                # -- Dangerous commands: confirm first --
                if item.is_dangerous:
                    live.stop()
                    try:
                        confirmed = Confirm.ask(
                            f"[warning]Send {item.cmd} to all nodes?[/]",
                            console=console,
                            default=False,
                        )
                    except (KeyboardInterrupt, EOFError):
                        confirmed = False
                    live.start()
                    if not confirmed:
                        state.last_result = "[muted]Cancelled.[/]"
                        live.update(_build_layout(state, manager, plan, http_url))
                        continue

                # -- Text-input commands: prompt for value(s) --
                if item.needs_text_input:
                    if item.cmd == CTRL_SET_TASK:
                        # SET_TASK: prompt for task_id + optional description
                        live.stop()
                        try:
                            task_id = Prompt.ask(
                                "[key]task-id[/]", console=console
                            ).strip()
                            task_desc = Prompt.ask(
                                "[key]description[/] [muted](optional)[/]",
                                console=console,
                                default="",
                            ).strip()
                        except (KeyboardInterrupt, EOFError):
                            task_id = ""
                            task_desc = ""
                        live.start()

                        if not task_id:
                            state.last_result = "[warning]task-id is required.[/]"
                            live.update(_build_layout(state, manager, plan, http_url))
                            continue

                        import json as _json

                        payload = _json.dumps(
                            {
                                "task_id": task_id,
                                "task_description": task_desc,
                            }
                        )
                        manager.send_command(item.cmd, payload=payload.encode())
                        state.last_result = (
                            f"[success]Sent: SET_TASK (task_id={task_id!r})[/]"
                        )

                    elif item.cmd == _RAW_CMD_SENTINEL:
                        # Raw command: free-text input
                        live.stop()
                        try:
                            raw = (
                                Prompt.ask("[key]raw command[/]", console=console)
                                .strip()
                                .upper()
                            )
                        except (KeyboardInterrupt, EOFError):
                            raw = ""
                        live.start()

                        if not raw:
                            state.last_result = "[muted]Cancelled.[/]"
                            live.update(_build_layout(state, manager, plan, http_url))
                            continue

                        manager.send_command(raw)
                        state.last_result = f"[success]Sent: {raw}[/]"

                    live.update(_build_layout(state, manager, plan, http_url))
                    continue

                # -- Standard command --
                manager.send_command(item.cmd)
                state.last_result = f"[success]Sent: {item.cmd}[/]"

            # Re-render
            live.update(_build_layout(state, manager, plan, http_url))
            time.sleep(0.1)

    console.print("\n[muted]Launch session ended.[/]")
