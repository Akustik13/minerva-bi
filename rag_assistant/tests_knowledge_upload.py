"""«📂 Джерела знань»: файли з ПК у папку джерела, папка Minerva, сайт зі списку посилань, одна перевірка одночасно.

python manage.py test rag_assistant --settings=tabele.settings_test   (мережа підмінена)
"""
import os
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from rag_assistant import knowledge_sources
from rag_assistant.knowledge_items import ItemError
from rag_assistant.models import KnowledgeDoc, KnowledgeSource
from rag_assistant.tests_knowledge_sources import PDF, SourcesBase

ADD = "/admin/rag_assistant/knowledgesource/add/"


class UploadBase(SourcesBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        inline = patch("rag_assistant.knowledge_sources._start_background", lambda fn: fn())
        inline.start()
        self.addCleanup(inline.stop)

    def form(self, **kw):
        data = {"name": "", "kind": "folder", "path": "", "url": "", "follow_links": "on", "recursive": "on",
                "max_files": 2000, "max_file_mb": 25, "rescan_hours": 24, "enabled": "on", "visibility": "team",
                "link_products_by_sku": "on", "file_types_text": ""}
        data.update(kw)
        return data

    def upload(self, source, name, data=PDF, **kw):
        values = {"file": SimpleUploadedFile(name, data), "replace": "1"}
        values.update(kw)
        return self.client.post(f"/admin/rag_assistant/knowledgesource/{source.pk}/upload/", values).json()


class ManagedFolderTests(UploadBase):
    def test_new_folder_upload_scan_and_delete(self):
        from inventory.models import Product
        product = Product.objects.create(sku="FT-100", name="Filter")
        self.assertContains(self.client.post(ADD, self.form()), "Вкажіть назву")        # папку немає як назвати
        r = self.client.post(ADD, self.form(name="Handbücher"))
        source = KnowledgeSource.objects.get()
        self.assertRedirects(r, f"/admin/rag_assistant/knowledgesource/{source.pk}/change/")   # одразу — завантажити
        self.assertTrue(source.managed)
        self.assertEqual(source.path, os.path.realpath(os.path.join(self.media, "knowledge_sources", "Handbücher")))
        self.assertTrue(os.path.isdir(source.path))

        ok = self.upload(source, "FT-100 Datenblatt.pdf", relpath="Antennen/2026/FT-100 Datenblatt.pdf")
        self.assertEqual((ok["ok"], ok["status"], ok["path"]), (True, "added", "Antennen/2026/FT-100 Datenblatt.pdf"))
        self.assertTrue(os.path.isfile(os.path.join(source.path, "Antennen", "2026", "FT-100 Datenblatt.pdf")))
        self.assertEqual(self.upload(source, "a.txt", b"Montage", subfolder="Notizen")["path"], "Notizen/a.txt")
        self.assertEqual(self.upload(source, "a.txt", b"Montage neu", subfolder="Notizen", replace="0")["status"], "exists")
        self.assertEqual(self.upload(source, "a.txt", b"Montage neu", subfolder="Notizen")["status"], "replaced")
        with open(os.path.join(source.path, "Notizen", "a.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"Montage neu")
        for name, data, kw, why in [
            ("tool.exe", b"MZ", {}, "не беруться"),
            ("fake.pdf", b"not a pdf", {}, "не схожий"),
            ("x.pdf", PDF, {"relpath": "../../evil.pdf"}, "не бере"),
            ("x.pdf", PDF, {"relpath": ".git/x.pdf"}, "не бере"),
            ("~$lock.docx", b"PK\x03\x04", {}, "Прихований"),
            ("empty.txt", b"", {}, "Порожній"),
        ]:
            res = self.upload(source, name, data, **kw)
            self.assertFalse(res["ok"], name)
            self.assertIn(why, res["error"], name)
        self.assertFalse(os.path.exists(os.path.join(self.media, "evil.pdf")))
        self.assertEqual(sorted(n for n in os.listdir(source.path)), ["Antennen", "Notizen"])   # без .part-файлів

        r = self.client.post(f"/admin/rag_assistant/knowledgesource/{source.pk}/upload/", {"action": "done"})
        self.assertEqual(r.json(), {"ok": True, "scan": "started"})
        items = {i.source_path: i for i in source.items.all()}
        self.assertEqual(set(items), {"Antennen/2026/FT-100 Datenblatt.pdf", "Notizen/a.txt"})
        self.assertEqual(items["Antennen/2026/FT-100 Datenblatt.pdf"].product_id, product.pk)
        status = self.client.get(f"/admin/rag_assistant/knowledgesource/{source.pk}/status/").json()
        self.assertEqual((status["running"], status["documents"]), (False, 2))
        page = self.client.get(f"/admin/rag_assistant/knowledgesource/{source.pk}/change/")
        self.assertContains(page, "📤 Завантажити файли")
        self.assertContains(page, "📁 Завантажити папку")
        self.assertContains(page, "Antennen/2026/FT-100 Datenblatt.pdf")
        self.assertContains(page, 'class="ks-del"')

        pk = items["Notizen/a.txt"].pk
        r = self.client.post(f"/admin/rag_assistant/knowledgesource/{source.pk}/delete-file/", {"path": "Notizen/a.txt"})
        self.assertEqual(r.json(), {"ok": True, "removed": True})
        self.assertFalse(os.path.exists(os.path.join(source.path, "Notizen")))           # порожня підпапка теж
        self.assertFalse(source.items.filter(pk=pk).exists())
        self.assertFalse(KnowledgeDoc.objects.get(doc_type="document", obj_id=pk).fed)   # RAG прибере
        r = self.client.post(f"/admin/rag_assistant/knowledgesource/{source.pk}/delete-file/", {"path": "../x"})
        self.assertEqual(r.json(), {"ok": True, "removed": False})

    def test_existing_folder_upload_but_no_delete(self):
        source = self.source()
        self.assertEqual(self.upload(source, "manual.pdf")["status"], "added")
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "manual.pdf")))
        r = self.client.post(f"/admin/rag_assistant/knowledgesource/{source.pk}/delete-file/", {"path": "manual.pdf"})
        self.assertIn("лише в папці, яку створила Minerva", r.json()["error"])
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "manual.pdf")))         # файл користувача лишився
        self.assertNotContains(self.client.get(f"/admin/rag_assistant/knowledgesource/{source.pk}/change/"),
                               'class="ks-del"')
        self.assertIn("не беруться", self.upload(self.source(file_types=["pdf"], name="Nur PDF"), "a.txt", b"x")["error"])
        too_big = self.upload(self.source(max_file_mb=1, name="Klein"), "big.pdf", PDF + b"x" * (1024 * 1024))
        self.assertIn("більший за 1 МБ", too_big["error"])

    def test_private_source_only_for_owner(self):
        other = User.objects.create_user("kolega", "k@x.y", "p", is_staff=True)
        other.user_permissions.add(*Permission.objects.filter(content_type__app_label="rag_assistant"))
        other.profile.role = "admin"                    # модуль rag_assistant доступний (core ModuleAccessMiddleware)
        other.profile.save()
        mine = self.source(visibility="private", name="Privat")
        team = self.source(name="Team")
        self.client.force_login(other)
        self.assertEqual(self.client.get(f"/admin/rag_assistant/knowledgesource/{mine.pk}/status/").status_code, 403)
        self.assertEqual(self.client.post(f"/admin/rag_assistant/knowledgesource/{mine.pk}/upload/",
                                          {"file": SimpleUploadedFile("a.pdf", PDF)}).status_code, 403)
        self.assertEqual(self.upload(team, "a.pdf")["status"], "added")
        listing = self.client.get("/admin/rag_assistant/knowledgesource/")
        self.assertContains(listing, "Team")
        self.assertNotContains(listing, "Privat")


