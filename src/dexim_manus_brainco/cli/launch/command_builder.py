"""Generic command-line builder for node subprocesses.

Constructs ``dexim <subcommand> run --config <path>`` commands from
resolved ``NodeSpec`` instances.  No per-package special cases --
every package is launched the same way.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..session_loader import NodeSpec

# ---------------------------------------------------------------------------
# Package-to-subcommand mapping
# ---------------------------------------------------------------------------

_PKG_TO_SUBCOMMAND: dict[str, str] = {
    "dexim-manus": "manus",
    "dexim-brainco": "brainco",
    "dexim-realsense": "realsense",
    "dexim-recorder": "recorder",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_dexim() -> list[str]:
    """Locate the ``dexim`` CLI entry point.

    Returns:
        Command prefix as a list of strings.
    """
    dexim_path = shutil.which("dexim")
    if dexim_path:
        return ["dexim"]
    return [sys.executable, "-m", "dexim_manus_brainco.cli"]


def _resolve_subcommand(package: str) -> str:
    """Map a pip package name to the CLI subcommand name.

    Args:
        package: e.g. ``"dexim-brainco"``.

    Returns:
        Subcommand name, e.g. ``"brainco"``.

    Raises:
        ValueError: If the package is not known.
    """
    sub = _PKG_TO_SUBCOMMAND.get(package)
    if sub is not None:
        return sub
    raise ValueError(
        f"Unknown package '{package}'.  Known packages: "
        f"{', '.join(sorted(_PKG_TO_SUBCOMMAND))}"
    )


# ---------------------------------------------------------------------------
# CommandBuilder
# ---------------------------------------------------------------------------


class CommandBuilder:
    """Constructs subprocess command-lines for node launching.

    Every node is launched with the same pattern::

        dexim <subcommand> run --config <absolute_config_path>

    There are no per-package special cases.  Each package's CLI ``run``
    command must accept ``--config`` pointing to its YAML config file.
    """

    def __init__(self) -> None:
        self._dexim_prefix: list[str] = _find_dexim()

    def build(self, spec: NodeSpec) -> list[str]:
        """Build the subprocess command-line for a node specification.

        Args:
            spec: The resolved node specification.

        Returns:
            List of command-line tokens.

        Raises:
            ValueError: If the package is not recognised.
        """
        subcommand = _resolve_subcommand(spec.package)

        cmd: list[str] = [
            *self._dexim_prefix,
            subcommand,
            "run",
            "--config",
            str(spec.config_path),
        ]

        # Inject COM port override for brainco devices
        if spec.port_override is not None:
            cmd.extend(["--port", spec.port_override])

        return cmd
