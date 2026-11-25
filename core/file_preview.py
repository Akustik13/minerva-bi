"""👁 Перегляд файлу в Minerva: PDF, картинки, Word (docx), Excel (xlsx / xlsm) і CSV — таблицею, текст (txt, json,
Gerber, свердління…), архіви (zip, tar) — список файлів, кожен відкривається тут же; doc / rtf / odt — у PDF через
LibreOffice, якщо він є на сервері. Решта (RAR, 7z, PowerPoint, програми…) — лише «⬇ Завантажити».

Вміст файлу ніколи не виконується й не вставляється як HTML: Word / Excel / текст стають рядками й таблицями, які
шаблон екранує; PDF і картинки віддає email_assistant.views._attachment_response (безпечний тип, nosniff, sandbox).

  kind(name, data=b"")          — pdf / image / sheet / docx / text / archive / office / none (за назвою й першими байтами);
  open_entry(name, data, chain) — файл усередині архіву (zip, tar; docx / xlsx — теж zip: картинки з документа);
                                  chain — шлях через вкладені архіви, до CHAIN_MAX; PreviewError — зрозуміла причина;
  build(name, data)             — Preview для сторінки: що показати й чого не вмістилось;
  raw(name, data)               — (байти, тип) для <img> / <iframe>: BMP / TIFF → PNG;
  office_pdf(name, data)        — doc / rtf / odt → PDF (кеш у тимчасовій теці за sha256), None — LibreOffice немає.
"""
import csv
import datetime
import hashlib
import io
import logging
import os
import re
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PREVIEW_MAX = 40 * 1024 * 1024        # більший файл — лише завантажити
ENTRY_MAX = 40 * 1024 * 1024          # файл в архіві після розпакування
RATIO_MAX = 200                       # стиснення понад 200 : 1 у великому файлі — «zip-бомба», не розпаковуємо
LIST_MAX = 2000                       # файлів у списку архіву
CHAIN_MAX = 4                         # вкладених архівів
SHEETS_MAX, ROWS_MAX, COLS_MAX = 20, 500, 40
RICH_MAX = 8 * 1024 * 1024            # xlsx до 8 МБ — з оформленням (об'єднані клітинки, кольори); більший — лише значення
TEXT_MAX = 300_000
BLOCKS_MAX = 3000
MEDIA_MAX = 40                        # картинок з docx / xlsx
PIXELS_MAX = 40_000_000

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
IMAGE_CONVERT = {"bmp", "tif", "tiff"}            # браузер не всі показує — у PNG
SHEET_EXT = {"xlsx", "xlsm"}
CSV_EXT = {"csv", "tsv"}
DOCX_EXT = {"docx", "docm", "dotx"}
OFFICE_EXT = {"doc", "rtf", "odt"}                # LibreOffice Writer (у Docker-образі лише він)
ZIP_EXT = {"zip"}
ZIP_LIKE = ZIP_EXT | DOCX_EXT | SHEET_EXT | {"pptx", "odt", "ods"}   # усередині — теж zip
TAR_SUFFIXES = (".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")
TEXT_EXT = {"txt", "md", "json", "xml", "log", "ini", "cfg", "conf", "yaml", "yml", "svg", "html", "htm", "readme",
            # Gerber, свердління, монтаж — текст
            "gbr", "ger", "pho", "art", "gtl", "gbl", "gto", "gbo", "gts", "gbs", "gtp", "gbp", "gko", "gm1", "gm2",
            "gml", "gpt", "gpb", "g1", "g2", "g3", "g4", "drl", "xln", "nc", "ncd", "exc", "tap", "ipc", "net",
            "pnp", "pos", "cpl", "rep", "rpt", "lst", "dri", "gpi"}
