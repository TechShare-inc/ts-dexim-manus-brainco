"""Session configuration loader for the ``dexim launch`` command.

Parses session YAML files from ``config/dexim/session/``, resolves device
names through the shared device registry (``devices.yaml``), and produces
structured launch plans consumable by :class:`LaunchOrchestrator`.

Usage::

    from dexim_manus_brainco.cli.session_loader import load_session

    plan = load_session("single-hand-teleop-mock")
    for step in plan.nodes:
        print(step.device_name, step.package, step.config_name)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEXIM_CONFIG_DIR_ENV = "DEXIM_CONFIG_DIR"
DEXIM_BRAINCO_PORT_ENV = "DEXIM_BRAINCO_PORT"
_DEFAULT_CONFIG_DIR = "./config"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceEntry:
    """One entry from the shared device registry (``devices.yaml``).

    Attributes:
        name: Device key (e.g. ``"brainco_left_mock"``).
        package: pip package name (e.g. ``"dexim-brainco"``).
        config: Logical config name (e.g. ``"brainco/mock-left"``).
    """

    name: str
    package: str
    config: str


#: Default launch priority for non-input nodes.
_DEFAULT_NODE_PRIORITY: int = 100

#: Highest launch priority (manus / input nodes must come up first).
_INPUT_NODE_PRIORITY: int = 0


@dataclass(frozen=True)
class NodeSpec:
    """A single node to launch from a session definition.

    Attributes:
        device_name: Device key in the registry.
        package: Pip package name.
        config_name: Config path relative to the config root.
        config_path: Resolved absolute path to the YAML config file.
        expected_node_id: The ``node_id`` the node will use internally in
            ZMQ status messages (e.g. ``"brainco_left"``).  This is derived
            from the device name and package conventions.
        priority: Launch priority (lower = higher priority).  Input nodes
            (manus) get priority 0; other nodes default to 100.
    """

    device_name: str
    package: str
    config_name: str
    config_path: Path
    expected_node_id: str
    priority: int = _DEFAULT_NODE_PRIORITY
    port_override: str | None = None


@dataclass
class SessionPlan:
    """Fully resolved launch plan for one session.

    Attributes:
        name: Session name from the YAML.
        description: Human-readable session description.
        nodes: Ordered list of :class:`NodeSpec` to launch.
        data_flow: Raw data-flow mapping from the session YAML.
    """

    name: str
    description: str = ""
    nodes: list[NodeSpec] = field(default_factory=list)
    data_flow: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate that ``data_flow`` references only known devices.

        Checks that every subscriber listed in ``data_flow`` refers to a
        device present in ``self.nodes``.  Raises ``ValueError`` if any
        unknown device is referenced.

        Raises:
            ValueError: If a subscriber references a device that is not
                in the session's node list.
        """
        node_names = {node.device_name for node in self.nodes}

        for device_name, flow in self.data_flow.items():
            if not isinstance(flow, dict):
                continue
            subscribers = flow.get("subscribers", [])
            if not isinstance(subscribers, list):
                continue
            for sub in subscribers:
                if isinstance(sub, str) and sub not in node_names:
                    raise ValueError(
                        f"data_flow subscriber '{sub}' (referenced by "
                        f"'{device_name}') is not a device in this session. "
                        f"Known devices: {', '.join(sorted(node_names)) or '(none)'}"
                    )


# ---------------------------------------------------------------------------
# Config directory helpers
# ---------------------------------------------------------------------------


def get_config_dir(explicit: str | None = None) -> Path:
    """Resolve the config root directory.

    Precedence:
    1. *explicit* argument.
    2. ``DEXIM_CONFIG_DIR`` environment variable.
    3. ``./config`` relative to CWD.

    Args:
        explicit: Optional override path.

    Returns:
        Absolute resolved Path.
    """
    raw = explicit or os.environ.get(DEXIM_CONFIG_DIR_ENV) or _DEFAULT_CONFIG_DIR
    return Path(raw).expanduser().resolve()


def get_session_dir(config_dir: str | None = None) -> Path:
    """Return the directory containing session YAML files.

    Args:
        config_dir: Config root override.

    Returns:
        ``{config_dir}/dexim/session/`` as an absolute Path.
    """
    return get_config_dir(config_dir) / "dexim" / "session"


def get_devices_path(config_dir: str | None = None) -> Path:
    """Return the shared device registry path.

    Args:
        config_dir: Config root override.

    Returns:
        ``{config_dir}/dexim/devices.yaml`` as an absolute Path.
    """
    return get_config_dir(config_dir) / "dexim" / "devices.yaml"


# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------


def load_device_registry(config_dir: str | None = None) -> dict[str, DeviceEntry]:
    """Load the shared device registry (``devices.yaml``).

    Args:
        config_dir: Config root override.

    Returns:
        Mapping of device name to :class:`DeviceEntry`.

    Raises:
        FileNotFoundError: If ``devices.yaml`` does not exist.
        ValueError: If the file is malformed.
    """
    path = get_devices_path(config_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"Device registry not found: {path}\n"
            f"Create it at config/dexim/devices.yaml or set DEXIM_CONFIG_DIR."
        )

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Device registry must be a mapping: {path}")

    entries: dict[str, DeviceEntry] = {}
    for name, raw in data.items():
        if not isinstance(raw, dict):
            continue
        entries[name] = DeviceEntry(
            name=str(name),
            package=str(raw.get("package", "")),
            config=str(raw.get("config", "")),
        )
    return entries


