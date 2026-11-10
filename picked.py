"""Read or write the one file the person just picked in a system dialog.

The Sheets page opens the dialog through the Desktop SDK (``ctx.os.pickOpenPath`` and
``pickSavePath``) and sends the chosen path to the backend, which is where the SDK says a picked
path belongs. Both calls are narrow on purpose: import reads text and CSV files up to a size cap,
export writes CSV, JSON or an Excel workbook, and neither touches Hermes' own home folder.

Every function returns ``{"ok": True, ...}`` or ``{"ok": False, "error": "..."}`` and never raises,
so the page can show the reason as it is.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

READ_SUFFIXES = (".csv", ".tsv", ".txt", ".md", ".log")
WRITE_SUFFIXES = (".csv", ".json")
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 64 * 1024 * 1024
MAX_XLSX_CELLS = 1_000_000
_NUMBER = re.compile(r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
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


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _xml_text(value) -> str:
    return escape(_XML_ILLEGAL.sub("", str(value)))


def _cell(ref: str, value: str, number: bool, style: int = 0) -> str:
    styled = f' s="{style}"' if style else ""
    if number and _NUMBER.fullmatch(value.strip()):
        return f'<c r="{ref}"{styled}><v>{value.strip()}</v></c>'
    if value == "":
        return ""
    # Inline strings, never formulas: a cell that starts with = stays text in Excel.
    return f'<c r="{ref}" t="inlineStr"{styled}><is><t xml:space="preserve">{_xml_text(value)}</t></is></c>'


def _sheet_name(name) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", _XML_ILLEGAL.sub("", str(name or ""))).strip().strip("'")
    return (cleaned or "Sheet")[:31]


def _workbook(sheet: dict) -> bytes:
    columns = sheet.get("columns")
    rows = sheet.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list):
        raise _Refused("There was nothing to write.")
    names = [str(c.get("name", "")) if isinstance(c, dict) else str(c) for c in columns]
    numbers = [isinstance(c, dict) and c.get("type") == "number" for c in columns]
    if len(names) * (len(rows) + 1) > MAX_XLSX_CELLS:
        raise _Refused("That export is too large to write.")

    lines = ['<row r="1">' + "".join(_cell(f"{_column_letter(i)}1", n, False, 1) for i, n in enumerate(names)) + "</row>"]
    for r, row in enumerate(rows, start=2):
        values = row if isinstance(row, list) else []
        cells = "".join(
            _cell(f"{_column_letter(i)}{r}", "" if i >= len(values) or values[i] is None else str(values[i]), numbers[i])
            for i in range(len(names))
        )
        lines.append(f'<row r="{r}">{cells}</row>')

    # Wide enough for the header and the longest value in the first 500 rows, up to 60 characters.
    longest = [len(n) for n in names]
    for row in rows[:500]:
        for i, value in enumerate(row[: len(names)] if isinstance(row, list) else []):
            longest[i] = max(longest[i], len(str(value or "")))
    widths = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{min(60, max(10, size + 2))}" customWidth="1"/>'
        for i, size in enumerate(longest)
    )
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{pkg}">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{main}" xmlns:r="{rel}">'
            f'<sheets><sheet name="{_xml_text(_sheet_name(sheet.get("name")))}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{pkg}">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel}/styles" Target="styles.xml"/></Relationships>'
        ),
        "xl/styles.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{main}">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{main}">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
            f'<cols>{widths}</cols><sheetData>{"".join(lines)}</sheetData></worksheet>'
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def write_xlsx(raw, sheet, home: Path | None = None) -> dict:
    """Write the grid as a one-sheet Excel workbook to the path named in the save dialog. Number
    columns become numbers; everything else is text, so nothing in a cell runs as a formula."""
    try:
        path = _checked(raw, (".xlsx",), "writes", home)
        if not isinstance(sheet, dict):
            raise _Refused("There was nothing to write.")
        data = _workbook(sheet)
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
