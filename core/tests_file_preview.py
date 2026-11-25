"""👁 core/file_preview.py — що й як показується: Excel / CSV таблицею, Word текстом і таблицями, текст, архіви
(список, файл усередині, GBK-назви, пароль, «zip-бомба»), картинки BMP → PNG, решта — лише завантажити."""
import io
import tarfile
import zipfile

from django.test import SimpleTestCase

from core import file_preview as fp


def _xlsx():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append(["Ref", "Part", "Qty"])
    ws.append(["U1", "STM32G0", 2])
    ws.append(["R1", "10k", 1.5])
    wb.create_sheet("Notes").append(["<b>bold</b>"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx():
    from docx import Document
    d = Document()
    d.add_heading("Stack-up", level=1)
    d.add_paragraph("4 layers, FR4 Tg170")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Layer", "Thickness"
    t.cell(1, 0).text, t.cell(1, 1).text = "L1", "35 um"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _zip(files, gbk=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    data = buf.getvalue()
    if gbk:                      # назва в GBK без позначки UTF-8 — як у китайських архіваторах
        raw = gbk.encode("gbk")
        holder = "X" * len(raw)
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as z:
            z.writestr(holder, b"x")
        data = buf2.getvalue().replace(holder.encode(), raw)       # байти назви — у заголовку й каталозі
    return data


class KindTests(SimpleTestCase):
    def test_kinds(self):
        self.assertEqual(fp.kind("a.PDF"), "pdf")
        self.assertEqual(fp.kind("noext", b"%PDF-1.7 ..."), "pdf")
        self.assertEqual(fp.kind("BOM.xlsx"), "sheet")
        self.assertEqual(fp.kind("bom.csv"), "sheet")
        self.assertEqual(fp.kind("spec.docx"), "docx")
        self.assertEqual(fp.kind("gerber.zip"), "archive")
        self.assertEqual(fp.kind("x.tar.gz"), "archive")
        self.assertEqual(fp.kind("top.GTL"), "text")
        self.assertEqual(fp.kind("old.doc"), "office")
        self.assertEqual(fp.kind("x.rar"), "none")
        self.assertEqual(fp.kind("blob.bin", b"\x00\x01\x02"), "none")
        self.assertEqual(fp.kind("notes", b"plain text"), "text")


class BuildTests(SimpleTestCase):
    def test_xlsx(self):
        p = fp.build("BOM.xlsx", _xlsx())
        self.assertEqual(p.kind, "sheet")
        self.assertEqual([s.name for s in p.sheets], ["BOM", "Notes"])
        bom = p.sheets[0]
        self.assertTrue(bom.rich)
        self.assertEqual([[c["v"] for c in r["cells"]] for r in bom.rows],
                         [["Ref", "Part", "Qty"], ["U1", "STM32G0", "2"], ["R1", "10k", "1.5"]])
        self.assertEqual([c["letter"] for c in bom.colw], ["A", "B", "C"])
        self.assertEqual(p.sheets[1].rows[0]["cells"][0]["v"], "<b>bold</b>")      # як текст — шаблон екранує

    def test_xlsx_like_excel(self):
        """Об'єднані клітинки, жирний, заливка, формат грошей, приховані рядки — як в Excel."""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "QUOTATION"
        ws.merge_cells("A1:C1")
        ws["A1"].font = Font(b=True, sz=16, color="FFC00000")
        ws["A1"].alignment = Alignment(horizontal="center")
        ws.append(["Qty", "PCB ($)", "Amount ($)"])
        ws["A2"].fill = PatternFill("solid", fgColor="FFD9D9D9")
        ws.append([2500, 1291.69, 26362.08])
        ws["B3"].number_format = '"$"#,##0.00'
        ws["C3"].number_format = '[$$-409]#,##0.00'
        ws.append(["hidden", 1, 2])
        ws.row_dimensions[4].hidden = True
        ws.append(["x", 0.25])
        ws["B5"].number_format = "0%"
        ws.column_dimensions["B"].width = 20
        buf = io.BytesIO()
        wb.save(buf)
        sh = fp.build("q.xlsx", buf.getvalue()).sheets[0]
        head = sh.rows[0]["cells"][0]
        self.assertEqual((head["v"], head["cs"], head["rs"]), ("QUOTATION", 3, 1))
        self.assertIn("font-weight:700", head["s"])
        self.assertIn("color:#c00000", head["s"])
        self.assertIn("text-align:center", head["s"])
        self.assertIn("background:#d9d9d9", sh.rows[1]["cells"][0]["s"])
        vals = [c["v"] for c in sh.rows[2]["cells"]]
        self.assertEqual(vals[0], "2500")
        self.assertTrue(vals[1].startswith("$1") and vals[1].endswith("291,69"))              # $1 291,69
        self.assertTrue(vals[2].startswith("$") and vals[2].endswith("362,08"))
        self.assertEqual([r["n"] for r in sh.rows], [1, 2, 3, 5])                              # 4 — приховано
        self.assertEqual(sh.rows[3]["cells"][1]["v"], "25 %")
        self.assertEqual(sh.colw[1]["w"], 150)                                                 # 20 знаків

    def test_csv(self):
        p = fp.build("bom.csv", "Ref;Part;Qty\nU1;STM32;2\n".encode("utf-8-sig"))
        self.assertEqual(p.sheets[0].rows, [["Ref", "Part", "Qty"], ["U1", "STM32", "2"]])

    def test_docx(self):
        p = fp.build("spec.docx", _docx())
        self.assertEqual(p.blocks[0], {"t": "h1", "text": "Stack-up", "bold": False})
        self.assertEqual(p.blocks[1]["text"], "4 layers, FR4 Tg170")
        self.assertEqual(p.blocks[2], {"t": "table", "rows": [["Layer", "Thickness"], ["L1", "35 um"]]})

    def test_text_and_none(self):
        p = fp.build("top.gtl", b"%FSLAX36Y36*%\nG04 test*\n")
        self.assertEqual(p.kind, "text")
        self.assertIn("G04 test", p.text)
        p = fp.build("board.rar", b"Rar!\x1a\x07\x00")
        self.assertEqual(p.kind, "none")
        self.assertIn("RAR", p.note)
        self.assertIn("порожній", fp.build("a.txt", b"").error)

    def test_broken_file(self):
        p = fp.build("BOM.xlsx", b"not really excel")
        self.assertIn("Не вдалося прочитати", p.error)


class ArchiveTests(SimpleTestCase):
    def test_list_and_open(self):
        inner = _zip({"readme.txt": b"inner hello"})
        data = _zip({"gerber/top.gtl": b"G04*", "BOM.xlsx": _xlsx(), "nested.zip": inner})
        p = fp.build("gerber.zip", data)
        self.assertEqual({e.path for e in p.entries}, {"gerber/top.gtl", "BOM.xlsx", "nested.zip"})
        kinds = {e.path: e.kind for e in p.entries}
        self.assertEqual((kinds["gerber/top.gtl"], kinds["BOM.xlsx"], kinds["nested.zip"]), ("text", "sheet", "archive"))
        name, got = fp.open_entry("gerber.zip", data, ["nested.zip", "readme.txt"])   # архів в архіві
        self.assertEqual((name, got), ("readme.txt", b"inner hello"))
        with self.assertRaises(fp.PreviewError):
            fp.open_entry("gerber.zip", data, ["missing.txt"])
        with self.assertRaises(fp.PreviewError):
            fp.open_entry("a.txt", b"plain", ["x"])

    def test_gbk_names(self):
        p = fp.build("cn.zip", _zip({}, gbk="报价单.pdf"))
        self.assertEqual(p.entries[0].label, "报价单.pdf")

    def test_encrypted_and_bomb(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.txt", b"0" * (6 * 1024 * 1024))           # стискається > 200 : 1
        data = bytearray(buf.getvalue())
        with self.assertRaises(fp.PreviewError):
            fp.open_entry("bomb.zip", bytes(data), ["big.txt"])
        info = zipfile.ZipFile(io.BytesIO(bytes(data))).infolist()[0]
        info.flag_bits |= 0x1
        with self.assertRaises(fp.PreviewError):
            fp._zip_read(zipfile.ZipFile(io.BytesIO(bytes(data))), info)

    def test_tar(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            ti = tarfile.TarInfo("drill.drl")
            ti.size = 4
            t.addfile(ti, io.BytesIO(b"M48\n"))
        p = fp.build("files.tar.gz", buf.getvalue())
        self.assertEqual([e.path for e in p.entries], ["drill.drl"])
        self.assertEqual(fp.open_entry("files.tar.gz", buf.getvalue(), ["drill.drl"])[1], b"M48\n")

    def test_docx_images(self):
        data = _zip({"word/document.xml": b"<x/>", "word/media/image1.png": b"png"})
        self.assertEqual(fp._media(data, "word/media/"), ["word/media/image1.png"])


class RawTests(SimpleTestCase):
    def test_bmp_to_png(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 3), "red").save(buf, "BMP")
        body, ctype = fp.raw("pic.bmp", buf.getvalue())
        self.assertEqual((ctype, body[:8]), ("image/png", b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(fp.raw("x.pdf", b"%PDF")[1], "application/pdf")
        self.assertEqual(fp.raw("x.xlsx", b"PK")[1], "application/octet-stream")
