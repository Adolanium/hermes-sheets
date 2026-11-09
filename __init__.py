"""Sheets: bring your sheet, and Hermes tells you what it means.

The Desktop page (desktop/plugin.js) is the spreadsheet. This package lets the agent work with it
from any chat:

- ``sheets_list`` / ``sheets_read``: read the person's grids (the page shares a copy while chat access
  is on; Run settings > Chats can read my grids);
- ``sheets_propose``: send proposed cell changes, new columns and new rows to a grid. They wait in
  Sheets until the person reviews them; nothing is applied without a click, and applied sets can be
  restored;
- ``sheets_create``: open a new grid from data the agent gathered;
- the ``sheets:grids`` skill: how to work with a person's sheet without overstepping.
"""

from pathlib import Path

_SKILL = Path(__file__).resolve().parent / "skills" / "grids" / "SKILL.md"


def register(ctx):
    from .tools import register_tools

    register_tools(ctx)
    ctx.register_skill(
        "grids",
        _SKILL,
        description="Work with the person's Sheets spreadsheets from a chat: read a grid, answer questions "
                    "about it, start a new grid, and propose columns, rows or cell fixes for them to review.",
    )
