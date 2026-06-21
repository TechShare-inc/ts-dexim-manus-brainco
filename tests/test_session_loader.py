"""Tests for ``dexim_manus_brainco.cli.session_loader``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dexim_manus_brainco.cli.session_loader import (
    DEXIM_BRAINCO_PORT_ENV,
    DEXIM_CONFIG_DIR_ENV,
    SessionPlan,
    _derive_expected_node_id,
    get_config_dir,
    list_sessions,
    load_device_registry,
    load_session,
    resolve_device,
)

# ---------------------------------------------------------------------------
# get_config_dir
# ---------------------------------------------------------------------------


class TestGetConfigDir:
    def test_default(self) -> None:
        result = get_config_dir()
        assert result.name == "config"

    def test_explicit(self, tmp_path: Path) -> None:
        result = get_config_dir(explicit=str(tmp_path / "myconfig"))
        assert result.name == "myconfig"
        assert result == (tmp_path / "myconfig").resolve()

    def test_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = str(tmp_path / "envconfig")
        monkeypatch.setenv(DEXIM_CONFIG_DIR_ENV, custom)
        result = get_config_dir()
        assert result.name == "envconfig"

    def test_explicit_beats_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DEXIM_CONFIG_DIR_ENV, str(tmp_path / "env"))
        result = get_config_dir(explicit=str(tmp_path / "explicit"))
        assert result.name == "explicit"


# ---------------------------------------------------------------------------
# load_device_registry
# ---------------------------------------------------------------------------


class TestLoadDeviceRegistry:
    def test_loads_valid_registry(self, temp_config_dir: Path) -> None:
        registry = load_device_registry(config_dir=str(temp_config_dir))
        assert "manus" in registry
        assert registry["manus"].package == "dexim-manus"
        assert registry["brainco_left"].package == "dexim-brainco"

    def test_missing_registry_file(self, temp_config_dir_no_registry: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Device registry not found"):
            load_device_registry(config_dir=str(temp_config_dir_no_registry))

    def test_empty_registry(self, tmp_path: Path) -> None:
        dexim = tmp_path / "dexim"
        dexim.mkdir()
        (dexim / "devices.yaml").write_text("")
        registry = load_device_registry(config_dir=str(tmp_path))
        assert registry == {}


# ---------------------------------------------------------------------------
# resolve_device
# ---------------------------------------------------------------------------


class TestResolveDevice:
    def test_resolves_existing(self, temp_config_dir: Path) -> None:
        entry = resolve_device("manus", config_dir=str(temp_config_dir))
        assert entry.name == "manus"
        assert entry.package == "dexim-manus"

    def test_raises_keyerror_for_missing(self, temp_config_dir: Path) -> None:
        with pytest.raises(KeyError, match="Device 'nonexistent' not found"):
            resolve_device("nonexistent", config_dir=str(temp_config_dir))


# ---------------------------------------------------------------------------
# _derive_expected_node_id
# ---------------------------------------------------------------------------


class TestDeriveExpectedNodeId:
    def test_manus(self) -> None:
        assert _derive_expected_node_id("dexim-manus", "manus") == "manus"

    def test_brainco_left(self) -> None:
        assert (
            _derive_expected_node_id("dexim-brainco", "brainco_left") == "brainco_left"
        )

    def test_brainco_right(self) -> None:
        assert (
            _derive_expected_node_id("dexim-brainco", "brainco_right")
            == "brainco_right"
        )

    def test_brainco_mock_left(self) -> None:
        # "left" in device_name → brainco_left
        assert (
            _derive_expected_node_id("dexim-brainco", "brainco_left_mock")
            == "brainco_left"
        )

    def test_recorder(self) -> None:
        assert (
            _derive_expected_node_id("dexim-recorder", "recorder_2cam")
            == "recorder_2cam"
        )

    def test_realsense(self) -> None:
        assert (
            _derive_expected_node_id("dexim-realsense", "my_realsense")
            == "my_realsense"
        )


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_lists_sessions(self, temp_config_dir: Path) -> None:
        sessions = list_sessions(config_dir=str(temp_config_dir))
        assert "single-hand-teleop-mock" in sessions
        assert "single-hand-teleop" in sessions
        assert "bimanual-teleop-mock" in sessions

    def test_empty_if_no_dir(self, tmp_path: Path) -> None:
        sessions = list_sessions(config_dir=str(tmp_path))
        assert sessions == []


# ---------------------------------------------------------------------------
# load_session
# ---------------------------------------------------------------------------


class TestLoadSession:
    def test_loads_single_hand_mock(self, temp_config_dir: Path) -> None:
        plan = load_session("single-hand-teleop-mock", config_dir=str(temp_config_dir))
        assert plan.name == "single-hand-teleop-mock"
        assert len(plan.nodes) == 2
        assert plan.nodes[0].device_name == "manus"
        assert plan.nodes[0].priority == 0
        assert plan.nodes[1].device_name == "brainco_left_mock"
        assert plan.nodes[1].priority == 100

    def test_loads_single_hand_hw(self, temp_config_dir: Path) -> None:
        plan = load_session("single-hand-teleop", config_dir=str(temp_config_dir))
        assert plan.name == "single-hand-teleop"
        assert len(plan.nodes) == 2
        assert plan.nodes[1].device_name == "brainco_left"

    def test_missing_session_raises(self, temp_config_dir: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Session 'nonexistent'"):
            load_session("nonexistent", config_dir=str(temp_config_dir))

    def test_ports_are_populated(self, temp_config_dir: Path) -> None:
        """Verify NodeSpec.port_override is None by default and set via env."""
        plan = load_session("single-hand-teleop", config_dir=str(temp_config_dir))
        for node in plan.nodes:
            assert node.port_override is None

    def test_brainco_port_env_var(
        self, temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DEXIM_BRAINCO_PORT_ENV, "COM9")
        plan = load_session("single-hand-teleop", config_dir=str(temp_config_dir))
        # Only brainco nodes get the port override
        brainco_nodes = [n for n in plan.nodes if n.package == "dexim-brainco"]
        assert len(brainco_nodes) == 1
        assert brainco_nodes[0].port_override == "COM9"
        # Manus node should NOT have port override
        manus_nodes = [n for n in plan.nodes if n.package == "dexim-manus"]
        assert manus_nodes[0].port_override is None


# ---------------------------------------------------------------------------
# SessionPlan.validate
# ---------------------------------------------------------------------------


class TestSessionPlanValidate:
    def test_valid_data_flow_passes(self, temp_config_dir: Path) -> None:
        plan = load_session("single-hand-teleop-mock", config_dir=str(temp_config_dir))
        plan.validate()  # Should not raise

    def test_bad_subscriber_raises(self, temp_config_dir: Path) -> None:
        # load_session() auto-validates data_flow — bad subscriber raises immediately
        with pytest.raises(ValueError, match="nonexistent_device"):
            load_session("bad-subscriber", config_dir=str(temp_config_dir))

    def test_empty_data_flow_passes(self, temp_config_dir: Path) -> None:
        """Session without data_flow section should pass validation."""
        plan = load_session("single-hand-teleop", config_dir=str(temp_config_dir))
        plan.validate()  # Should not raise