NO_PREVIEW = {"rar": "Архів RAR", "7z": "Архів 7z", "ppt": "PowerPoint", "pptx": "PowerPoint", "xls": "Старий Excel (xls)",
              "exe": "Програма", "msi": "Програма", "dwg": "Креслення AutoCAD", "step": "3D-модель STEP",
              "stp": "3D-модель STEP", "iges": "3D-модель IGES", "igs": "3D-модель IGES", "stl": "3D-модель STL",
              "brd": "Плата (CAD)", "pcbdoc": "Плата Altium", "schdoc": "Схема Altium", "kicad_pcb": "Плата KiCad"}
ICONS = {"pdf": "📕", "image": "🖼", "sheet": "📗", "docx": "📘", "text": "📄", "archive": "📦", "office": "📘",
         "none": "📎"}
KIND_NAMES = {"pdf": "PDF", "image": "Картинка", "sheet": "Таблиця", "docx": "Документ Word", "text": "Текст",
              "archive": "Архів", "office": "Документ", "none": "Файл"}


class PreviewError(Exception):
    pass


def ext_of(name) -> str:
    base = os.path.basename(str(name or "")).lower()
    for suf in TAR_SUFFIXES:
        if base.endswith(suf):
            return suf.lstrip(".")
    if base.startswith("readme") and "." not in base:
        return "readme"
    return base.rsplit(".", 1)[-1] if "." in base else ""


def _is_tar(name) -> bool:
    return os.path.basename(str(name or "")).lower().endswith(TAR_SUFFIXES)


def _looks_text(data: bytes) -> bool:
    head = data[:8192]
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:          # обрізаний посередині символ у кінці шматка — теж текст
        return e.start > len(head) - 4


def kind(name, data: bytes = b"") -> str:
    e = ext_of(name)
    if e == "pdf" or data[:5] == b"%PDF-":
        return "pdf"
    if e in IMAGE_EXT or e in IMAGE_CONVERT:
        return "image"
    if e in SHEET_EXT or e in CSV_EXT:
        return "sheet"
    if e in DOCX_EXT:
        return "docx"
    if e in ZIP_EXT or _is_tar(name):
        return "archive"
    if e in OFFICE_EXT:
        return "office"
    if e in NO_PREVIEW:
        return "none"
    if e in TEXT_EXT or (data and _looks_text(data)):
        return "text"
    return "none"


def icon(name) -> str:
    return ICONS.get(kind(name), "📎")


def size_text(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} КБ"
    return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")


