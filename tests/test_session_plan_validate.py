"""Tests for ``SessionPlan.validate()`` data_flow validation."""

from __future__ import annotations

import pytest

from dexim_manus_brainco.cli.session_loader import NodeSpec, SessionPlan

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------


def _make_plan(
    node_names: list[str],
    data_flow: dict | None = None,
) -> SessionPlan:
    """Build a minimal SessionPlan for validation testing."""
    nodes = [
        NodeSpec(
            device_name=name,
            package=f"dexim-{name}",
            config_name=f"{name}/default",
            config_path=f"/config/{name}.yaml",  # type: ignore[arg-type]
            expected_node_id=name,
        )
        for name in node_names
    ]
    return SessionPlan(
        name="test-session",
        nodes=nodes,
        data_flow=data_flow or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidate:
    def test_empty_data_flow_passes(self) -> None:
        plan = _make_plan(["manus", "brainco_left"])
        plan.validate()  # Should not raise

    def test_valid_subscriber_references(self) -> None:
        plan = _make_plan(
            ["manus", "brainco_left", "recorder_2cam"],
            data_flow={
                "manus": {
                    "publishes": ["tracker_poses"],
                    "subscribers": ["brainco_left", "recorder_2cam"],
                },
                "brainco_left": {
                    "publishes": ["joint_states"],
                    "subscribers": ["recorder_2cam"],
                },
            },
        )
        plan.validate()  # Should not raise

    def test_invalid_subscriber_raises(self) -> None:
        plan = _make_plan(
            ["manus", "brainco_left"],
            data_flow={
                "manus": {
                    "publishes": ["tracker_poses"],
                    "subscribers": ["nonexistent_device"],
                },
            },
        )
        with pytest.raises(ValueError, match="nonexistent_device"):
            plan.validate()

    def test_non_dict_flow_is_skipped(self) -> None:
        """Non-dict flow entries should be skipped gracefully."""
        plan = _make_plan(
            ["manus"],
            data_flow={
                "manus": "not-a-dict",  # type: ignore[dict-item]
            },
        )
        plan.validate()  # Should not raise

    def test_non_list_subscribers_is_skipped(self) -> None:
        """Non-list subscribers should be skipped gracefully."""
        plan = _make_plan(
            ["manus"],
            data_flow={
                "manus": {
                    "publishes": ["poses"],
                    "subscribers": "not-a-list",
                },
            },
        )
        plan.validate()  # Should not raise

    def test_single_node_passes(self) -> None:
        plan = _make_plan(
            ["manus"],
            data_flow={
                "manus": {"publishes": [], "subscribers": []},
            },
        )
        plan.validate()  # Should not raise
