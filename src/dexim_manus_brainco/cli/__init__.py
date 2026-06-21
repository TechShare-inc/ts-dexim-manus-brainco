"""Umbrella CLI entry point - mounts all subpackage command groups.

Usage::

    dexim --help
    dexim system info
    dexim system doctor
    dexim launch --session single-hand-teleop-mock
    dexim brainco run --mode sim
    dexim manus monitor
"""

from importlib import import_module

import rich_click as click
from dexim.cli.common import configure_logging, setup_error_handling

from dexim_manus_brainco import __version__

# rich-click visual configuration
click.rich_click.COMMAND_GROUPS = {
    "dexim": [
        {"name": "Dexterous Hand", "commands": ["brainco"]},
        {"name": "Input Device", "commands": ["manus"]},
        {"name": "Sensor Device", "commands": ["realsense"]},
        {"name": "Data Recording", "commands": ["recorder"]},
        {"name": "Session", "commands": ["launch", "viz"]},
        {"name": "Control Plane", "commands": ["ctrl"]},
        {"name": "System", "commands": ["system"]},
    ]
}
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_COMMANDS_TABLE_COLUMN_WIDTH_RATIO = (1, 3)

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="dexim")
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        case_sensitive=False,
    ),
    default="INFO",
    show_default=True,
    envvar="DEXIM_LOG_LEVEL",
    help="Logging verbosity.",
)
def main(log_level: str) -> None:
    """DexImitate - Real-time Manus-to-BrainCo Teleoperation Framework.

    \b
    Command categories:
      launch    Start a multi-node session from a session config
      brainco   BrainCo Revo2 dexterous hand control
      manus     Manus glove input device
      realsense RealSense camera input device
      recorder  Data recording and episode management
      ctrl      Interactive control-plane command sender
      system    System information and diagnostics
    """
    setup_error_handling()
    configure_logging(log_level)


def _register_subcommands() -> None:
    """Lazy-mount subpackage CLIs. Gracefully skip missing packages."""
    mounts = [
        ("brainco", "dexim.brainco.cli", "brainco_group"),
        ("manus", "dexim.manus.cli", "manus_group"),
        ("realsense", "dexim.realsense.cli", "realsense_group"),
        ("recorder", "dexim.recorder.cli", "recorder_group"),
        ("viz", "dexim.visualizer.cli", "visualizer_command"),
    ]
    for name, module_path, attr in mounts:
        try:
            mod = import_module(module_path)
            group = getattr(mod, attr)
            main.add_command(group, name=name)
        except ImportError:
            pass  # Package CLI not yet implemented; skip silently


_register_subcommands()

from .ctrl import ctrl_command  # noqa: E402
from .launch_command import launch_command  # noqa: E402
from .system import system_group  # noqa: E402  (must come after main is defined)

main.add_command(ctrl_command, name="ctrl")
main.add_command(launch_command, name="launch")
main.add_command(system_group, name="system")