def _zip_label(info) -> str:
    """Назва файлу в архіві для людини: архіви з Китаю часто в GBK без позначки UTF-8 — zipfile читає їх як cp437."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def _zip_read(z, info) -> bytes:
    if info.flag_bits & 0x1:
        raise PreviewError("Файл в архіві зашифровано паролем — завантажте архів і відкрийте його на ПК.")
    if info.file_size > ENTRY_MAX:
        raise PreviewError(f"Файл в архіві завеликий для перегляду ({size_text(info.file_size)}) — завантажте його.")
    if info.file_size > 5 * 1024 * 1024 and info.file_size > max(info.compress_size, 1) * RATIO_MAX:
        raise PreviewError("Файл в архіві стиснуто підозріло сильно — Minerva його не розпаковує; завантажте архів.")
    with z.open(info) as fh:
        data = fh.read(ENTRY_MAX + 1)
    if len(data) > ENTRY_MAX:
        raise PreviewError("Файл в архіві завеликий для перегляду — завантажте його.")
    return data


def open_entry(name, data: bytes, chain) -> tuple:
    """(назва, байти) файлу за шляхом chain усередині архівів; порожній chain — сам файл."""
    chain = [str(c) for c in (chain or []) if str(c)]
    if len(chain) > CHAIN_MAX:
        raise PreviewError("Забагато вкладених архівів.")
    for step in chain:
        e = ext_of(name)
        if e in ZIP_LIKE or data[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    info = next((i for i in z.infolist() if i.filename == step and not i.is_dir()), None)
                    if info is None:
                        raise PreviewError("Такого файлу в архіві немає.")
                    name, data = step, _zip_read(z, info)
            except zipfile.BadZipFile:
                raise PreviewError("Архів пошкоджений — завантажте його.") from None
        elif _is_tar(name):
            try:
                with tarfile.open(fileobj=io.BytesIO(data)) as t:
                    m = next((m for m in t.getmembers() if m.name == step and m.isfile()), None)
                    if m is None:
                        raise PreviewError("Такого файлу в архіві немає.")
                    if m.size > ENTRY_MAX:
                        raise PreviewError(f"Файл в архіві завеликий для перегляду ({size_text(m.size)}).")
                    name, data = step, t.extractfile(m).read()
            except tarfile.TarError:
                raise PreviewError("Архів пошкоджений — завантажте його.") from None
        else:
            raise PreviewError("Це не архів.")
    return name, data


# ── що показати ─────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Entry:
    path: str                  # як в архіві — для посилання
    label: str                 # для людини
    size: int = 0
    when: str = ""
    kind: str = "none"
    note: str = ""             # 🔒 зашифровано, завеликий…

    @property
    def icon(self):
        return ICONS.get(self.kind, "📎")

    @property
    def size_text(self):
        return size_text(self.size)

    @property
    def viewable(self):
        return not self.note and self.kind != "none"


@dataclass
class Sheet:
    name: str
    rows: list
    cols: int
    more_rows: bool = False
    more_cols: bool = False
    hidden: bool = False
    rich: bool = False         # rows — [{n, cells: [{v, s, rs, cs}]}], colw — [{letter, w}] (лише видимі)
    colw: list = field(default_factory=list)

    @property
    def letters(self):
        return [_col_letter(i) for i in range(self.cols)]

    @property
    def width(self):            # px: колонка номерів рядків + ширини колонок, як у файлі
        return 42 + sum(c["w"] for c in self.colw)


@dataclass
class Preview:
    name: str
    size: int
    kind: str
    error: str = ""
    note: str = ""
    text: str = ""
    more: bool = False
    blocks: list = field(default_factory=list)      # docx: {t: h1..h4 / p / li / table, text, rows, bold}
    sheets: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    media: list = field(default_factory=list)       # картинки з docx / xlsx — шляхи в zip
    images_in_doc: int = 0

    @property
    def icon(self):
        return ICONS.get(self.kind, "📎")

    @property
    def kind_name(self):
        return KIND_NAMES.get(self.kind, "Файл")

    @property
    def size_text(self):
        return size_text(self.size)

    @property
    def basename(self):
        return os.path.basename(self.name.rstrip("/")) or self.name

    @property
    def entries_total(self):
        return sum(e.size for e in self.entries)


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else f"{v:.10g}"
    if isinstance(v, datetime.datetime):
        return v.strftime("%d.%m.%Y %H:%M") if (v.hour or v.minute) else v.strftime("%d.%m.%Y")
    if isinstance(v, datetime.date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, datetime.time):
        return v.strftime("%H:%M")
    return str(v)


def _trim(rows) -> tuple:
    """Без порожніх рядків / колонок у кінці; (рядки, колонок)."""
    while rows and not any(c != "" for c in rows[-1]):
        rows.pop()
    cols = max((max((i + 1 for i, c in enumerate(r) if c != ""), default=0) for r in rows), default=0)
    return [r[:cols] + [""] * (cols - len(r[:cols])) for r in rows], cols


_RGB = re.compile(r"^[0-9A-Fa-f]{8}$")
_CURRENCY = re.compile(r'"([^"]*)"|\[\$([^\]-]*)[^\]]*\]|([$€£¥₴₽])')


def _rgb(color):
    """#rrggbb з кольору openpyxl (лише явний ARGB; тема / індекс — ні)."""
    try:
        if color is not None and getattr(color, "type", "") == "rgb" and _RGB.match(str(color.rgb or "")):
            return "#" + str(color.rgb)[2:].lower()
    except Exception:  # noqa: BLE001 — дивний колір
        pass
    return ""


