"""Where the chat agent and the Sheets page meet, under ``<hermes home>/plugin-data/sheets/``.

- ``grids/<id>.json``: a copy of each grid that the Sheets page writes while it runs, so the agent
  can read a sheet from any chat. The page owns these files: it rewrites one after every save and
  deletes them all when the person turns chat access off.
- ``inbox/<id>.json``: what the agent sent the page, a new grid or a set of proposed changes. The
  page takes each entry and acknowledges it, which removes the file. Proposed changes then wait in
  Sheets until the person reviews them; nothing here writes to a grid.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

GRID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENTRY_ID = re.compile(r"^w[0-9a-f]{12}$")
_MAX_INBOX = 50


def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("sheets")
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        path = home / "plugin-data" / "sheets"
        path.mkdir(parents=True, exist_ok=True)
        return path


def hermes_home() -> Path:
    """The Hermes home folder this plugin's data lives in."""
    return data_dir().parent.parent


def _write_json(path: Path, value) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Grids:
    """The page's copies of its grids, plus whether the person lets chats see them."""

    def __init__(self, root: Path | None = None):
        self.dir = (root or data_dir()) / "grids"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.dir.parent / "sharing.json"

    # sharing -----------------------------------------------------------------------------------

    def sharing(self) -> dict:
        value = _read_json(self.settings_path)
        if not isinstance(value, dict):
            return {"known": False, "on": False}
        return {"known": True, "on": bool(value.get("on")), "at": value.get("at")}

    def set_sharing(self, on: bool) -> None:
        _write_json(self.settings_path, {"on": bool(on), "at": time.time()})
        if not on:
            self.prune([])

    # grids -------------------------------------------------------------------------------------

    def put(self, grid: dict) -> bool:
        gid = str(grid.get("id") or "")
        if not GRID_ID.match(gid):
            return False
        _write_json(self.dir / f"{gid}.json", grid)
        return True

    def get(self, gid: str) -> dict | None:
        if not GRID_ID.match(gid or ""):
            return None
        value = _read_json(self.dir / f"{gid}.json")
        return value if isinstance(value, dict) else None

    def all(self) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("*.json")):
            value = _read_json(path)
            if isinstance(value, dict) and value.get("id"):
                out.append(value)
        out.sort(key=lambda g: -(g.get("updatedAt") or 0))
        return out

    def remove(self, gid: str) -> bool:
        if not GRID_ID.match(gid or ""):
            return False
        path = self.dir / f"{gid}.json"
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def prune(self, keep: list[str]) -> int:
        wanted = {g for g in keep if GRID_ID.match(str(g))}
        removed = 0
        for path in self.dir.glob("*.json"):
            if path.stem not in wanted:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def find(self, ref: str) -> dict | None:
        """A grid by id, else by name (case-insensitive, exact before prefix)."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        hit = self.get(ref) if GRID_ID.match(ref) else None
        if hit:
            return hit
        grids = self.all()
        low = ref.lower()
        for grid in grids:
            if str(grid.get("name", "")).strip().lower() == low:
                return grid
        starts = [g for g in grids if str(g.get("name", "")).strip().lower().startswith(low)]
        return starts[0] if len(starts) == 1 else None


class Inbox:
    """Entries the agent sent the page, one JSON file each."""

    def __init__(self, root: Path | None = None):
        self.dir = (root or data_dir()) / "inbox"
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, entry: dict) -> None:
        _write_json(self.dir / f"{entry['id']}.json", entry)
        for old in sorted(self.dir.glob("w*.json"), key=lambda p: p.stat().st_mtime)[:-_MAX_INBOX]:
            old.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("w*.json"), key=lambda p: p.stat().st_mtime):
            value = _read_json(path)
            if isinstance(value, dict):
                out.append(value)
        return out

    def ack(self, entry_id: str) -> bool:
        if not ENTRY_ID.match(entry_id or ""):
            return False
        path = self.dir / f"{entry_id}.json"
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed
