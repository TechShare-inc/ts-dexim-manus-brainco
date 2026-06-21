"""Control-plane broadcast bus.

Binds a ZMQ PUB socket and broadcasts control commands to all nodes
with configurable repeat count for reliability.
"""

from __future__ import annotations

import time
from typing import Any

import zmq
from dexim.core.messages import CTRL_PUB_ENDPOINT, TOPIC_CTRL


class ControlBus:
    """Broadcasts control commands to all nodes via ZMQ PUB.

    Args:
        endpoint: ZMQ endpoint to bind (default: ``tcp://0.0.0.0:5550``).
        warmup_s: Seconds to wait after binding for SUB sockets to connect.
        repeat_count: Number of times to repeat each command (default: 3).
        repeat_interval_s: Seconds between repeats (default: 0.10).
    """

    def __init__(
        self,
        endpoint: str = CTRL_PUB_ENDPOINT,
        warmup_s: float = 0.30,
        repeat_count: int = 3,
        repeat_interval_s: float = 0.10,
    ) -> None:
        self._endpoint = endpoint
        self._warmup_s = warmup_s
        self._repeat_count = repeat_count
        self._repeat_interval_s = repeat_interval_s

        self._ctx: zmq.Context | None = None
        self._pub: zmq.Socket | None = None

        self._init()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, command: str, payload: bytes | None = None) -> None:
        """Broadcast a control command to all nodes.

        Args:
            command: Control command string (e.g. ``"START"``).
            payload: Optional payload frame (used by ``SET_TASK``).
        """
        if self._pub is None:
            return

        frames: list[bytes] = [TOPIC_CTRL, command.encode("utf-8")]
        if payload is not None:
            frames.append(payload)

        for _ in range(self._repeat_count):
            try:
                self._pub.send_multipart(frames, flags=zmq.DONTWAIT)
            except zmq.ZMQError:
                pass
            time.sleep(self._repeat_interval_s)

    def close(self) -> None:
        """Close the ZMQ PUB socket."""
        if self._pub is not None:
            try:
                self._pub.close(linger=0)
            except Exception:
                pass
        self._pub = None
        self._ctx = None

    # ------------------------------------------------------------------
    # Emit event callback (set by orchestrator)
    # ------------------------------------------------------------------

    #: Optional callback for structured log events.
    #: Signature: ``fn(level: str, msg: str, node: str = "")``
    on_event: Any = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init(self) -> None:
        """Create and bind the ZMQ PUB socket."""
        self._ctx = zmq.Context.instance()

        self._pub = self._ctx.socket(zmq.PUB)
        self._pub.setsockopt(zmq.SNDHWM, 100)

        bind_addr = self._endpoint.replace("localhost", "0.0.0.0")
        if bind_addr.startswith("tcp://*"):
            bind_addr = bind_addr.replace("*", "0.0.0.0")
        self._pub.bind(bind_addr)
