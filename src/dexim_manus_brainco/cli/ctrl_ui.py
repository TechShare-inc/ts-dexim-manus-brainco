"""Interactive TUI for the DexImitate control-plane command sender.

Provides an arrow-key menu that lets users select and send control commands
without memorising subcommand names.  Follows the same readchar + rich.live.Live
pattern established in dexim-manus calibration_ui.py.

Public API
----------
run_ctrl_session(endpoint, pub_send_fn)
    Start the interactive session loop.  Blocks until the user quits.
"""

from __future__ import annotations

import sys
import threading
import time
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
    TOPIC_CTRL,
)
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Sentinel for the "raw command" entry row
_RAW_CMD_SENTINEL = "__RAW__"

# Type alias: takes (endpoint: str, frames: list[bytes]) → None
PubSendFn = Callable[[str, list[bytes]], None]


@dataclass
class CtrlMenuItem:
    """One row in the interactive command menu.

    Attributes:
        label: Short display name shown in the menu.
        description: One-line description shown in the second column.
        cmd_const: The control-plane command constant to send, or
            ``_RAW_CMD_SENTINEL`` for the free-text raw entry row.
        needs_task_input: If True, pause Live and prompt for task-id and
            description before building the message frames.
        is_dangerous: If True, show a confirmation prompt before sending.
    """

    label: str
    description: str
    cmd_const: str
    needs_task_input: bool = False
    is_dangerous: bool = False


# Ordered menu: lifecycle → recording → raw
MENU_ITEMS: list[CtrlMenuItem] = [
    CtrlMenuItem(CTRL_START, "Begin robot control on all nodes", CTRL_START),
    CtrlMenuItem(CTRL_PAUSE, "Pause control (hold position, quick resume)", CTRL_PAUSE),
    CtrlMenuItem(CTRL_STOP, "Stop control (move to safe position)", CTRL_STOP),
    CtrlMenuItem(
        CTRL_STANDBY,
        "Return to standby (keep hardware connected, deactivate control)",
        CTRL_STANDBY,
    ),
    CtrlMenuItem(
        CTRL_SHUTDOWN,
        "Shut down all nodes",
        CTRL_SHUTDOWN,
        is_dangerous=True,
    ),
    CtrlMenuItem(CTRL_START_REC, "Start data recording", CTRL_START_REC),
    CtrlMenuItem(CTRL_STOP_REC, "Stop recording and save episode", CTRL_STOP_REC),
    CtrlMenuItem(
        CTRL_DISCARD_REC,
        "Discard current episode without saving",
        CTRL_DISCARD_REC,
        is_dangerous=True,
    ),
    CtrlMenuItem(
        CTRL_SET_TASK,
        "Set active task metadata on all nodes",
        CTRL_SET_TASK,
        needs_task_input=True,
    ),
    CtrlMenuItem(CTRL_START_PUB, "Resume data publishing", CTRL_START_PUB),
    CtrlMenuItem(CTRL_PAUSE_PUB, "Pause data publishing", CTRL_PAUSE_PUB),
    CtrlMenuItem(CTRL_STOP_PUB, "Stop data publishing", CTRL_STOP_PUB),
    CtrlMenuItem(
        _RAW_CMD_SENTINEL,
        "Send a raw control-plane command string",
        _RAW_CMD_SENTINEL,
    ),
]


def _build_header() -> Panel:
    """Render the banner / header panel."""
    from dexim.cli.common import print_banner as _print_banner

    console = get_console()
    with console.capture() as capture:
        _print_banner()
    banner_text = capture.get()

    header = Text.assemble(
        banner_text,
        Text("\n"),
        Text("MANUS & BRAINCO CONTROL PLANE", style="bold green"),
    )

    return Panel(header, border_style="green")


def _build_menu(selected_index: int) -> Panel:
    """Render the command menu with the selected row highlighted.

    Args:
        selected_index: Index of the currently selected menu item.

    Returns:
        A rich Panel containing the menu table.
    """
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="bold cyan", width=7)
    table.add_column("Command", style="bold white")
    table.add_column("Description", style="dim")

    for i, item in enumerate(MENU_ITEMS):
        key = f"{'▶':>3} {i:>2}" if i == selected_index else f"   {i:>2}"
        style = "reverse" if i == selected_index else ""
        table.add_row(key, f"[{style}]{item.label}[/]", item.description)

    return Panel(table, title="Control Commands", border_style="blue")


def _build_footer() -> Text:
    """Render the footer with keyboard shortcuts."""
    return Text(
        "↑↓ Navigate | Enter Send | q/Esc Quit",
        style="dim",
        justify="center",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_ctrl_session(endpoint: str, pub_send_fn: PubSendFn) -> None:
    """Start the interactive control-plane session loop.

    Args:
        endpoint: ZMQ bind endpoint for the PUB socket.
        pub_send_fn: Callable that sends frames on the PUB socket.
    """
    console = get_console()
    selected_index = 0
    should_exit = threading.Event()

    def _render() -> Group:
        return Group(
            _build_header(),
            _build_menu(selected_index),
            _build_footer(),
        )

    from rich.live import Live

    with Live(_render(), console=console, refresh_per_second=30, screen=True) as live:
        while not should_exit.is_set():
            try:
                key = readchar.readkey()
            except KeyboardInterrupt:
                break

            if key in (readchar.key.UP, "k"):
                selected_index = (selected_index - 1) % len(MENU_ITEMS)
            elif key in (readchar.key.DOWN, "j"):
                selected_index = (selected_index + 1) % len(MENU_ITEMS)
            elif key in (readchar.key.ENTER, "\r", "\n"):
                item = MENU_ITEMS[selected_index]
                live.stop()

                if item.cmd_const == _RAW_CMD_SENTINEL:
                    console.print()
                    raw = input("  Command string: ").strip()
                    console.print()
                    if raw:
                        frames: list[bytes] = [
                            TOPIC_CTRL,
                            raw.encode("utf-8"),
                        ]
                        pub_send_fn(endpoint, frames)
                elif item.is_dangerous:
                    console.print()
                    confirm = (
                        input(f"  ⚠  Really send [bold red]{item.label}[/]? [y/N] ")
                        .strip()
                        .lower()
                    )
                    console.print()
                    if confirm in ("y", "yes"):
                        frames = [TOPIC_CTRL, item.cmd_const.encode("utf-8")]
                        pub_send_fn(endpoint, frames)
                elif item.needs_task_input:
                    console.print()
                    task_id = input("  Task ID: ").strip()
                    task_desc = input("  Description: ").strip()
                    console.print()
                    if task_id:
                        payload = (
                            f'{{"task_id":"{task_id}","description":"{task_desc}"}}'
                        ).encode("utf-8")
                        frames = [
                            TOPIC_CTRL,
                            item.cmd_const.encode("utf-8"),
                            payload,
                        ]
                        pub_send_fn(endpoint, frames)
                else:
                    frames = [TOPIC_CTRL, item.cmd_const.encode("utf-8")]
                    pub_send_fn(endpoint, frames)

                live.start()
            elif key in (readchar.key.ESC, "q"):
                should_exit.set()