class ScanQueueTests(UploadBase):
    def test_one_scan_at_a_time_and_rescan_after_upload(self):
        self.write("a.txt", b"one")
        source = self.source()
        self.assertTrue(knowledge_sources._claim(source))
        self.assertFalse(knowledge_sources._claim(source))                               # інший потік / процес
        self.assertEqual(knowledge_sources.scan(source), {"error": "Перевірка вже йде", "busy": True})
        self.assertEqual(knowledge_sources.request_scan(source), "queued")              # після поточної
        source.refresh_from_db()
        self.assertTrue(source.rescan_requested)

        calls = []
        original = knowledge_sources._scan_once

        def once(obj):
            calls.append(1)
            if len(calls) == 1:                                                         # файл додали під час перевірки
                self.write("b.txt", b"two")
                KnowledgeSource.objects.filter(pk=obj.pk).update(rescan_requested=True)
            return original(obj)
        with patch("rag_assistant.knowledge_sources._scan_once", side_effect=once):
            knowledge_sources.scan(source, claimed=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(source.items.values_list("source_path", flat=True)), {"a.txt", "b.txt"})
        source.refresh_from_db()
        self.assertEqual((source.scan_started_at, source.rescan_requested), (None, False))
        self.assertTrue(knowledge_sources._claim(source))

    def test_cron_picks_requested_rescan(self):
        source = self.source()
        KnowledgeSource.objects.filter(pk=source.pk).update(last_scan_at=timezone.now(), rescan_requested=True)
        self.write("late.txt", b"x")
        self.assertEqual(knowledge_sources.scan_due(), [source.pk])
        self.assertTrue(source.items.filter(source_path="late.txt").exists())
        self.assertEqual(knowledge_sources.scan_due(), [])                               # свіже — ні


