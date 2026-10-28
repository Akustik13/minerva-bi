"""«📂 Джерела знань» (етап 4): папки й сайти → база знань (rag_assistant/knowledge_sources.py).

python manage.py test rag_assistant --settings=tabele.settings_test   (мережа підмінена)
"""
import os
import shutil
import tempfile
import time
from unittest.mock import patch

from django.test import override_settings

from rag_assistant import knowledge, knowledge_sources
from rag_assistant.models import KnowledgeDoc, KnowledgeItem, KnowledgeSource
from rag_assistant.tests_knowledge import KnowledgeBase

PDF = b"%PDF-1.4\n% synthetic datasheet\n"


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class SourcesBase(KnowledgeBase):
    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        roots = override_settings(KNOWLEDGE_SOURCE_ROOTS=[self.root])
        roots.enable()
        self.addCleanup(roots.disable)
        self.folder = os.path.join(self.root, "Dokumentation")
        os.makedirs(self.folder)

    def write(self, rel, data):
        path = os.path.join(self.folder, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def source(self, **kw):
        values = dict(name="Doku", kind="folder", path=self.folder, owner=self.user)
        values.update(kw)
        return KnowledgeSource.objects.create(**values)

    def documents(self, after=0):
        data = knowledge.read_changes(after=after, types=("email", "document"))
        return data, {i["document_id"]: i for i in data["items"] if i["document_type"] == "document"}


class FolderTests(SourcesBase):
    def test_paths_only_inside_allowed_roots(self):
        self.assertEqual(knowledge_sources.check_folder(self.folder), os.path.realpath(self.folder))
        outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        with self.assertRaises(knowledge_sources.SourceError):
            knowledge_sources.check_folder(outside)
        with self.assertRaises(knowledge_sources.SourceError):
            knowledge_sources.check_folder(os.path.join(self.root, "missing"))
        with self.assertRaises(knowledge_sources.SourceError):
            knowledge_sources.check_folder("relative/path")
        self.assertEqual(knowledge_sources.resolve(self.source(), "../../etc/passwd"), "")

    def test_scan_types_links_text_and_feed(self):
        from inventory.models import Product
        from openpyxl import Workbook
        product = Product.objects.create(sku="FT-100", name="Filter")
        self.write("FT-100_Datenblatt.pdf", PDF)
        wb = Workbook()
        wb.active.title = "Preise"
        wb.active.append(["Artikel", "Preis"])
        wb.active.append(["FT-100", 19.5])
        wb.save(os.path.join(self.folder, "Preisliste.xlsx"))
        self.write("Montage.md", "# Montage\nDrehmoment 0,56 Nm".encode())
        self.write("faq.html", b"<html><body><h1>FAQ</h1><p>Lieferzeit 3 Wochen</p></body></html>")
        self.write("sub/manual.txt", b"Manual text")
        self.write(".hidden.pdf", PDF)
        self.write("~$lock.docx", b"x")
        self.write("tool.exe", b"MZ")
        source = self.source(customer=self.client_co)
        result = knowledge_sources.scan(source)
        self.assertEqual((result["created"], result["removed"]), (5, 0), result)
        items = {i.source_path: i for i in source.items.all()}
        self.assertEqual(set(items), {"FT-100_Datenblatt.pdf", "Preisliste.xlsx", "Montage.md", "faq.html", "sub/manual.txt"})
        self.assertEqual(items["FT-100_Datenblatt.pdf"].product_id, product.pk)          # артикул у назві файлу
        self.assertTrue(all(i.customer_id == self.client_co.pk and i.auto for i in items.values()))
        self.assertIn("FT-100 | 19.5", items["Preisliste.xlsx"].text)
        self.assertIn("Drehmoment", items["Montage.md"].text)
        self.assertIn("Lieferzeit 3 Wochen", items["faq.html"].text)
        self.assertNotIn("<p>", items["faq.html"].text)
        _, docs = self.documents()
        pdf = docs[f"document:{items['FT-100_Datenblatt.pdf'].pk}"]["document"]
        self.assertSchema("document", pdf)
        self.assertEqual((pdf["kind"], pdf["products"][0]["sku"]), ("upload", "FT-100"))   # вид з контракту
        att = pdf["attachments"][0]
        r = self.client.get(att["url"], HTTP_AUTHORIZATION=f"Token {self.key.key}")     # файл читається на місці
        self.assertEqual((r.status_code, r.content), (200, PDF))
        xlsx = docs[f"document:{items['Preisliste.xlsx'].pk}"]["document"]
        self.assertEqual(xlsx["attachments"], [])                                       # текст витягла Minerva
        self.assertIn("FT-100 | 19.5", xlsx["text"])
        self.assertEqual(knowledge_sources.resolve(source, "../x"), "")                 # поза папкою — ні

    def test_rescan_only_changes_and_removals(self):
        path = self.write("a.txt", b"one")
        self.write("b.txt", b"two")
        source = self.source()
        knowledge_sources.scan(source)
        self.assertEqual(knowledge_sources.scan(source), {"created": 0, "updated": 0, "removed": 0, "skipped": 0})
        item = source.items.get(source_path="a.txt")
        item.file_sha256 = "sha256:" + "0" * 64
        item.save()
        with open(path, "wb") as fh:
            fh.write(b"changed content")
        os.utime(path, (time.time() + 10, time.time() + 10))
        os.remove(os.path.join(self.folder, "b.txt"))
        data, _ = self.documents()
        result = knowledge_sources.scan(source)
        self.assertEqual((result["updated"], result["removed"]), (1, 1))
        import hashlib
        self.assertEqual(source.items.get(source_path="a.txt").file_sha256,             # хеш нового вмісту
                         "sha256:" + hashlib.sha256(b"changed content").hexdigest())
        _, after = self.documents(data["next_after"])
        self.assertEqual(sorted(i["action"] for i in after.values()), ["delete", "upsert"])

    def test_too_large_and_disabled_source(self):
        self.write("big.pdf", PDF + b"x" * (1024 * 1024 + 10))
        self.write("small.pdf", PDF)
        source = self.source(max_file_mb=1)
        result = knowledge_sources.scan(source)
        self.assertEqual((result["created"], result["skipped"]), (1, 1))
        item = source.items.get()
        self.assertTrue(KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk).fed)
        source.enabled = False
        source.save()
        knowledge.touch_docs("document", [item.pk])
        self.assertFalse(KnowledgeDoc.objects.get(doc_type="document", obj_id=item.pk).fed)

    def test_source_deleted_items_tombstoned_files_kept(self):
        path = self.write("keep.pdf", PDF)
        source = self.source()
        knowledge_sources.scan(source)
        pk = source.items.get().pk
        source.delete()
        self.assertFalse(KnowledgeItem.objects.filter(pk=pk).exists())
        self.assertFalse(KnowledgeDoc.objects.get(doc_type="document", obj_id=pk).fed)
        self.assertTrue(os.path.isfile(path))                                          # файл користувача не чіпаємо

    def test_scan_error_is_recorded(self):
        source = self.source(path=os.path.join(self.root, "gone"))
        self.assertIn("error", knowledge_sources.scan(source))
        source.refresh_from_db()
        self.assertIn("Папки немає", source.last_error)


