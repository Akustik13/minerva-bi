"""
suppliers/services/rfq_files.py — що AI бачить у файлах запиту (🧠 «AI: скласти запит»): текст стекапу (docx / pdf),
BOM (xlsx / csv), README / txt і вміст ZIP з Gerber (назви файлів — шари, README й таблиці всередині). Лише читання
в пам'яті, з обмеженнями (архів-бомба, великий PDF не повісять сторінку); не вдалося — порожньо.

  read(name, data) → текст файлу (≤ TEXT_MAX);
  texts([(name, data)]) → [(name, текст)] разом ≤ TOTAL_MAX.
"""
import io
import logging
import os
import re
import zipfile

logger = logging.getLogger(__name__)

TEXT_MAX = 6000            # з одного файлу
TOTAL_MAX = 24000          # з усіх разом — до AI
FILE_MAX = 25 * 1024 * 1024
INNER_MAX = 2 * 1024 * 1024     # файл усередині ZIP, який читаємо
INNER_TOTAL = 8 * 1024 * 1024   # разом прочитаного з одного ZIP
LIST_MAX = 200                  # назв файлів архіву
TEXT_EXT = {"txt", "md", "csv", "tsv", "ini", "readme", "log", "json", "xml"}


def _ext(name) -> str:
    base = os.path.basename(str(name or "")).lower()
    if base.startswith("readme") and "." not in base:
        return "readme"
    return base.rsplit(".", 1)[-1] if "." in base else ""


def _text(data: bytes) -> str:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _pdf(data) -> str:
    from pypdf import PdfReader
    out = []
    for page in PdfReader(io.BytesIO(data)).pages[:12]:
        out.append(page.extract_text() or "")
        if sum(len(p) for p in out) > TEXT_MAX:
            break
    return "\n".join(out)


def _docx(data) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables[:10]:                     # стекап часто таблицею
        for row in table.rows[:60]:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _xlsx(data) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets[:3]:
        lines.append(f"[{ws.title}]")
        for row in ws.iter_rows(max_row=200, values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row[:15]]
            if any(cells):
                lines.append("\t".join(cells))
    wb.close()
    return "\n".join(lines)


def _zip(data) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        lines = [f"Файли в архіві ({len(names)}): " + ", ".join(names[:LIST_MAX])]
        budget = INNER_TOTAL
        for info in infos:
            ext, base = _ext(info.filename), os.path.basename(info.filename).lower()
            wanted = ext in {"pdf", "docx", "xlsx", "xlsm", "csv", "md", "readme"} or base.startswith("readme") or \
                (ext == "txt" and not any(w in base for w in ("drill", "drl", "hole")))   # свердління — координати
            if not wanted or info.file_size > INNER_MAX or info.file_size > budget:
                continue
            budget -= info.file_size
            inner = read(info.filename, z.read(info))
            if inner:
                lines.append(f"--- {info.filename} ---\n{inner}")
            if sum(len(x) for x in lines) > TEXT_MAX:
                break
    return "\n".join(lines)


def read(name, data: bytes) -> str:
    """Текст файлу для AI; не той тип / не вдалося — ''."""
    if not data or len(data) > FILE_MAX:
        return ""
    ext = _ext(name)
    try:
        if ext == "pdf":
            out = _pdf(data)
        elif ext == "docx":
            out = _docx(data)
        elif ext in ("xlsx", "xlsm"):
            out = _xlsx(data)
        elif ext == "zip":
            out = _zip(data)
        elif ext in TEXT_EXT or os.path.basename(str(name)).lower().startswith("readme"):
            out = _text(data[:200_000])
        else:
            return ""
    except Exception:  # noqa: BLE001 — зламаний файл, немає бібліотеки: лише без нього
        logger.info("rfq file %s not readable", name, exc_info=True)
        return ""
    return re.sub(r"\n{3,}", "\n\n", out).strip()[:TEXT_MAX]


def texts(files) -> list:
    """[(назва, байти)] → [(назва, текст)] з тексту, разом ≤ TOTAL_MAX (порожні — без тексту, але з назвою)."""
    out, left = [], TOTAL_MAX
    for name, data in files:
        text = read(name, data)[:max(0, left)]
        left -= len(text)
        out.append((str(name)[:200], text))
    return out