class WebsiteLinksTests(UploadBase):
    pages = {
        "https://docs.example.com/": b'<html><title>Start</title><body>Home <a href="/a">A</a></body></html>',
        "https://docs.example.com/a": b"<html><title>Seite A</title><body>Antenne 2,4 GHz</body></html>",
        "https://shop.example.com/datenblatt.pdf": PDF,
        "https://shop.example.com/p/1": b"<html><title>Produkt</title><body>Kabel</body></html>",
    }

    def fake(self, url):
        if url not in self.pages:
            raise ItemError("Сторінка відповіла HTTP 503")
        return url, self.pages[url], "application/pdf" if url.endswith(".pdf") else "text/html"

    def test_only_listed_links(self):
        with patch("rag_assistant.knowledge_items._download", side_effect=self.fake):   # перевірка — одразу після «Зберегти»
            r = self.client.post(ADD, self.form(
                kind="website", follow_links="",
                url="https://shop.example.com/datenblatt.pdf\n  https://shop.example.com/p/1  \nhttps://shop.example.com/p/1"))
        self.assertEqual(r.status_code, 302, r.content[:400])
        source = KnowledgeSource.objects.get()
        self.assertEqual((source.name, source.url), ("shop.example.com",                 # назва — з адреси
                                                     "https://shop.example.com/datenblatt.pdf\nhttps://shop.example.com/p/1"))
        self.assertEqual(source.items.count(), 2)
        pdf = source.items.get(source_path="https://shop.example.com/datenblatt.pdf")
        self.assertTrue(pdf.file and pdf.file_sha256)                                    # PDF — файлом
        self.assertIn("Kabel", source.items.get(source_path="https://shop.example.com/p/1").text)

    def test_follow_links_and_site_down(self):
        source = KnowledgeSource.objects.create(name="Docs", kind="website", owner=self.user,
                                                url="https://docs.example.com/\nhttps://shop.example.com/p/1")
        with patch("rag_assistant.knowledge_items._download", side_effect=self.fake):
            self.assertEqual(knowledge_sources.scan(source)["created"], 3)
        del self.pages["https://docs.example.com/"]                                      # сайт тимчасово не відповідає
        with patch("rag_assistant.knowledge_items._download", side_effect=self.fake):
            result = knowledge_sources.scan(source)
        self.assertEqual(result["removed"], 0, result)                                   # його сторінки лишились
        source.refresh_from_db()
        self.assertIn("не відповіли посилань 1", source.last_scan_result)
        self.assertEqual(source.items.count(), 3)
        source.url = "https://docs.example.com/"
        source.save()
        with patch("rag_assistant.knowledge_items._download", side_effect=self.fake):
            self.assertIn("Не відповіло жодне посилання", knowledge_sources.scan(source)["error"])
        self.assertEqual(source.items.count(), 3)                                        # нічого не прибрано

    def test_form_checks_links(self):
        self.assertContains(self.client.post(ADD, self.form(kind="website", url="")), "хоча б одне посилання")
        self.assertContains(self.client.post(ADD, self.form(kind="website", url="docs.example.com\nftp://x")),
                            "Не схоже на адресу")
        self.assertFalse(KnowledgeSource.objects.exists())
