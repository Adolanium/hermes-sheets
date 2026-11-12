"""Read or write the one file the person just picked in a system dialog.

The Sheets page opens the dialog through the Desktop SDK (``ctx.os.pickOpenPath`` and
``pickSavePath``) and sends the chosen path to the backend, which is where the SDK says a picked
path belongs. Both calls are narrow on purpose: import reads text and CSV files up to a size cap,
or one sheet of an Excel workbook, export writes CSV, JSON or an Excel workbook, and neither touches
Hermes' own home folder.

Workbooks are read and written with the standard library only (``zipfile`` and ``xml.etree``). No
macro runs and no formula is evaluated, in either direction.

Every function returns ``{"ok": True, ...}`` or ``{"ok": False, "error": "..."}`` and never raises,
so the page can show the reason as it is.
"""

from __future__ import annotations

import colorsys
import datetime
import io
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

READ_SUFFIXES = (".csv", ".tsv", ".txt", ".md", ".log")
WRITE_SUFFIXES = (".csv", ".json")
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 64 * 1024 * 1024
MAX_XLSX_CELLS = 1_000_000
MAX_STYLED_CELLS = 20_000
TABLE_SUFFIXES = (".xlsx",)
MAX_TABLE_BYTES = 8 * 1024 * 1024
MAX_PART_BYTES = 96 * 1024 * 1024
MAX_SHARED_STRINGS = 400_000
MAX_ROWS = 5001
MAX_COLUMNS = 60
MAX_CELL = 2000
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
        # An empty cell is still written when it carries a fill.
        return f'<c r="{ref}"{styled}/>' if style else ""
    # Inline strings, never formulas: a cell that starts with = stays text in Excel.
    return f'<c r="{ref}" t="inlineStr"{styled}><is><t xml:space="preserve">{_xml_text(value)}</t></is></c>'


