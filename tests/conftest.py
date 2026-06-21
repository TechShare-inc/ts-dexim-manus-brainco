"""Pytest configuration and shared fixtures for dexim-manus-brainco tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Minimal YAML content for mock configs
# ---------------------------------------------------------------------------

_DEVICES_YAML = """
manus:
  package: dexim-manus
  config: manus/default

brainco_left:
  package: dexim-brainco
  config: brainco/hw-left

brainco_left_mock:
  package: dexim-brainco
  config: brainco/mock-left

recorder_2cam:
  package: dexim-recorder
  config: recorder/2-cam
"""

_MANUS_CONFIG = """
manus:
  name: manus
  zmq:
    pub_endpoint: tcp://*:5555
"""

_BRAINCO_HW_LEFT = """
brainco:
  name: brainco_left
  interface:
    mode: hw
    port: COM5
"""

_BRAINCO_MOCK_LEFT = """
brainco:
  name: brainco_left_mock
  interface:
    mode: mock
"""

_RECORDER_CONFIG = """
recorder:
  name: recorder_2cam
  cameras: 2
"""

_SINGLE_HAND_MOCK_SESSION = """
session:
  name: single-hand-teleop-mock
  description: Single hand teleop in mock mode

nodes:
  - device: manus
  - device: brainco_left_mock

data_flow:
  manus:
    publishes: [tracker_poses]
    subscribers: [brainco_left_mock]
  brainco_left_mock:
    publishes: [joint_states]
    subscribers: [manus]
"""

_SINGLE_HAND_HW_SESSION = """
session:
  name: single-hand-teleop
  description: Single hand teleop with hardware

nodes:
  - device: manus
  - device: brainco_left
"""

_BIMANUAL_MOCK_SESSION = """
session:
  name: bimanual-teleop-mock
  description: Bimanual teleop with mock hands

nodes:
  - device: manus
  - device: brainco_left_mock

data_flow:
  manus:
    publishes: [tracker_poses, skeleton_states]
    subscribers: [brainco_left_mock, recorder_2cam]
  brainco_left_mock:
    publishes: [joint_states]
    subscribers: [recorder_2cam]
  recorder_2cam:
    publishes: []
    subscribers: [manus, brainco_left_mock]
"""

_BAD_SUBSCRIBER_SESSION = """
session:
  name: bad-subscriber
  description: References a non-existent device

nodes:
  - device: manus
  - device: brainco_left_mock

data_flow:
  manus:
    publishes: [tracker_poses]
    subscribers: [nonexistent_device]
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config_structure(root: Path) -> None:
    """Write a complete minimal config directory tree under *root*.

    Creates:
      <root>/dexim/
        devices.yaml
        manus/default.yaml
        brainco/hw-left.yaml
        brainco/mock-left.yaml
        recorder/2-cam.yaml
        session/
          single-hand-teleop-mock.yaml
          single-hand-teleop.yaml
          bimanual-teleop-mock.yaml
          bad-subscriber.yaml
    """
    dexim = root / "dexim"
    dexim.mkdir(parents=True, exist_ok=True)
    (dexim / "manus").mkdir(exist_ok=True)
    (dexim / "brainco").mkdir(exist_ok=True)
    (dexim / "recorder").mkdir(exist_ok=True)
    (dexim / "session").mkdir(exist_ok=True)

    (dexim / "devices.yaml").write_text(_DEVICES_YAML)
    (dexim / "manus" / "default.yaml").write_text(_MANUS_CONFIG)
    (dexim / "brainco" / "hw-left.yaml").write_text(_BRAINCO_HW_LEFT)
    (dexim / "brainco" / "mock-left.yaml").write_text(_BRAINCO_MOCK_LEFT)
    (dexim / "recorder" / "2-cam.yaml").write_text(_RECORDER_CONFIG)
    (dexim / "session" / "single-hand-teleop-mock.yaml").write_text(
        _SINGLE_HAND_MOCK_SESSION
    )
    (dexim / "session" / "single-hand-teleop.yaml").write_text(_SINGLE_HAND_HW_SESSION)
    (dexim / "session" / "bimanual-teleop-mock.yaml").write_text(_BIMANUAL_MOCK_SESSION)
    (dexim / "session" / "bad-subscriber.yaml").write_text(_BAD_SUBSCRIBER_SESSION)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_config_dir() -> Path:
    """Create a temporary config directory with a complete mock config tree.

    Returns:
        Absolute path to the temp directory root.
    """
    with tempfile.TemporaryDirectory(prefix="dexim_test_") as tmp:
        root = Path(tmp)
        _write_config_structure(root)
        yield root


@pytest.fixture
def temp_config_dir_no_registry() -> Path:
    """Temp config dir with NO devices.yaml (for error-path testing)."""
    with tempfile.TemporaryDirectory(prefix="dexim_test_") as tmp:
        root = Path(tmp)
        dexim = root / "dexim"
        dexim.mkdir()
        (dexim / "session").mkdir()
        (dexim / "session" / "single-hand-teleop-mock.yaml").write_text(
            _SINGLE_HAND_MOCK_SESSION
        )
        yield root
