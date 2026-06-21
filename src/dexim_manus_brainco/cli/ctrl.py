"""Control-plane interactive session for the DexImitate umbrella CLI.

Starts a persistent TUI that lets users select and send lifecycle and
recording commands to connected nodes via a ZMQ PUB socket.

Usage::

    dexim ctrl
    dexim ctrl --endpoint tcp://*:5550
"""

from __future__ import annotations

import time

import rich_click as click
import zmq
from dexim.cli.common import get_console, handle_cli_error
from dexim.core.messages import CTRL_PUB_ENDPOINT

# Derive the bind address from the connect address (localhost → *)
_BIND_ENDPOINT = CTRL_PUB_ENDPOINT.replace("localhost", "*")

# How long to wait after binding before the first send.
# ZMQ SUB sockets reconnect with a base interval of 100 ms (RECONNECT_IVL),
# so 300 ms covers at least two reconnect cycles, making delivery reliable.
_PUB_WARMUP_S = 0.30
# Interval between repeated sends, in seconds.
_PUB_REPEAT_INTERVAL_S = 0.10
# Number of times to send each command.  All ManagedNode control commands are
# idempotent, so retransmitting is safe and ensures late-connecting subscribers
# receive the message.
_PUB_REPEAT_COUNT = 3

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _pub_send(endpoint: str, frames: list[bytes]) -> None:
    """Bind a PUB socket, send frames repeatedly, then tear down.

    Sends the message ``_PUB_REPEAT_COUNT`` times with ``_PUB_REPEAT_INTERVAL_S``
    gaps to work around the ZMQ slow-joiner problem: SUB sockets that are still
    completing their TCP reconnect during the warmup period will catch a later
    repeat rather than missing the message entirely.

    Args:
        endpoint: ZMQ bind endpoint (e.g. ``tcp://*:5550``).
        frames: Multipart message frames to publish.

    Raises:
        SystemExit: On ZMQ error.
    """
    console = get_console()
    ctx = zmq.Context.instance()
    pub: zmq.Socket = ctx.socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    try:
        pub.bind(endpoint)
        # Wait for subscriber TCP connections to complete.
        time.sleep(_PUB_WARMUP_S)
        for _ in range(_PUB_REPEAT_COUNT):
            pub.send_multipart(frames, flags=zmq.DONTWAIT)
            time.sleep(_PUB_REPEAT_INTERVAL_S)
    except zmq.ZMQError as exc:
        console.print(f"[error]ZMQ error: {exc}[/]")
        raise SystemExit(1) from exc
    finally:
        pub.close()


@click.command(name="ctrl", context_settings=CONTEXT_SETTINGS)
@click.option(
    "--endpoint",
    default=_BIND_ENDPOINT,
    show_default=True,
    help="ZMQ bind endpoint for the control-plane PUB socket.",
)
@handle_cli_error
def ctrl_command(endpoint: str) -> None:
    """Open an interactive control-plane session.

    Arrow keys select a command; Enter sends it to all connected nodes.
    Press q or Esc to exit.
    """
    from .ctrl_ui import run_ctrl_session

    run_ctrl_session(endpoint, _pub_send)
