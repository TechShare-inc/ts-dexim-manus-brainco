"""Welcome TUI for ``dexim launch`` -- session picker shown when no session
is specified on the command line.

Scans ``config/dexim/session/`` for available session YAML files and
presents an interactive arrow-key menu so the user can browse, preview,
and select a session to launch.

Follows the same ``readchar`` + ``rich.live.Live`` pattern as
``ctrl_ui.py`` and ``launch_ui.py``.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import readchar
import yaml
from dexim.cli.common import get_console
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .session_loader import get_session_dir, list_sessions

# ---------------------------------------------------------------------------
# Session preview
# ---------------------------------------------------------------------------


@dataclass
class SessionPreview:
    """Lightweight session metadata for the welcome screen.

    Attributes:
        filename: The session YAML stem (e.g. ``"bimanual-teleop-mock"``).
        name: Display name from the ``session.name`` field.
        description: Human-readable description.
        node_count: Number of nodes in the session.
    """

    filename: str
    name: str
    description: str = ""
    node_count: int = 0


def _parse_session_preview(session_dir: Path, filename: str) -> SessionPreview:
    """Read a session YAML and extract metadata without full resolution.

    Args:
        session_dir: Path to the session directory.
        filename: Session YAML stem (without ``.yaml``).

    Returns:
        A :class:`SessionPreview` with parsed metadata.
    """
    path = session_dir / f"{filename}.yaml"
    try:
        with path.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}
    except Exception:
        return SessionPreview(filename=filename, name=filename)

    meta = data.get("session", {})
    if not isinstance(meta, dict):
        return SessionPreview(filename=filename, name=filename)

    raw_nodes: list[dict[str, Any]] = data.get("nodes", [])
    node_count = sum(1 for n in raw_nodes if isinstance(n, dict))

    return SessionPreview(
        filename=filename,
        name=str(meta.get("name", filename)),
        description=str(meta.get("description", "")),
        node_count=node_count,
    )


def _load_all_session_previews(config_dir: str) -> list[SessionPreview]:
    """Scan the session directory and return previews for all sessions.

    Args:
        config_dir: Config root path.

    Returns:
        List of :class:`SessionPreview`, sorted alphabetically by filename.
    """
    session_dir = get_session_dir(config_dir)
    filenames = list_sessions(config_dir)
    return [_parse_session_preview(session_dir, fn) for fn in filenames]


# ---------------------------------------------------------------------------
# Welcome TUI state
# ---------------------------------------------------------------------------


@dataclass
class WelcomeState:
    """Mutable state for the welcome TUI.

    Attributes:
        selected_idx: Currently highlighted session index.
        quit_event: Set on q / Esc / Ctrl+C.
        select_event: Set on Enter -- user picked a session.
        selected_session: The filename of the chosen session (set on Enter).
    """

    selected_idx: int = 0
    quit_event: threading.Event = field(default_factory=threading.Event)
    select_event: threading.Event = field(default_factory=threading.Event)
    selected_session: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# Keyboard reader thread
# ---------------------------------------------------------------------------


def _keyboard_thread(state: WelcomeState, num_items: int) -> None:
    """Daemon thread: read keys and update shared state.

    Args:
        state: Shared welcome state.
        num_items: Number of session items (for index clamping).
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
                state.select_event.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _render_banner() -> Panel:
    """Build the top banner pane with ASCII art title.

    Returns:
        A Rich Panel with a large "DEXIM" ASCII art header
        and "MANUS & BRAINCO" subtitle.
    """
    ascii_art = Text(
        """\n\u2588\u2588\u2588\u2588\u2588\u2588  \u2588\u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588   \u2588\u2588 \u2588\u2588 \u2588\u2588\u2588    \u2588\u2588\u2588
\u2588\u2588   \u2588\u2588 \u2588\u2588       \u2588\u2588 \u2588\u2588  \u2588\u2588 \u2588\u2588\u2588\u2588  \u2588\u2588\u2588\u2588
\u2588\u2588   \u2588\u2588 \u2588\u2588\u2588\u2588\u2588     \u2588\u2588\u2588   \u2588\u2588 \u2588\u2588 \u2588\u2588\u2588\u2588 \u2588\u2588
\u2588\u2588   \u2588\u2588 \u2588\u2588       \u2588\u2588 \u2588\u2588  \u2588\u2588 \u2588\u2588  \u2588\u2588  \u2588\u2588
\u2588\u2588\u2588\u2588\u2588\u2588  \u2588\u2588\u2588\u2588\u2588\u2588\u2588 \u2588\u2588   \u2588\u2588 \u2588\u2588 \u2588\u2588      \u2588\u2588
""",
        style="brand",
    )

    subtitle = Text("MANUS & BRAINCO", style="brand.dim", justify="center")

    return Panel(
        Group(ascii_art, subtitle),
        border_style="brand",
        padding=(0, 2),
    )


def _render_session_list(
    previews: list[SessionPreview],
    state: WelcomeState,
) -> Panel:
    """Build the session selection pane.

    Args:
        previews: All available session previews.
        state: Welcome TUI state.

    Returns:
        A Rich Panel containing the scrollable session list.
    """
    with state.lock:
        selected = state.selected_idx

    lines: list[Text] = []
    lines.append(Text.from_markup("[muted]Select a session to launch:[/]"))
    lines.append(Text(""))

    if not previews:
        lines.append(Text.from_markup("[error]No session files found.[/]"))
        lines.append(
            Text.from_markup("[muted]Add .yaml files to config/dexim/session/[/]")
        )
    else:
        for idx, pv in enumerate(previews):
            prefix = ">" if idx == selected else " "
            if idx == selected:
                label = Text.from_markup(
                    f"[bold on dark_blue]{prefix} [key]{pv.name}[/] "
                    f"[muted]({pv.node_count} node(s))[/]"
                )
            else:
                label = Text.from_markup(
                    f" {prefix} [key]{pv.name}[/] [muted]({pv.node_count} node(s))[/]"
                )
            lines.append(label)

            # Show description on a second line for the selected item,
            # or show it dimmed for all items
            if pv.description:
                if idx == selected:
                    lines.append(Text.from_markup(f"   [muted]{pv.description}[/]"))
                else:
                    lines.append(Text.from_markup(f"   [muted dim]{pv.description}[/]"))

    lines.append(Text(""))
    lines.append(
        Text.from_markup(
            "[muted]q[/] quit  [muted]Up/Down[/] select  [muted]Enter[/] launch"
        )
    )

    return Panel(
        Group(*lines),
        title=Text("Welcome", style="brand"),
        border_style="brand",
        padding=(0, 2),
    )


def _build_welcome_layout(
    previews: list[SessionPreview],
    state: WelcomeState,
) -> Group:
    """Assemble the welcome screen layout.

    Args:
        previews: All available session previews.
        state: Welcome TUI state.

    Returns:
        A Rich Group (stacked panels).
    """
    return Group(
        _render_banner(),
        _render_session_list(previews, state),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_welcome_ui(config_dir: str) -> str | None:
    """Start the interactive welcome / session-picker TUI.

    Blocks until the user picks a session or quits.

    Args:
        config_dir: Config root path.

    Returns:
        The selected session filename stem, or ``None`` if the user quit.
    """
    console = get_console()
    previews = _load_all_session_previews(config_dir)
    state = WelcomeState()

    if not previews:
        console.print(
            f"\n[error]No session files found in {get_session_dir(config_dir)}[/]"
        )
        console.print(
            "[muted]Add .yaml files to config/dexim/session/ "
            "or specify --session directly.[/]"
        )
        return None

    num_items = len(previews)

    # Start keyboard thread
    _keyboard_thread(state, num_items)

    layout = _build_welcome_layout(previews, state)

    with Live(
        layout,
        console=console,
        refresh_per_second=10,
        screen=True,
    ) as live:
        while not state.quit_event.is_set() and not state.select_event.is_set():
            live.update(_build_welcome_layout(previews, state))
            time.sleep(0.1)

        if state.select_event.is_set():
            with state.lock:
                chosen = previews[state.selected_idx].filename
            live.stop()
            return chosen

        live.stop()
        return None
