"""Етикетки кабелів з шаблону — у пам'яті; файл у базі етикеток (можливо, виправлений вручну) не
перезаписується без явного «💾 Зберегти» й підтвердження. Завантаження, видалення й генерація — з CSRF.

    python manage.py test labels_app --settings=tabele.settings_test
"""
import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from labels_app import mark
from labels_app import views as label_views
from shipping.services.dymo_label_service import DymoLabelService

PN = "CA-MHF1-MC1.13-175-MHF4"
HAND_EDITED = "<DieCutLabel><Text>P/N: CA-MHF1-MC1.13-175-MHF4 — виправлено вручну</Text></DieCutLabel>"
SALES_TEMPLATES = Path(__file__).resolve().parent.parent / "sales" / "templates" / "admin" / "sales"


class CableLabelTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(label_views, "LABELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_user("staff", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def generate(self, **body):
        return self.client.post("/labels/generate/", json.dumps({"part_no": PN, "qty": 20, **body}),
                                content_type="application/json")

    def texts(self, xml):
        return [t.text for t in ET.fromstring(xml).iter("Text")]

    def test_print_from_template_writes_no_file(self):
        r = self.generate()
        d = r.json()
        self.assertTrue(d["ok"], d)
        self.assertFalse(d["saved"])
        self.assertEqual(list(self.dir.iterdir()), [])                     # у базі етикеток нічого не з'явилось
        self.assertEqual(d["download_url"], f"/labels/cable/{PN}/?qty=20")
        r = self.client.get(d["download_url"])
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'filename="{PN}.dymo"', r["Content-Disposition"])
        texts = self.texts(r.content)
        self.assertIn("Qty: 20 PCS ", texts)
        self.assertIn("Compatible with: U.FL-1st end, MHF4-2nd end", texts)
        marks = [o for o in ET.fromstring(r.content).iter("TextObject") if o.findtext("Name") == mark.NAME]
        self.assertEqual(next(marks[0].iter("Text")).text[:2], "A ")      # позначка «з шаблону»
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_hand_edited_file_is_not_overwritten(self):
        (self.dir / f"{PN}.dymo").write_text(HAND_EDITED, encoding="utf-8")
        self.assertTrue(self.generate().json()["ok"])                       # друк з шаблону — файл не чіпає
        r = self.generate(save=True)                                       # «💾 Зберегти» без підтвердження
        self.assertEqual(r.status_code, 409)
        self.assertEqual((r.json()["exists"], r.json()["filename"]), (True, f"{PN}.dymo"))
        self.assertEqual((self.dir / f"{PN}.dymo").read_text(encoding="utf-8"), HAND_EDITED)
        r = self.generate(save=True, overwrite=True)                       # людина підтвердила заміну
        self.assertTrue(r.json()["saved"] and r.json()["replaced"])
        self.assertIn("Qty: 20 PCS ", self.texts((self.dir / f"{PN}.dymo").read_bytes()))

    def test_overwrite_replaces_the_existing_alt_file_instead_of_shadowing_it(self):
        (self.dir / f"{PN} Alt.dymo").write_text(HAND_EDITED, encoding="utf-8")
        self.assertEqual(self.generate(save=True).status_code, 409)
        self.assertEqual(self.generate(save=True, overwrite=True).json()["filename"], f"{PN} Alt.dymo")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [f"{PN} Alt.dymo"])

    def test_save_creates_a_new_file(self):
        d = self.generate(save=True).json()
        self.assertEqual((d["saved"], d["replaced"], d["filename"]), (True, False, f"{PN}.dymo"))
        self.assertTrue((self.dir / f"{PN}.dymo").exists())

    def test_bad_part_number(self):
        r = self.client.post("/labels/generate/", json.dumps({"part_no": "CA-NOPE"}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/labels/cable/CA-NOPE/").status_code, 400)

    def test_changes_need_csrf_token(self):
        (self.dir / "AN1.dymo").write_text("<x/>", encoding="utf-8")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        upload = SimpleUploadedFile("AN2.dymo", b"<x/>")
        self.assertEqual(client.post("/labels/upload/", {"labels": upload}).status_code, 403)
        self.assertEqual(client.post("/labels/delete/AN1/").status_code, 403)
        self.assertEqual(client.post("/labels/generate/", json.dumps({"part_no": PN, "save": True}),
                                     content_type="application/json").status_code, 403)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["AN1.dymo"])
        client.get("/labels/")                                              # сторінка ставить cookie csrftoken
        token = client.cookies["csrftoken"].value
        upload = SimpleUploadedFile("AN2.dymo", b"<x/>")
        r = client.post("/labels/upload/", {"labels": upload}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.json()["results"][0]["status"], "created")
        self.assertTrue(client.post("/labels/delete/AN1/", HTTP_X_CSRFTOKEN=token).json()["ok"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["AN2.dymo"])

    def test_staff_only(self):
        guest = User.objects.create_user("guest", password="x")
        User.objects.filter(pk=guest.pk).update(is_staff=False)            # core.signals робить новий акаунт staff
        guest.refresh_from_db()
        client = Client()
        client.force_login(guest)
        self.assertEqual(client.post("/labels/delete/AN1/").status_code, 302)      # на вхід в адмінку
        self.assertEqual(client.get(f"/labels/cable/{PN}/").status_code, 302)

    def test_service_never_overwrites_silently(self):
        out = self.dir / f"{PN}.dymo"
        out.write_text(HAND_EDITED, encoding="utf-8")
        with self.assertRaises(FileExistsError):
            DymoLabelService.generate(PN, 5, output_path=out)
        self.assertEqual(out.read_text(encoding="utf-8"), HAND_EDITED)
        DymoLabelService.generate(PN, 5, output_path=out, overwrite=True)
        self.assertIn("Qty: 5 PCS ", self.texts(out.read_bytes()))
        with mock.patch.object(DymoLabelService, "generate") as write:
            items = DymoLabelService.generate_for_order([{"part_no": PN, "qty": 3}, {"part_no": "AN1", "qty": 1}])
        write.assert_not_called()
        self.assertEqual(items, [{"part_no": PN, "qty": 3, "download_url": f"/labels/cable/{PN}/?qty=3"}])

    def test_order_list_prints_an_uploaded_cable_file_as_is(self):
        html = (SALES_TEMPLATES / "salesorder_changelist.html").read_text(encoding="utf-8")
        self.assertNotIn("svCaGenThenOpen(", html)                         # раніше перезаписував щойно завантажений
        self.assertNotIn("fetch('/labels/generate/',{method:'POST',headers:{'Content-Type':'application/json'}}", html)
