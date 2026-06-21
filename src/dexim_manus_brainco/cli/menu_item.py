"""Shared ``MenuItem`` dataclass for interactive TUI menus.

Used by both ``ctrl_ui.py`` (control-plane command sender) and
``launch_ui.py`` (launch session TUI).  Provides a uniform structure
for menu items with optional danger confirmation and text-input prompts.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sentinel for the free-text raw-command entry row.
_RAW_CMD_SENTINEL = "__RAW__"


@dataclass
class MenuItem:
    """One row in an interactive command menu.

    Attributes:
        label: Short display name shown in the menu.
        cmd: The command string to send, or ``_RAW_CMD_SENTINEL`` for
            the free-text raw entry row.
        description: One-line description shown alongside the label
            (used in ``ctrl_ui``).  Defaults to empty.
        section: Menu section name for grouping (``"Teleop"`` or
            ``"Raw"``).  Ignored by ``ctrl_ui`` which has no sections.
        is_dangerous: If ``True``, require confirmation before sending.
        needs_text_input: If ``True``, pause the TUI to prompt for text
            input before sending (used for SET_TASK and raw commands).
    """

    label: str
    cmd: str
    description: str = ""
    section: str = "Teleop"
    is_dangerous: bool = False
    needs_text_input: bool = False