def _num(v, fmt) -> str:
    """Число за форматом Excel: знаки після коми, групування, %, валюта (символ як у форматі, до / після)."""
    from django.utils.formats import number_format
    f = (fmt or "").split(";")[0]
    if not f or f == "General" or "@" in f:
        return _cell(v)
    plain = re.sub(r'"[^"]*"|\[[^\]]*\]', "", f)
    m = re.search(r"0[.,](0+)", plain)
    dec = len(m.group(1)) if m else 0
    pct = "%" in plain
    x = v * 100 if pct else v
    text = number_format(round(x, dec), decimal_pos=dec, use_l10n=True, force_grouping="," in plain or "#" in plain)
    cur = ""
    pos = 0
    for mm in _CURRENCY.finditer(f):
        sym = next((g for g in mm.groups() if g), "")
        if sym and not sym.isspace():
            cur, pos = sym.strip(), mm.start()
            break
    if pct:
        text += " %"
    if cur:
        first0 = min([i for i in (f.find("0"), f.find("#")) if i >= 0] or [0])
        text = f"{cur}{text}" if pos < first0 else f"{text} {cur}"
    return text


def _rich_value(c) -> str:
    v = c.value
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return _num(v, c.number_format)
        except Exception:  # noqa: BLE001
            return _cell(v)
    return _cell(v)


def _rich_style(c) -> str:
    out = []
    try:
        f = c.font
        if f is not None:
            if f.b:
                out.append("font-weight:700")
            if f.i:
                out.append("font-style:italic")
            if f.u:
                out.append("text-decoration:underline")
            col = _rgb(f.color)
            if col and col != "#000000":
                out.append(f"color:{col}")
            if f.sz and float(f.sz) >= 12.5:
                out.append(f"font-size:{min(float(f.sz), 22):.0f}pt")
        fill = c.fill
        if fill is not None and getattr(fill, "fill_type", None) == "solid":
            bg = _rgb(fill.fgColor)
            if bg and bg != "#ffffff":
                out.append(f"background:{bg}")
        a = c.alignment
        h = getattr(a, "horizontal", None) if a is not None else None
        if h in ("center", "centerContinuous"):
            out.append("text-align:center")
        elif h in ("right", "left"):
            out.append(f"text-align:{h}")
        elif isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
            out.append("text-align:right")
        if a is not None and getattr(a, "vertical", None) in ("center", "top", "bottom"):
            out.append("vertical-align:" + {"center": "middle"}.get(a.vertical, a.vertical))
        if a is not None and a.wrap_text:
            out.append("white-space:pre-wrap")
    except Exception:  # noqa: BLE001 — дивне оформлення — без нього
        pass
    return ";".join(out)


def _px(width) -> int:
    """Ширина колонки Excel (у знаках, за замовчуванням 8,43) → px."""
    return int(min(max(width or 8.43, 2), 80) * 7 + 10)


def _overflow(cells, px) -> list:
    """Як в Excel: довгий текст заходить на порожні клітинки праворуч (без заливки й об'єднання) — colspan."""
    out, i = [], 0
    while i < len(cells):
        c = cells[i]
        out.append(c)
        i += 1
        if not (c["text"] and c["v"] and c["cs"] == 1 and c["rs"] == 1 and "pre-wrap" not in c["s"]):
            continue
        need, have = len(c["v"]) * 7 + 10, px.get(c["c"], 70)
        while need > have and i < len(cells):
            n = cells[i]
            if n["v"] or n["cs"] != 1 or n["rs"] != 1 or "background" in n["s"] or n["c"] != c["c"] + c["cs"]:
                break
            c["cs"] += 1
            have += px.get(n["c"], 70)
            i += 1
    return out