class WebsiteTests(SourcesBase):
    def test_sitemap_pages_and_removal(self):
        sitemap = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                   b"<url><loc>https://docs.example.com/a</loc></url><url><loc>https://docs.example.com/b</loc></url>"
                   b"<url><loc>https://other.example.com/c</loc></url></urlset>")
        pages = {"https://docs.example.com/sitemap.xml": sitemap,
                 "https://docs.example.com/a": b"<html><title>Seite A</title><body>Antenne 2,4 GHz</body></html>",
                 "https://docs.example.com/b": b"<html><title>Seite B</title><body>Kabel</body></html>"}

        def fake(url):
            return url, pages[url], "application/xml" if url.endswith(".xml") else "text/html"
        source = KnowledgeSource.objects.create(name="Docs", kind="website", url="https://docs.example.com/sitemap.xml",
                                                owner=self.user)
        with patch("rag_assistant.knowledge_items._download", side_effect=fake):
            result = knowledge_sources.scan(source)
        self.assertEqual(result["created"], 2, result)
        a = source.items.get(source_path="https://docs.example.com/a")
        self.assertEqual((a.title, a.url), ("Seite A", "https://docs.example.com/a"))
        self.assertIn("Antenne", a.text)
        pages[source.url] = sitemap.replace(b"<url><loc>https://docs.example.com/b</loc></url>", b"")
        with patch("rag_assistant.knowledge_items._download", side_effect=fake):
            self.assertEqual(knowledge_sources.scan(source)["removed"], 1)


class SourcesAdminTests(SourcesBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        inline = patch("rag_assistant.knowledge_sources._start_background", lambda fn: fn())
        inline.start()
        self.addCleanup(inline.stop)

    def form(self, **kw):
        data = {"name": "Doku", "kind": "folder", "path": self.folder, "url": "", "recursive": "on", "max_files": 2000,
                "max_file_mb": 25, "rescan_hours": 24, "enabled": "on", "visibility": "team", "link_products_by_sku": "on",
                "file_types_text": ""}
        data.update(kw)
        return data

    def test_add_validates_path_and_scans(self):
        self.write("x.txt", b"hello")
        r = self.client.post("/admin/rag_assistant/knowledgesource/add/", self.form(path="/etc"))
        self.assertContains(r, "Папка поза дозволеними")
        r = self.client.post("/admin/rag_assistant/knowledgesource/add/", self.form())
        self.assertEqual(r.status_code, 302, r.content[:400])
        source = KnowledgeSource.objects.get()
        self.assertEqual((source.owner, source.items.count()), (self.user, 1))        # перевірено одразу
        page = self.client.get(f"/admin/rag_assistant/knowledgesource/{source.pk}/change/")
        self.assertContains(page, "🔄 Перевірити зараз")
        self.assertContains(page, "MINERVA_KNOWLEDGE_ROOTS")                           # як підключити папку NAS
        self.assertEqual(self.client.post(f"/admin/rag_assistant/knowledgesource/{source.pk}/scan/").status_code, 302)
        self.assertEqual(self.client.get("/admin/rag_assistant/knowledgesource/").status_code, 200)
