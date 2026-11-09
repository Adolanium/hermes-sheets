"""Backend for the Sheets page in Hermes Desktop, mounted at ``/api/plugins/sheets/``.

The page writes a copy of each grid here while chat access is on, so the agent's sheets_* tools can
read it, and it collects what the agent sent (new grids, proposed changes) from the inbox. Files
live under ``<hermes home>/plugin-data/sheets/`` (see store.py).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, Body

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _store():
    name = "hermes_sheets_store"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "store.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


router = APIRouter()


@router.get("/inbox")
def inbox() -> dict:
    return {"entries": _store().Inbox().list()}


@router.post("/inbox/{entry_id}/ack")
def ack(entry_id: str) -> dict:
    return {"removed": _store().Inbox().ack(entry_id)}


@router.post("/sharing")
def sharing(body: dict = Body(...)) -> dict:
    grids = _store().Grids()
    grids.set_sharing(bool(body.get("on")))
    return grids.sharing()


@router.put("/grids/{grid_id}")
def put_grid(grid_id: str, body: dict = Body(...)) -> dict:
    grids = _store().Grids()
    if not grids.sharing()["on"] or str(body.get("id")) != grid_id:
        return {"stored": False}
    return {"stored": grids.put(body)}


@router.delete("/grids/{grid_id}")
def delete_grid(grid_id: str) -> dict:
    return {"removed": _store().Grids().remove(grid_id)}


@router.post("/grids/prune")
def prune(body: dict = Body(...)) -> dict:
    keep = body.get("keep") if isinstance(body.get("keep"), list) else []
    return {"removed": _store().Grids().prune([str(k) for k in keep])}
