"""``dexim launch`` - Start a multi-node session from a session config.

Usage::

    dexim launch
    dexim launch --session single-hand-teleop-mock
    dexim launch --session bimanual-teleop-mock --config-dir ./config
    dexim launch --session single-hand-teleop-mock --log-port 8090

When run without ``--session``, a Welcome TUI scans for available session
files and lets the user pick one interactively.

This command reads a session YAML from ``config/dexim/session/``, spawns
all nodes as subprocesses, opens an interactive TUI for monitoring
and lifecycle control, and starts an HTTP log server for browser access.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import rich_click as click
from dexim.cli.common import get_console, handle_cli_error

from .launch.orchestrator import LaunchOrchestrator
from .launch_ui import run_launch_ui
from .log_server import LogServer
from .session_loader import (
    get_config_dir,
    get_session_dir,
    list_sessions,
    load_session,
)
from .welcome_ui import run_welcome_ui

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.command(name="launch", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--session",
    "-s",
    required=False,
    default=None,
    help=(
        "Session name (without .yaml extension). If omitted, an interactive "
        "picker is shown."
    ),
)
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Config root directory (default: ./config or DEXIM_CONFIG_DIR).",
)
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default="./logs",
    show_default=True,
    help="Directory for rotating debug log files.",
)
@click.option(
    "--log-port",
    type=int,
    default=8090,
    show_default=True,
    help="HTTP port for the live log viewer.",
)
@click.option(
    "--no-http",
    is_flag=True,
    default=False,
    help="Disable the HTTP log server.",
)
@handle_cli_error
def launch_command(
    session: str | None,
    config_dir: Path | None,
    log_dir: Path | None,
    log_port: int,
    no_http: bool,
) -> None:
    """Launch a multi-node session from a session config.

    Reads a session YAML from config/dexim/session/<name>.yaml, spawns all
    nodes as subprocesses, and opens an interactive TUI for monitoring and
    lifecycle control.

    \b
    Examples:
        dexim launch
        dexim launch --session single-hand-teleop-mock
        dexim launch -s bimanual-teleop-mock --log-port 9090
        dexim launch -s single-hand-teleop-mock --no-http
    """
    console = get_console()

    # Resolve config directory
    config_root = get_config_dir(str(config_dir) if config_dir else None)
    config_root_str = str(config_root)

    # If no session specified, show the interactive Welcome TUI
    if session is None:
        session = run_welcome_ui(config_root_str)
        if session is None:
            console.print("\n[muted]Launch cancelled.[/]")
            return

    # Validate session exists
    available = list_sessions(config_root_str)
    if session not in available:
        session_dir = get_session_dir(config_root_str)
        console.print(f"[error]Session '{session}' not found.[/]")
        if available:
            console.print("[muted]Available sessions:[/]")
            for s in available:
                console.print(f"  - {s}")
        else:
            console.print(f"[muted]No session files found in {session_dir}[/]")
        raise SystemExit(1)

    # Load and resolve the session plan
    console.print(f"[info]Loading session [key]{session}[/key]...[/]")
    plan = load_session(session, config_root_str)
    console.print(f"  Session: [key]{plan.name}[/]  ({len(plan.nodes)} node(s))")
    for node in plan.nodes:
        console.print(
            f"    [muted]{node.device_name}[/] ({node.package} / {node.config_name})"
        )

    # Start HTTP log server
    log_server: LogServer | None = None
    http_url = "(disabled)"
    if not no_http:
        log_server = LogServer(port=log_port)
        log_server.start()
        http_url = log_server.url
        console.print(f"[info]Log viewer: [link={http_url}]{http_url}[/][/]")

    # Create log store (use HTTP server's if available, else standalone)
    log_store = log_server.store if log_server else LogServer().store

    # Create launch orchestrator
    log_dir_str = str(log_dir) if log_dir else "./logs"
    orchestrator = LaunchOrchestrator(plan=plan, log_dir=log_dir_str)

    # Wire structured event emission into the LogStore (TUI + HTTP SSE).
    def _emit_to_log(
        level: str,
        msg: str,
        node: str = "",
        level_override: str = "",
    ) -> None:
        from datetime import datetime

        ts = datetime.now().strftime("%H:%M:%S")
        event: dict[str, Any] = {
            "ts": ts,
            "level": level_override or level,
            "msg": msg,
        }
        if node:
            event["node"] = node
        log_store.append(event)

    orchestrator.set_event_callback(_emit_to_log)

    # Launch nodes in a background thread so the TUI appears immediately.
    # Input nodes (manus) are launched first; the orchestrator waits for
    # them to reach STARTED before launching downstream nodes -- all while
    # the TUI shows live progress (status monitor is already running).
    def _do_launch() -> None:
        try:
            orchestrator.start()
        except Exception as exc:
            # start() should not raise; launch errors are captured
            # internally via orchestrator.launch_error.
            _emit_to_log("error", f"Unexpected launch error: {exc}")

    launch_thread = threading.Thread(target=_do_launch, daemon=True)
    launch_thread.start()

    try:
        # TUI starts immediately -- shows nodes as they come online
        run_launch_ui(
            manager=orchestrator,
            plan=plan,
            log_store=log_store,
            http_url=http_url,
        )

        # After TUI exits, check if the background launch failed
        if orchestrator.launch_error:
            console.print(f"[error]Launch failed: {orchestrator.launch_error}[/]")
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted. Shutting down...[/]")
    finally:
        orchestrator.stop()
        if log_server:
            console.print("[muted]Stopping HTTP log server...[/]")
            log_server.stop()
        console.print("[muted]Launch session ended.[/]")
