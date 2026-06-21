"""Tests for ``dexim_manus_brainco.cli.launch.command_builder``."""

from __future__ import annotations

from pathlib import Path

import pytest

from dexim_manus_brainco.cli.launch.command_builder import (
    CommandBuilder,
    _resolve_subcommand,
)
from dexim_manus_brainco.cli.session_loader import NodeSpec

# ---------------------------------------------------------------------------
# _resolve_subcommand
# ---------------------------------------------------------------------------


class TestResolveSubcommand:
    def test_known_packages(self) -> None:
        assert _resolve_subcommand("dexim-manus") == "manus"
        assert _resolve_subcommand("dexim-brainco") == "brainco"
        assert _resolve_subcommand("dexim-realsense") == "realsense"
        assert _resolve_subcommand("dexim-recorder") == "recorder"

    def test_unknown_package_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown package"):
            _resolve_subcommand("dexim-unknown")


# ---------------------------------------------------------------------------
# CommandBuilder.build
# ---------------------------------------------------------------------------


def _make_spec(
    device_name: str = "manus",
    package: str = "dexim-manus",
    config_name: str = "manus/default",
    config_path: Path | None = None,
    port_override: str | None = None,
) -> NodeSpec:
    if config_path is None:
        config_path = Path("/config/dexim/manus/default.yaml")
    return NodeSpec(
        device_name=device_name,
        package=package,
        config_name=config_name,
        config_path=config_path,
        expected_node_id=device_name,
        port_override=port_override,
    )


class TestCommandBuilder:
    def test_build_manus_command(self) -> None:
        builder = CommandBuilder()
        cmd = builder.build(_make_spec(package="dexim-manus"))
        assert "manus" in cmd
        assert "run" in cmd
        assert "--config" in cmd

    def test_build_brainco_command(self) -> None:
        builder = CommandBuilder()
        spec = _make_spec(
            device_name="brainco_left",
            package="dexim-brainco",
            config_name="brainco/hw-left",
            config_path=Path("/config/dexim/brainco/hw-left.yaml"),
        )
        cmd = builder.build(spec)
        assert "brainco" in cmd
        assert "run" in cmd
        assert "--config" in cmd
        # On Windows, str(Path(...)) uses backslashes
        config_idx = cmd.index("--config") + 1
        assert Path(cmd[config_idx]).name == "hw-left.yaml"

    def test_build_includes_config_path(self) -> None:
        builder = CommandBuilder()
        spec = _make_spec(config_path=Path("/custom/path.yaml"))
        cmd = builder.build(spec)
        idx = cmd.index("--config")
        assert cmd[idx + 1] == str(Path("/custom/path.yaml"))

    def test_build_unknown_package_raises(self) -> None:
        builder = CommandBuilder()
        spec = _make_spec(package="dexim-unknown")
        with pytest.raises(ValueError, match="Unknown package"):
            builder.build(spec)

    def test_build_with_port_override(self) -> None:
        builder = CommandBuilder()
        spec = _make_spec(
            device_name="brainco_left",
            package="dexim-brainco",
            port_override="COM9",
        )
        cmd = builder.build(spec)
        assert "--port" in cmd
        port_idx = cmd.index("--port")
        assert cmd[port_idx + 1] == "COM9"

    def test_build_without_port_override(self) -> None:
        builder = CommandBuilder()
        spec = _make_spec(package="dexim-manus", port_override=None)
        cmd = builder.build(spec)
        assert "--port" not in cmd
