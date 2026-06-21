"""System information and diagnostics commands for the DexImitate umbrella CLI."""

import json
import sys
import time
from typing import Any

import rich_click as click
import zmq
from dexim.cli.common import (
    get_console,
    handle_cli_error,
    make_table,
    print_banner,
    status_badge,
)
from dexim.cli.common.constants import CLI_VERSION
from dexim.core.messages import STATUS_PULL_ENDPOINT, StatusInfo


@click.group(name="system")
def system_group() -> None:
    """System information and diagnostics."""


@system_group.command()
@handle_cli_error
def info() -> None:
    """Show installed dexim packages and their versions."""
    console = get_console()
    print_banner(version=CLI_VERSION)

    packages = _detect_packages()
    table = make_table(
        title="Installed Packages",
        columns=[
            ("Package", "key"),
            ("Version", "value"),
            ("Status", ""),
        ],
        rows=[
            [str(p["name"]), str(p["version"]), status_badge(bool(p["installed"]))]
            for p in packages
        ],
    )
    console.print(table)


@system_group.command()
@handle_cli_error
def doctor() -> None:
    """Run diagnostics on the runtime environment."""
    console = get_console()
    console.print("[info]Running system diagnostics…[/]\n")

    checks = _run_diagnostics()
    for check in checks:
        icon = "[success]✓[/]" if check["ok"] else "[error]✗[/]"
        console.print(f"  {icon}  {check['label']}")
    console.print()

    all_ok = all(c["ok"] for c in checks)
    if all_ok:
        console.print("[success]All checks passed.[/]")
    else:
        failed = sum(1 for c in checks if not c["ok"])
        console.print(f"[warning]{failed} check(s) failed.[/]")


@system_group.command()
@click.option(
    "--endpoint",
    default=STATUS_PULL_ENDPOINT,
    show_default=True,
    help="ZMQ status endpoint to query.",
)
@click.option(
    "--timeout",
    default=2.0,
    show_default=True,
    help="Seconds to wait for status messages.",
)
@click.option(
    "--watch",
    is_flag=True,
    default=False,
    help="Continuously refresh status (Ctrl-C to exit).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output status as JSON.",
)
@handle_cli_error
def status(
    endpoint: str,
    timeout: float,
    watch: bool,
    as_json: bool,
) -> None:
    """Query and display live node status."""
    console = get_console()

    if as_json:
        _status_json(endpoint, timeout)
        return

    if watch:
        _status_watch(endpoint)
        return

    _status_one_shot(endpoint, timeout)


# ---------------------------------------------------------------------------
# Internal: package detection
# ---------------------------------------------------------------------------

_KNOWN_PACKAGES = [
    "dexim-manus-brainco",
    "dexim-core",
    "dexim-cli-common",
    "dexim-manus",
    "dexim-brainco",
    "dexim-realsense",
    "dexim-recorder",
    "dexim-visualizer",
]


def _detect_packages() -> list[dict[str, Any]]:
    """Detect installed dexim packages and their versions."""
    results: list[dict[str, Any]] = []
    for pkg in _KNOWN_PACKAGES:
        try:
            mod = __import__(pkg.replace("-", "_"), fromlist=["__version__"])
            version = getattr(mod, "__version__", "?")
            results.append({"name": pkg, "version": version, "installed": True})
        except ImportError:
            results.append({"name": pkg, "version": "-", "installed": False})
    return results


# ---------------------------------------------------------------------------
# Internal: diagnostics
# ---------------------------------------------------------------------------


def _run_diagnostics() -> list[dict[str, Any]]:
    """Run a battery of diagnostic checks."""
    checks: list[dict[str, Any]] = []

    # Python version
    py_ver = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    checks.append(
        {
            "label": f"Python {py_ver}",
            "ok": sys.version_info >= (3, 10),
        }
    )

    # ZMQ available
    try:
        import zmq as _zmq  # noqa: F811

        checks.append({"label": "ZMQ available", "ok": True})
    except ImportError:
        checks.append({"label": "ZMQ available", "ok": False})

    # Core packages
    for pkg_name, label in [
        ("dexim.core", "dexim-core"),
        ("dexim.cli.common", "dexim-cli-common"),
    ]:
        try:
            __import__(pkg_name)
            checks.append({"label": f"{label} installed", "ok": True})
        except ImportError:
            checks.append({"label": f"{label} installed", "ok": False})

    return checks


# ---------------------------------------------------------------------------
# Internal: status display
# ---------------------------------------------------------------------------


def _status_one_shot(endpoint: str, timeout: float) -> None:
    """Collect and display a single snapshot of node status."""
    console = get_console()
    msgs = _collect_status_messages(endpoint, timeout)

    if not msgs:
        console.print("[warning]No status messages received.[/]")
        return

    _display_status_table(msgs)


def _status_watch(endpoint: str) -> None:
    """Continuously display node status until interrupted."""
    console = get_console()
    console.print("[info]Watching node status (Ctrl-C to stop)...[/]\n")
    try:
        while True:
            msgs = _collect_status_messages(endpoint, 1.0)
            if msgs:
                _display_status_table(msgs)
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[info]Stopped.[/]")


def _status_json(endpoint: str, timeout: float) -> None:
    """Output status as JSON."""
    msgs = _collect_status_messages(endpoint, timeout)
    print(json.dumps(msgs, indent=2))


def _collect_status_messages(
    endpoint: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Collect status messages from the status plane.

    Args:
        endpoint: ZMQ PULL endpoint to connect to.
        timeout: Maximum seconds to wait.

    Returns:
        List of status message dicts.
    """
    messages: list[dict[str, Any]] = []
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.RCVTIMEO, int(timeout * 1000))
    sock.setsockopt(zmq.LINGER, 0)

    try:
        sock.connect(endpoint)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload = sock.recv(flags=zmq.NOBLOCK)
                msg = json.loads(payload.decode("utf-8"))
                messages.append(msg)
            except zmq.ZMQError:
                break
    except Exception:
        pass
    finally:
        sock.close()

    return messages


def _display_status_table(messages: list[dict[str, Any]]) -> None:
    """Display status messages as a rich table."""
    console = get_console()

    rows: list[list[str]] = []
    for msg in messages:
        node_id = msg.get("node_id", "?")
        status = msg.get("status", "UNKNOWN")
        rows.append([node_id, status_badge(True), status])

    table = make_table(
        title="Node Status",
        columns=[
            ("Node", "key"),
            ("Healthy", ""),
            ("Status", "value"),
        ],
        rows=rows,
    )
    console.print(table)