def _argb(hue: float, saturation: float, lightness: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(hue / 360, lightness / 100, saturation / 100)
    return "FF%02X%02X%02X" % (round(red * 255), round(green * 255), round(blue * 255))


# The page stores a paint by name. These are the same colours it draws, as Excel wants them:
# (fill, ink) for each name.
_PAINTS = {
    "red": (_argb(4, 90, 91), _argb(4, 72, 42)),
    "orange": (_argb(28, 96, 88), _argb(24, 82, 40)),
    "yellow": (_argb(48, 96, 84), _argb(40, 86, 32)),
    "green": (_argb(140, 56, 86), _argb(146, 62, 28)),
    "blue": (_argb(208, 90, 90), _argb(212, 72, 42)),
    "violet": (_argb(262, 82, 92), _argb(262, 56, 48)),
    "grey": (_argb(220, 12, 90), _argb(220, 8, 44)),
}
_SIGNS = "\u00a3$\u20ac\u00a5"


def _format_code(raw) -> str:
    """The Excel number format for a column's format, or "" for General."""
    if not isinstance(raw, dict) or raw.get("kind") not in ("number", "currency", "percent"):
        return ""
    kind = raw["kind"]
    places = raw.get("decimals")
    if not isinstance(places, int) or isinstance(places, bool) or not 0 <= places <= 6:
        places = 0 if kind == "percent" else 2
    tail = "." + "0" * places if places else ""
    if kind == "percent":
        return f"0{tail}%"
    if kind == "number":
        return f"#,##0{tail}"
    sign = raw.get("symbol") if isinstance(raw.get("symbol"), str) and raw.get("symbol") in tuple(_SIGNS) else "$"
    body = f'"{sign}"#,##0{tail}'
    return f"{body};-{body}"


class _Looks:
    """The workbook's style table, built up as cells ask for a look. Style 0 is plain and style 1
    is the bold header; everything else is added once and shared."""

    def __init__(self) -> None:
        self.fonts = [(False, False, ""), (True, False, "")]
        self.fills = [""]
        self.formats: list[str] = []
        self.xfs = [(0, 0, 0, ""), (0, 1, 0, "")]

    @staticmethod
    def _at(items: list, item) -> int:
        if item not in items:
            items.append(item)
        return items.index(item)

    def index(self, code: str, look) -> int:
        look = look if isinstance(look, dict) else {}
        ink = _PAINTS[look["c"]][1] if look.get("c") in _PAINTS else ""
        fill = _PAINTS[look["f"]][0] if look.get("f") in _PAINTS else ""
        align = look.get("a") if look.get("a") in ("left", "center", "right") else ""
        font = self._at(self.fonts, (bool(look.get("b")), bool(look.get("i")), ink))
        # Fill 1 is the gray125 pattern every workbook must carry, so ours start at 2.
        fill_id = self._at(self.fills, fill) + 1 if fill else 0
        format_id = self._at(self.formats, code) + 164 if code else 0
        return self._at(self.xfs, (format_id, font, fill_id, align))

    def xml(self, main: str) -> str:
        formats = "".join(
            f'<numFmt numFmtId="{164 + i}" formatCode={quoteattr(code)}/>' for i, code in enumerate(self.formats)
        )
        fonts = "".join(
            "<font>"
            + ("<b/>" if bold else "")
            + ("<i/>" if italic else "")
            + '<sz val="11"/>'
            + (f'<color rgb="{ink}"/>' if ink else "")
            + '<name val="Calibri"/></font>'
            for bold, italic, ink in self.fonts
        )
        fills = '<fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>' + "".join(
            f'<fill><patternFill patternType="solid"><fgColor rgb="{fill}"/><bgColor indexed="64"/></patternFill></fill>'
            for fill in self.fills[1:]
        )
        xfs = "".join(
            f'<xf numFmtId="{format_id}" fontId="{font}" fillId="{fill}" borderId="0" xfId="0"'
            + (' applyNumberFormat="1"' if format_id else "")
            + (' applyFont="1"' if font else "")
            + (' applyFill="1"' if fill else "")
            + (f' applyAlignment="1"><alignment horizontal="{align}"/></xf>' if align else "/>")
            for format_id, font, fill, align in self.xfs
        )
        return (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{main}">'
            + (f'<numFmts count="{len(self.formats)}">{formats}</numFmts>' if self.formats else "")
            + f'<fonts count="{len(self.fonts)}">{fonts}</fonts>'
            f'<fills count="{len(self.fills) + 1}">{fills}</fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(self.xfs)}">{xfs}</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        )


def _cell_looks(raw) -> dict:
    """``[[row, column, look], ...]`` from the page as a lookup, ignoring anything malformed."""
    out = {}
    for entry in raw[:MAX_STYLED_CELLS] if isinstance(raw, list) else []:
        if isinstance(entry, list) and len(entry) == 3 and isinstance(entry[2], dict):
            row, column = entry[0], entry[1]
            if isinstance(row, int) and isinstance(column, int) and not isinstance(row, bool) and not isinstance(column, bool):
                out[(row, column)] = entry[2]
    return out


def _sheet_name(name) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", _XML_ILLEGAL.sub("", str(name or ""))).strip().strip("'")
    return (cleaned or "Sheet")[:31]


def _workbook(sheet: dict) -> bytes:
    columns = sheet.get("columns")
    rows = sheet.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list):
        raise _Refused("There was nothing to write.")
    names = [str(c.get("name", "")) if isinstance(c, dict) else str(c) for c in columns]
    codes = [_format_code(c.get("format")) if isinstance(c, dict) else "" for c in columns]
    numbers = [bool(codes[i]) or (isinstance(c, dict) and c.get("type") == "number") for i, c in enumerate(columns)]
    looks = _Looks()
    cell_looks = _cell_looks(sheet.get("styles"))
    if len(names) * (len(rows) + 1) > MAX_XLSX_CELLS:
        raise _Refused("That export is too large to write.")

    lines = ['<row r="1">' + "".join(_cell(f"{_column_letter(i)}1", n, False, 1) for i, n in enumerate(names)) + "</row>"]
    for r, row in enumerate(rows, start=2):
        values = row if isinstance(row, list) else []
        cells = []
        for i in range(len(names)):
            value = "" if i >= len(values) or values[i] is None else str(values[i])
            look = cell_looks.get((r - 2, i))
            # A number format only goes on a cell that holds a number.
            code = codes[i] if codes[i] and _NUMBER.fullmatch(value.strip()) else ""
            cells.append(_cell(f"{_column_letter(i)}{r}", value, numbers[i], looks.index(code, look) if code or look else 0))
        lines.append(f'<row r="{r}">{"".join(cells)}</row>')

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
        "xl/styles.xml": looks.xml(main),
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
    columns become numbers; everything else is text, so nothing in a cell runs as a formula. A
    column's number format and each cell's bold, italic, colour, fill and alignment go with it."""
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


_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_CURRENCY = "$\u00a3\u20ac\u00a5\u20b9"
_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")


def _part(archive: zipfile.ZipFile, name: str) -> bytes:
    """One file out of the workbook, refused when it is too big or carries a DTD."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise _Refused("That workbook is missing a part Sheets needs. Save it again as .xlsx and retry.") from None
    if info.file_size > MAX_PART_BYTES:
        raise _Refused("That workbook is too large to read.")
    data = archive.read(info)
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
        raise _Refused("That workbook has a kind of XML Sheets does not read.")
    return data


def _format_kind(code: str) -> str:
    """What a number format shows: a date, money, a percentage, or a plain number."""
    plain = re.sub(r'"[^"]*"|\\.', "", code)
    tagged = re.search(r"\[\$([^\-\]]+)", code)
    symbol = tagged.group(1)[:3] if tagged else next((ch for ch in re.sub(r"\[[^\]]*\]", "", code) if ch in _CURRENCY), "")
    bare = re.sub(r"\[[^\]]*\]", "", plain).lower()
    if bare.strip() in ("", "general", "@"):
        return "number"
    if any(ch in bare for ch in "dy") or ("m" in bare and any(ch in bare for ch in "hs:")) or bare.strip("m/-. ") == "":
        return "date"
    if any(ch in bare for ch in "hs") and "#" not in bare and "0" not in bare:
        return "date"
    if "%" in bare:
        return "percent"
    return f"money{symbol}" if symbol else "number"


def _styles(archive: zipfile.ZipFile) -> list[str]:
    """The kind of each cell style, by its index."""
    if "xl/styles.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(_part(archive, "xl/styles.xml"))
    custom = {}
    for fmt in root.iter(f"{_MAIN}numFmt"):
        try:
            custom[int(fmt.get("numFmtId", "-1"))] = fmt.get("formatCode", "")
        except ValueError:
            continue
    kinds = []
    cell_xfs = root.find(f"{_MAIN}cellXfs")
    for xf in cell_xfs if cell_xfs is not None else []:
        try:
            fmt_id = int(xf.get("numFmtId", "0"))
        except ValueError:
            fmt_id = 0
        if fmt_id in custom:
            kinds.append(_format_kind(custom[fmt_id]))
        elif fmt_id in _DATE_FORMATS:
            kinds.append("date")
        elif fmt_id in (9, 10):
            kinds.append("percent")
        elif fmt_id in (5, 6, 7, 8, 42, 44):
            kinds.append("money$")
        else:
            kinds.append("number")
    return kinds


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    out = []
    for _event, node in ElementTree.iterparse(_open_part(archive, "xl/sharedStrings.xml")):
        if node.tag == f"{_MAIN}si":
            out.append("".join(t.text or "" for t in node.iter(f"{_MAIN}t") if _not_phonetic(node, t)))
            node.clear()
            if len(out) > MAX_SHARED_STRINGS:
                raise _Refused("That workbook has too much text to read.")
    return out


def _not_phonetic(si, text_node) -> bool:
    """Skip the phonetic guide runs some workbooks carry beside the real text."""
    for run in si.iter(f"{_MAIN}rPh"):
        if text_node in list(run.iter(f"{_MAIN}t")):
            return False
    return True


def _open_part(archive: zipfile.ZipFile, name: str):
    info = archive.getinfo(name)
    if info.file_size > MAX_PART_BYTES:
        raise _Refused("That workbook is too large to read.")
    with archive.open(info) as probe:
        head = probe.read(4096)
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise _Refused("That workbook has a kind of XML Sheets does not read.")
    return archive.open(info)


def _sheets(archive: zipfile.ZipFile) -> tuple[list[tuple[str, str]], bool]:
    """Each visible sheet's name and the file it lives in, and whether dates count from 1904."""
    book = ElementTree.fromstring(_part(archive, "xl/workbook.xml"))
    rels = ElementTree.fromstring(_part(archive, "xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels.iter(f"{_PKG}Relationship")}
    props = book.find(f"{_MAIN}workbookPr")
    late = props is not None and props.get("date1904") in ("1", "true")
    out = []
    for sheet in book.iter(f"{_MAIN}sheet"):
        target = targets.get(sheet.get(f"{_REL}id"), "")
        if not target or sheet.get("state") in ("hidden", "veryHidden"):
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if path in archive.namelist() and "worksheets" in path:
            out.append((sheet.get("name") or f"Sheet {len(out) + 1}", path))
    return out, late


def _column(ref: str) -> int:
    match = _REF.match(ref or "")
    if not match:
        return -1
    index = 0
    for letter in match.group(1):
        index = index * 26 + ord(letter) - 64
    return index - 1


def _value(text: str, kind: str, late: bool):
    """A stored number as the page should see it: a date, money, a percentage or a number."""
    try:
        number = float(text)
    except ValueError:
        return text[:MAX_CELL]
    if kind == "date":
        if number < 1:
            seconds = round(number * 86400)
            return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"
        try:
            day = datetime.date(1904, 1, 1) if late else datetime.date(1899, 12, 30)
            return (day + datetime.timedelta(days=int(number))).isoformat()
        except OverflowError:
            return text[:MAX_CELL]
    if kind == "percent":
        return f"{round(number * 100, 4):g}%"
    if kind.startswith("money"):
        return f"{'-' if number < 0 else ''}{kind[5:]}{abs(number):.2f}"
    return int(number) if number.is_integer() and abs(number) < 1e15 else number


def _rows(archive: zipfile.ZipFile, path: str, strings: list[str], kinds: list[str], late: bool) -> tuple[list[list], bool]:
    rows: list[list] = []
    truncated = False
    for _event, node in ElementTree.iterparse(_open_part(archive, path)):
        if node.tag != f"{_MAIN}row":
            continue
        cells: list = []
        position = 0
        for cell in node.iter(f"{_MAIN}c"):
            at = _column(cell.get("r", ""))
            at = position if at < 0 else at
            position = at + 1
            if at >= MAX_COLUMNS:
                # Wider than Sheets takes: say so instead of dropping columns quietly.
                truncated = truncated or cell.find(f"{_MAIN}v") is not None or cell.find(f"{_MAIN}is") is not None
                continue
            kind = cell.get("t", "n")
            raw = cell.find(f"{_MAIN}v")
            text = raw.text if raw is not None and raw.text is not None else ""
            if kind == "s":
                try:
                    value = strings[int(text)][:MAX_CELL]
                except (ValueError, IndexError):
                    value = ""
            elif kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{_MAIN}t"))[:MAX_CELL]
            elif kind == "b":
                value = "true" if text == "1" else "false"
            elif kind in ("str", "e"):
                value = "" if kind == "e" else text[:MAX_CELL]
            elif text == "":
                value = ""
            else:
                try:
                    style = kinds[int(cell.get("s", "0"))]
                except (ValueError, IndexError):
                    style = "number"
                value = _value(text, style, late)
            if value != "":
                cells.extend([""] * (at - len(cells)))
                cells.append(value)
        node.clear()
        if cells:
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            rows.append(cells)
    width = max((len(row) for row in rows), default=0)
    return [row + [""] * (width - len(row)) for row in rows], truncated


def read_table(raw, sheet=None, home: Path | None = None) -> dict:
    """The rows of one sheet of a picked Excel workbook, and the names of its sheets."""
    try:
        path = _checked(raw, TABLE_SUFFIXES, "reads", home)
        if not path.is_file():
            raise _Refused("That file is not there any more.")
        if path.stat().st_size > MAX_TABLE_BYTES:
            raise _Refused("That workbook is over 8 MB. Save a smaller cut of it and try again.")
        try:
            with zipfile.ZipFile(path) as archive:
                sheets, late = _sheets(archive)
                if not sheets:
                    raise _Refused("That workbook has no sheet Sheets can read.")
                names = [name for name, _path in sheets]
                wanted = sheet if isinstance(sheet, str) and sheet in names else names[0]
                rows, truncated = _rows(archive, dict(sheets)[wanted], _shared_strings(archive), _styles(archive), late)
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError):
            raise _Refused("That file is not an Excel workbook Sheets can read. Old .xls files need saving as .xlsx first.") from None
    except _Refused as refused:
        return {"ok": False, "error": str(refused)}
    except OSError as error:
        return {"ok": False, "error": f"Hermes could not read that file: {_problem(error)}."}
    return {"ok": True, "path": raw, "name": path.name, "sheets": names, "sheet": wanted, "rows": rows, "truncated": truncated}
