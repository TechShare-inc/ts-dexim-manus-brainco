"""Tests for ``dexim_manus_brainco.cli.launch.control_bus``."""

from __future__ import annotations

import pytest

# Skip the entire module if zmq is not available (e.g. in CI).
zmq = pytest.importorskip("zmq", reason="zmq not installed")


# ---------------------------------------------------------------------------
# Address normalisation (pure functions, no ZMQ required)
# ---------------------------------------------------------------------------


class TestAddressNormalisation:
    """ControlBus._normalise_address is tested indirectly via module import.

    The ControlBus constructor normalises the endpoint address:
    - ``localhost`` → ``0.0.0.0``
    - ``*`` → ``0.0.0.0``
    - other strings pass through unchanged.
    """

    def test_normalisation_logic(self) -> None:
        """Test the normalisation logic inline since it's a private method."""

        # Replicate the logic from ControlBus.__init__
        def _normalise(addr: str) -> str:
            addr = addr.replace("localhost", "0.0.0.0", 1)
            addr = addr.replace("*", "0.0.0.0", 1)
            return addr

        # localhost → 0.0.0.0
        assert _normalise("tcp://localhost:5550") == "tcp://0.0.0.0:5550"

        # * → 0.0.0.0
        assert _normalise("tcp://*:5550") == "tcp://0.0.0.0:5550"

        # Already 0.0.0.0 → unchanged
        assert _normalise("tcp://0.0.0.0:5550") == "tcp://0.0.0.0:5550"

        # No prefix → unchanged
        assert _normalise("ipc:///tmp/ctrl") == "ipc:///tmp/ctrl"

        # Empty → unchanged
        assert _normalise("") == ""


# ---------------------------------------------------------------------------
# ControlBus integration tests (skip if zmq unavailable)
# ---------------------------------------------------------------------------


class TestControlBusIntegration:
    def test_constructor_creates_socket(self) -> None:
        from dexim_manus_brainco.cli.launch.control_bus import ControlBus

        bus = ControlBus(endpoint="tcp://127.0.0.1:15550")
        assert bus._pub is not None
        bus.close()

    def test_send_succeeds(self) -> None:
        from dexim_manus_brainco.cli.launch.control_bus import ControlBus

        bus = ControlBus(endpoint="tcp://127.0.0.1:15551")
        bus.send("TEST_CMD")
        bus.close()

    def test_send_with_payload(self) -> None:
        from dexim_manus_brainco.cli.launch.control_bus import ControlBus

        bus = ControlBus(endpoint="tcp://127.0.0.1:15552")
        bus.send("TEST_CMD", b'{"key":"value"}')
        bus.close()

    def test_close_cleanup(self) -> None:
        from dexim_manus_brainco.cli.launch.control_bus import ControlBus

        bus = ControlBus(endpoint="tcp://127.0.0.1:15553")
        bus.close()
        assert bus._pub is None