def _sheet_rich(ws) -> Sheet:
    """Аркуш з оформленням: видимі рядки / колонки до ROWS_MAX × COLS_MAX, об'єднані клітинки — rowspan / colspan."""
    from openpyxl.utils import get_column_letter
    max_r, max_c = min(ws.max_row or 0, ROWS_MAX), min(ws.max_column or 0, COLS_MAX)
    # останні рядок / колонка з даними (порожнє форматування в кінці — не показуємо)
    last_r = last_c = 0
    for row in ws.iter_rows(min_row=1, max_row=max_r, max_col=max_c):
        for c in row:
            if c.value is not None and str(c.value).strip() != "":
                last_r, last_c = max(last_r, c.row), max(last_c, c.column)
    widths, hidden_c = {}, set()
    for dim in ws.column_dimensions.values():
        lo, hi = (dim.min or 0), (dim.max or 0)
        for i in range(max(lo, 1), min(hi, COLS_MAX) + 1):
            if dim.hidden:
                hidden_c.add(i)
            if dim.width:
                widths[i] = dim.width
    rows_vis = [r for r in range(1, last_r + 1) if not ws.row_dimensions[r].hidden] if last_r else []
    cols_vis = [c for c in range(1, last_c + 1) if c not in hidden_c] if last_c else []
    rv, cv = set(rows_vis), set(cols_vis)
    span, covered = {}, set()
    for rng in ws.merged_cells.ranges:
        r1, c1, r2, c2 = rng.min_row, rng.min_col, min(rng.max_row, last_r), min(rng.max_col, last_c)
        if r1 > last_r or c1 > last_c:
            continue
        rs = sum(1 for r in range(r1, r2 + 1) if r in rv)
        cs = sum(1 for c in range(c1, c2 + 1) if c in cv)
        span[(r1, c1)] = (rs, cs)
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if (r, c) != (r1, c1):
                    covered.add((r, c))
    out = []
    for r in rows_vis:
        cells = []
        for c in cols_vis:
            if (r, c) in covered:
                continue
            cell = ws.cell(row=r, column=c)
            rs, cs = span.get((r, c), (1, 1))
            if rs < 1 or cs < 1:        # об'єднана клітинка починається в прихованому рядку / колонці
                continue
            cells.append({"v": _rich_value(cell), "s": _rich_style(cell), "rs": rs, "cs": cs, "c": c,
                          "text": isinstance(cell.value, str)})
        out.append({"n": r, "cells": _overflow(cells, {c: _px(widths.get(c)) for c in cols_vis})})
    colw = [{"letter": get_column_letter(c), "w": _px(widths.get(c))} for c in cols_vis]
    return Sheet(ws.title, out, len(cols_vis), more_rows=(ws.max_row or 0) > ROWS_MAX,
                 more_cols=(ws.max_column or 0) > COLS_MAX, hidden=getattr(ws, "sheet_state", "") != "visible",
                 rich=True, colw=colw)


def _xlsx(data) -> list:
    from openpyxl import load_workbook
    if len(data) <= RICH_MAX:
        try:
            wb = load_workbook(io.BytesIO(data), read_only=False, data_only=True)
            try:
                return [_sheet_rich(ws) for ws in wb.worksheets[:SHEETS_MAX]]
            finally:
                wb.close()
        except Exception:  # noqa: BLE001 — не вийшло з оформленням — значення, як раніше
            logger.info("rich xlsx failed", exc_info=True)
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets[:SHEETS_MAX]:
            rows, more_rows, more_cols = [], False, False
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= ROWS_MAX:
                    more_rows = more_rows or any(v is not None for v in row)
                    if more_rows or n > ROWS_MAX + 2000:
                        break
                    continue
                more_cols = more_cols or any(v is not None for v in row[COLS_MAX:])
                rows.append([_cell(v) for v in row[:COLS_MAX]])
            rows, cols = _trim(rows)
            out.append(Sheet(ws.title, rows, cols, more_rows, more_cols, getattr(ws, "sheet_state", "") != "visible"))
    finally:
        wb.close()
    return out


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _csv(name, data) -> list:
    text = _decode(data[:8 * 1024 * 1024])
    sample = text[:8192]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = "\t" if ext_of(name) == "tsv" else (";" if sample.count(";") > sample.count(",") else ",")
    rows, more_rows, more_cols = [], False, False
    for n, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if n >= ROWS_MAX:
            more_rows = True
            break
        more_cols = more_cols or len(row) > COLS_MAX
        rows.append([c.strip() for c in row[:COLS_MAX]])
    rows, cols = _trim(rows)
    return [Sheet(os.path.basename(name), rows, cols, more_rows, more_cols)]


