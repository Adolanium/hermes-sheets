"""Read or write the one file the person just picked in a system dialog.

The Sheets page opens the dialog through the Desktop SDK (``ctx.os.pickOpenPath`` and
``pickSavePath``) and sends the chosen path to the backend, which is where the SDK says a picked
path belongs. Both calls are narrow on purpose: import reads text and CSV files up to a size cap,
export writes CSV and JSON, and neither touches Hermes' own home folder.

Every function returns ``{"ok": True, ...}`` or ``{"ok": False, "error": "..."}`` and never raises,
so the page can show the reason as it is.
"""

from __future__ import annotations

from pathlib import Path

READ_SUFFIXES = (".csv", ".tsv", ".txt", ".md", ".log")
WRITE_SUFFIXES = (".csv", ".json")
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 64 * 1024 * 1024
_BINARY_SAMPLE = 4096


class _Refused(Exception):
    pass


def _protected(home: Path | None) -> list[Path]:
    """Hermes' home folder, and the root above it when the home is a profile."""
    if home is None:
        return []
    roots = [home.resolve()]
    if roots[0].parent.name == "profiles":
        roots.append(roots[0].parent.parent)
    return roots


def _checked(raw, suffixes: tuple[str, ...], verb: str, home: Path | None) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise _Refused("No file was picked.")
    path = Path(raw)
    if not path.is_absolute():
        raise _Refused("Sheets needs the full path of the picked file.")
    resolved = path.resolve()
    kinds = ", ".join(suffixes)
    if path.suffix.lower() not in suffixes or resolved.suffix.lower() not in suffixes:
        raise _Refused(f"Sheets only {verb} {kinds} files.")
    for root in _protected(home):
        if resolved == root or root in resolved.parents:
            raise _Refused(f"Sheets never {verb} files inside the Hermes home folder.")
    return resolved


def _problem(error: OSError) -> str:
    return error.strerror or error.__class__.__name__


def read_text(raw, home: Path | None = None) -> dict:
    """The text of a picked file. A file over the cap comes back ``truncated`` with no text, so a
    partial sheet is never imported by mistake."""
    try:
        path = _checked(raw, READ_SUFFIXES, "reads", home)
        if not path.is_file():
            raise _Refused("That file is not there any more.")
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return {"ok": True, "path": raw, "text": "", "truncated": True, "binary": False, "bytes": size}
        data = path.read_bytes()
    except _Refused as refused:
        return {"ok": False, "error": str(refused)}
    except OSError as error:
        return {"ok": False, "error": f"Hermes could not read that file: {_problem(error)}."}
    if b"\x00" in data[:_BINARY_SAMPLE]:
        return {"ok": True, "path": raw, "text": "", "truncated": False, "binary": True, "bytes": len(data)}
    return {
        "ok": True,
        "path": raw,
        "text": data.decode("utf-8", errors="replace"),
        "truncated": False,
        "binary": False,
        "bytes": len(data),
    }


def write_text(raw, text, home: Path | None = None) -> dict:
    """Write an export to the path the person named in the save dialog."""
    try:
        path = _checked(raw, WRITE_SUFFIXES, "writes", home)
        if not isinstance(text, str):
            raise _Refused("There was nothing to write.")
        data = text.encode("utf-8", errors="replace")
        if len(data) > MAX_WRITE_BYTES:
            raise _Refused("That export is too large to write.")
        if path.is_dir():
            raise _Refused("That path is a folder, not a file.")
        if not path.parent.is_dir():
            raise _Refused("That folder does not exist.")
        path.write_bytes(data)
    except _Refused as refused:
        return {"ok": False, "error": str(refused)}
    except OSError as error:
        return {"ok": False, "error": f"Hermes could not write that file: {_problem(error)}."}
    return {"ok": True, "path": raw, "bytes": len(data)}