def resolve_device(
    device_name: str,
    config_dir: str | None = None,
) -> DeviceEntry:
    """Look up a device in the registry.

    Args:
        device_name: Device key to look up.
        config_dir: Config root override.

    Returns:
        Matching :class:`DeviceEntry`.

    Raises:
        KeyError: If the device is not found.
    """
    registry = load_device_registry(config_dir)
    try:
        return registry[device_name]
    except KeyError as exc:
        available = ", ".join(sorted(registry.keys()))
        raise KeyError(
            f"Device '{device_name}' not found in {get_devices_path(config_dir)}.\n"
            f"Available devices: {available}"
        ) from exc


# ---------------------------------------------------------------------------
# Node ID derivation
# ---------------------------------------------------------------------------


def _derive_expected_node_id(package: str, device_name: str) -> str:
    """Derive the internal ``node_id`` a node will use in ZMQ status messages.

    Each package follows a convention for its internal node identifier:

    - ``dexim-manus``        → ``"manus"`` (always)
    - ``dexim-brainco``      → ``"brainco_left"`` / ``"brainco_right"``
    - ``dexim-realsense``    → ``device_name`` itself (matches config)
    - ``dexim-recorder``     → ``device_name`` itself (matches config)

    Args:
        package: Pip package name.
        device_name: Device key from the registry.

    Returns:
        The expected internal node_id string.
    """
    if package == "dexim-manus":
        return "manus"

    if package == "dexim-brainco":
        name_lower = device_name.lower()
        if "left" in name_lower:
            return "brainco_left"
        if "right" in name_lower:
            return "brainco_right"
        # Fallback: use device_name as-is
        return device_name

    # realsense, recorder, and any other packages use the device name
    return device_name


# ---------------------------------------------------------------------------
# Session loading
# ---------------------------------------------------------------------------


def list_sessions(config_dir: str | None = None) -> list[str]:
    """List available session names (without ``.yaml`` extension).

    Args:
        config_dir: Config root override.

    Returns:
        Sorted list of session names.
    """
    session_dir = get_session_dir(config_dir)
    if not session_dir.exists():
        return []
    return sorted(p.stem for p in session_dir.glob("*.yaml"))


def load_session(
    session_name: str,
    config_dir: str | None = None,
) -> SessionPlan:
    """Load and resolve a session configuration.

    Args:
        session_name: Session name (without ``.yaml`` extension).
        config_dir: Config root override.

    Returns:
        Fully resolved :class:`SessionPlan`.

    Raises:
        FileNotFoundError: If the session file or device registry is missing.
        KeyError: If a referenced device is not in the registry.
        ValueError: If the session YAML is malformed.
    """
    config_root = get_config_dir(config_dir)
    session_path = get_session_dir(config_dir) / f"{session_name}.yaml"

    if not session_path.exists():
        available = ", ".join(list_sessions(config_dir)) or "(none)"
        raise FileNotFoundError(
            f"Session '{session_name}' not found: {session_path}\n"
            f"Available sessions: {available}"
        )

    with session_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    session_meta = data.get("session", {})
    if not isinstance(session_meta, dict):
        raise ValueError(f"Invalid 'session' section in {session_path}")

    plan = SessionPlan(
        name=str(session_meta.get("name", session_name)),
        description=str(session_meta.get("description", "")),
        data_flow=data.get("data_flow", {}),
    )

    # Read port override for brainco devices
    brainco_port = os.environ.get(DEXIM_BRAINCO_PORT_ENV) or None

    # Resolve each node
    raw_nodes: list[dict[str, str]] = data.get("nodes", [])
    if not raw_nodes:
        raise ValueError(f"No nodes defined in session '{session_name}'")

    registry = load_device_registry(config_dir)

    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        device_name = str(raw.get("device", ""))
        if not device_name:
            continue

        entry = registry.get(device_name)
        if entry is None:
            available = ", ".join(sorted(registry.keys()))
            raise KeyError(
                f"Device '{device_name}' (referenced in session '{session_name}') "
                f"not found in {get_devices_path(config_dir)}.\n"
                f"Available devices: {available}"
            )

        config_path = config_root / "dexim" / f"{entry.config}.yaml"
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config for device '{device_name}' not found: {config_path}"
            )

        priority = (
            _INPUT_NODE_PRIORITY
            if entry.package == "dexim-manus"
            else _DEFAULT_NODE_PRIORITY
        )

        port_override = brainco_port if entry.package == "dexim-brainco" else None

        plan.nodes.append(
            NodeSpec(
                device_name=device_name,
                package=entry.package,
                config_name=entry.config,
                config_path=config_path.resolve(),
                expected_node_id=_derive_expected_node_id(entry.package, device_name),
                priority=priority,
                port_override=port_override,
            )
        )

    plan.validate()
    return plan