_HEADING = re.compile(r"^(?:heading|überschrift|заголовок|titre|título|标题)\s*(\d)", re.I)


def _docx(data) -> tuple:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(io.BytesIO(data))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    blocks, images = [], 0
    for el in doc.element.body.iterchildren():
        if len(blocks) >= BLOCKS_MAX:
            break
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            images += len(el.findall(f".//{w}drawing")) + len(el.findall(f".//{w}pict"))
            p = Paragraph(el, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = _HEADING.match(style)
            if style.lower() == "title" or style.lower() == "titel":
                t = "h1"
            elif m:
                t = f"h{min(max(int(m.group(1)), 1), 4)}"
            elif "list" in style.lower() or el.find(f"{w}pPr/{w}numPr") is not None:
                t = "li"
            else:
                t = "p"
            runs = [r for r in p.runs if r.text.strip()]
            blocks.append({"t": t, "text": text, "bold": bool(runs) and all(r.bold for r in runs)})
        elif tag == "tbl":
            rows = []
            try:
                for row in Table(el, doc).rows[:ROWS_MAX]:
                    seen, cells = set(), []
                    for c in row.cells[:COLS_MAX]:
                        if id(c._tc) in seen:               # об'єднані клітинки python-docx повторює
                            continue
                        seen.add(id(c._tc))
                        cells.append(c.text.strip())
                    if any(cells):
                        rows.append(cells)
            except Exception:  # noqa: BLE001 — дивна таблиця: без неї
                logger.info("docx table skipped", exc_info=True)
            if rows:
                blocks.append({"t": "table", "rows": rows})
    return blocks, images


def _media(data, prefix) -> list:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [i.filename for i in z.infolist()
                    if i.filename.startswith(prefix) and ext_of(i.filename) in IMAGE_EXT | IMAGE_CONVERT][:MEDIA_MAX]
    except zipfile.BadZipFile:
        return []


def _zip_entries(data) -> list:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            if len(out) >= LIST_MAX:
                break
            e = Entry(info.filename, _zip_label(info), info.file_size,
                      "{:02d}.{:02d}.{:04d}".format(info.date_time[2], info.date_time[1], info.date_time[0]),
                      kind(info.filename))
            if info.flag_bits & 0x1:
                e.note = "🔒 зашифровано"
            elif info.file_size > ENTRY_MAX:
                e.note = "завеликий для перегляду"
            out.append(e)
    return out


def _tar_entries(data) -> list:
    out = []
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            if not m.isfile():
                continue
            if len(out) >= LIST_MAX:
                break
            e = Entry(m.name, m.name, m.size, datetime.datetime.fromtimestamp(m.mtime).strftime("%d.%m.%Y")
                      if m.mtime else "", kind(m.name))
            if m.size > ENTRY_MAX:
                e.note = "завеликий для перегляду"
            out.append(e)
    return out


def build(name, data: bytes) -> Preview:
    k = kind(name, data)
    p = Preview(name=str(name), size=len(data or b""), kind=k)
    if not data:
        p.error = "Файл порожній."
        return p
    if len(data) > PREVIEW_MAX:
        p.kind, p.error = "none", f"Файл завеликий для перегляду в Minerva ({size_text(len(data))}) — завантажте його."
        return p
    e = ext_of(name)
    try:
        if k == "sheet":
            p.sheets = _csv(name, data) if e in CSV_EXT else _xlsx(data)
            if e in SHEET_EXT:
                p.media = _media(data, "xl/media/")
        elif k == "docx":
            p.blocks, p.images_in_doc = _docx(data)
            p.media = _media(data, "word/media/")
            p.more = len(p.blocks) >= BLOCKS_MAX
        elif k == "text":
            text = _decode(data[:TEXT_MAX * 2])
            p.more = len(text) > TEXT_MAX or len(data) > TEXT_MAX * 2
            p.text = text[:TEXT_MAX]
        elif k == "archive":
            p.entries = _zip_entries(data) if e in ZIP_EXT else _tar_entries(data)
            p.more = len(p.entries) >= LIST_MAX
        elif k == "none":
            what = NO_PREVIEW.get(e)
            p.note = (f"{what} — Minerva такі файли не показує; завантажте й відкрийте на ПК." if what else
                      "Такий файл Minerva не показує — завантажте й відкрийте на ПК.")
    except PreviewError as ex:
        p.error = str(ex)
    except Exception:  # noqa: BLE001 — зламаний файл, немає бібліотеки: людині — зрозуміло, у журнал — деталі
        logger.info("preview %s failed", name, exc_info=True)
        p.error = "Не вдалося прочитати файл — можливо, він пошкоджений або в старому форматі. Завантажте його."
    return p


def raw(name, data: bytes) -> tuple:
    """(байти, тип) для показу в браузері: BMP / TIFF — у PNG (браузери їх показують не всі)."""
    e = ext_of(name)
    if e in IMAGE_CONVERT:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = PIXELS_MAX
        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            (im if im.mode in ("RGB", "RGBA", "L") else im.convert("RGBA")).save(buf, "PNG")
        return buf.getvalue(), "image/png"
    types = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "gif": "image/gif", "webp": "image/webp"}
    return data, types.get(e, "application/octet-stream")


def _cache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "minerva-preview")
    os.makedirs(d, exist_ok=True)
    return d


def office_pdf(name, data: bytes):
    """doc / rtf / odt → PDF через LibreOffice (той самий, що для документів Minerva); None — немає LibreOffice
    або не вдалося. Результат — у тимчасовій теці за sha256 вмісту (поза /media/)."""
    if ext_of(name) not in OFFICE_EXT or not data:
        return None
    cached = os.path.join(_cache_dir(), hashlib.sha256(data).hexdigest() + ".pdf")
    if os.path.exists(cached):
        with open(cached, "rb") as fh:
            return fh.read()
    from documents.service import _convert_to_pdf
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "file." + ext_of(name))
        with open(src, "wb") as fh:
            fh.write(data)
        try:
            pdf = _convert_to_pdf(src)
        except Exception:  # noqa: BLE001 — LibreOffice упав / завис
            logger.info("office preview %s failed", name, exc_info=True)
            pdf = None
    if pdf:
        tmp_path = cached + ".part"
        with open(tmp_path, "wb") as fh:
            fh.write(pdf)
        os.replace(tmp_path, cached)
    return pdf


# ── сторінка перегляду ─────────────────────────────────────────────────────────────────────────────────────────


def respond(request, name, data: bytes, *, context=None, back="", back_label="", error=""):
    """Сторінка «👁 Перегляд» (шаблон admin/file_preview.html) для файлу, який виклик уже перевірив за правами.
    ?e=…&e=… — файл в архіві; ?raw=1 — сам файл для <img> / <iframe> (BMP / TIFF — PNG); ?pdf=1 — doc / rtf / odt
    у PDF; ?download=1 — завантажити; ?embed=1 — без меню Minerva (у вікні «📂 Файли», admin/file_preview_embed.html).
    error — файлу немає / не прочитався: сторінка з поясненням і «назад»."""
    from django.http import HttpResponse
    from django.shortcuts import render
    from django.utils.http import urlencode
    from email_assistant.views import _attachment_response

    chain = [c for c in request.GET.getlist("e") if c][:CHAIN_MAX + 1]
    embed = request.GET.get("embed") == "1"

    def url(ch, page=False, **extra):
        """Адреса цього ж перегляду: файл у архіві ch; перегляд у вікні лишається у вікні (embed), page — окрема сторінка."""
        q = [("e", c) for c in ch] + list(extra.items()) + ([("embed", "1")] if embed and not page and not extra else [])
        return request.path + ("?" + urlencode(q) if q else "")

    try:
        if error:
            raise PreviewError(error)
        inner_name, inner = open_entry(name, data or b"", chain)
        err = ""
    except PreviewError as ex:
        inner_name, inner, err = (chain[-1] if chain else name), b"", str(ex)
    base = os.path.basename(str(inner_name).rstrip("/")) or "file"
    if not err:
        if request.GET.get("download") == "1":
            return _attachment_response(inner, base, "application/octet-stream", force_download=True)
        if request.GET.get("raw") == "1":
            try:
                body, ctype = raw(inner_name, inner)
            except Exception:  # noqa: BLE001 — картинку не розібрати: віддати як є
                body, ctype = inner, "application/octet-stream"
            resp = _attachment_response(body, base, ctype)
            resp["X-Frame-Options"] = "SAMEORIGIN"
            return resp
        if request.GET.get("pdf") == "1":
            pdf = office_pdf(inner_name, inner)
            if not pdf:
                return HttpResponse("Не вдалося показати документ — завантажте його.", status=404,
                                    content_type="text/plain; charset=utf-8")
            resp = _attachment_response(pdf, os.path.splitext(base)[0] + ".pdf", "application/pdf")
            resp["X-Frame-Options"] = "SAMEORIGIN"
            return resp
    p = Preview(name=str(inner_name), size=0, kind=kind(inner_name), error=err) if err else build(inner_name, inner)
    office_ok = p.kind == "office" and not p.error and bool(office_pdf(inner_name, inner))
    if p.kind == "office" and not office_ok and not p.error:
        p.note = "Цей документ Minerva не змогла показати (на сервері немає LibreOffice або файл пошкоджений) — " \
                 "завантажте його й відкрийте на ПК."
    crumbs = [{"label": os.path.basename(str(name)) or str(name), "url": url([]) if chain else ""}]
    for i, c in enumerate(chain):
        crumbs.append({"label": os.path.basename(c.rstrip("/")) or c, "url": url(chain[:i + 1]) if i < len(chain) - 1 else ""})
    for e in p.entries:
        e.view_url = url(chain + [e.path])
        e.download_url = url(chain + [e.path], download="1")
    media = [{"name": os.path.basename(m), "raw": url(chain + [m], raw="1"), "view": url(chain + [m])}
             for m in p.media]
    resp = render(request, "admin/file_preview_embed.html" if embed else "admin/file_preview.html", {
        **({} if embed else (context or {})), "title": f"👁 {base}", "p": p, "crumbs": crumbs, "media": media,
        "raw_url": url(chain, raw="1"), "pdf_url": url(chain, pdf="1"), "download_url": url(chain, download="1"),
        "page_url": url(chain, page=True), "office_ok": office_ok, "back": back, "back_label": back_label, "embed": embed,
        # ← Назад: з файлу в архіві — до архіву, інакше — до сторінки, звідки файл (запит)
        "back_url": "" if embed else (url(chain[:-1]) if chain else back),
    })
    if embed:
        resp["X-Frame-Options"] = "SAMEORIGIN"
    return resp
