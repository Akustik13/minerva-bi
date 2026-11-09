"""📎 PDF рахунку до замовлення DigiKey при підтвердженні відправки (bots/services/dk_vat.py). DigiKey імітується.

    python manage.py test bots.tests_dk_vat --settings=tabele.settings_test
"""
import datetime
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from bots.models import DigiKeyConfig
from bots.services import digikey, dk_vat
from sales.models import SalesOrder

PDF = b"%PDF-1.4\n%test invoice\n"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self.ok, self._body = status, 200 <= status < 300, body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


ORDER = {"supplierId": "sup-uuid", "orderState": "ShippingInProgress", "orderDetails": [
    {"orderDetailId": "d1", "supplierSku": "FT220407", "quantity": 10},
], "shippingTrackingInfoList": []}


class ShipWithInvoiceTests(TestCase):
    """ship_marketplace_order(vat_document=…): завантажити, передати supplierVATObjectId, перечитати замовлення."""

    def run_ship(self, puts, after=None, before=ORDER, upload=None, **kw):
        sent, uploads = [], []

        def put(url, headers=None, json=None, timeout=None):
            sent.append(json)
            return puts.pop(0)

        def up(config, data, name, supplier_id=None):
            uploads.append((data, name, supplier_id))
            return upload or {"ok": True, "file_id": "F1", "message": "ok"}

        orders = [before, after if after is not None else before]
        with mock.patch.object(digikey, "get_marketplace_token", return_value="t"), \
                mock.patch.object(digikey, "_fetch_marketplace_order", side_effect=lambda *a: orders.pop(0)), \
                mock.patch.object(digikey, "upload_vat_invoice", side_effect=up), \
                mock.patch("requests.put", side_effect=put):
            kw.setdefault("vat_document", (PDF, "Invoice_10240.pdf"))
            res = digikey.ship_marketplace_order(mock.Mock(use_sandbox=True), "1001", tracking_number="1Z", **kw)
        return res, sent, uploads

    def test_linked(self):
        after = dict(ORDER, vatFileId="F1", vatFileName="Invoice_10240.pdf")
        res, sent, uploads = self.run_ship([Resp()], after=after)
        self.assertTrue(res["ok"])
        self.assertEqual(uploads, [(PDF, "Invoice_10240.pdf", "sup-uuid")])
        self.assertEqual(sent[0]["shippingTracking"][0]["supplierVATObjectId"], "F1")
        self.assertEqual(sent[0]["vatFileId"], "F1")
        self.assertEqual(res["vat"]["state"], dk_vat.LINKED)
        level, text = dk_vat.message(res["vat"], "1001")
        self.assertEqual(level, "success")
        self.assertIn("на сайті нічого робити не треба", text)

    def test_linked_by_tracking_object(self):
        after = dict(ORDER, shippingTrackingInfoList=[{"shippingTrackingNumber": "1Z", "supplierVATObjectId": "F1"}])
        res, _, _ = self.run_ship([Resp()], after=after)
        self.assertEqual(res["vat"]["state"], dk_vat.LINKED)

    def test_accepted_but_not_shown(self):
        res, sent, _ = self.run_ship([Resp()])
        self.assertTrue(res["ok"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(res["vat"]["state"], dk_vat.IGNORED)
        level, text = dk_vat.message(res["vat"], "1001")
        self.assertEqual(level, "warning")
        self.assertIn("supplier.digikey.com", text)

    def test_messages_translate_whole(self):
        """🌐 перекладач підставляє значення {n} як є: у повідомленнях лише дані, і кожне є в комплекті перекладів."""
        import re
        from core.ui_text import bundled
        from core.ui_text.text import edges
        cyr = re.compile("[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]")
        pats = sorted(((src, tr["en"]) for src, tr in bundled.load().items() if "{0}" in src and tr.get("en")),
                      key=lambda p: -len(p[0]))             # як mv_translate.js: довші шаблони першими
        for st in (dk_vat.LINKED, dk_vat.IGNORED, dk_vat.REJECTED, dk_vat.UPLOAD_FAILED, dk_vat.ALREADY,
                   dk_vat.UNKNOWN, dk_vat.NO_PDF):
            _, text = dk_vat.message({"state": st, "name": "Invoice_1.pdf", "detail": "Bad Request"}, "1001")
            core = edges(text)[1]
            out = None
            for src, dst in pats:
                rx = "^" + "(.+?)".join(re.escape(part) for part in re.split(r"\{\d+\}", src)) + "$"
                m = re.match(rx, core)
                if m:
                    out = dst
                    for i, v in enumerate(m.groups()):
                        out = out.replace("{%d}" % i, v)
                    break
            self.assertIsNotNone(out, text)
            self.assertIsNone(cyr.search(out), out)

    def test_rejected_field_retries_without_it(self):
        res, sent, _ = self.run_ship([Resp(400, {"title": "Unknown field supplierVATObjectId"}), Resp()])
        self.assertTrue(res["ok"])
        self.assertEqual(len(sent), 2)
        self.assertIn("supplierVATObjectId", sent[0]["shippingTracking"][0])
        self.assertNotIn("supplierVATObjectId", sent[1]["shippingTracking"][0])
        self.assertEqual((sent[0].get("vatFileId"), sent[1].get("vatFileId")), ("F1", None))
        self.assertEqual(res["vat"]["state"], dk_vat.REJECTED)
        self.assertIn("Unknown field", res["vat"]["detail"])

    def test_quantity_error_is_not_retried(self):
        res, sent, _ = self.run_ship([Resp(400, {"detail": "Invalid Shipped Quantity"})])
        self.assertFalse(res["ok"])
        self.assertEqual(len(sent), 1)
        self.assertIn("Invalid Shipped Quantity", res["message"])

    def test_invoice_already_on_digikey(self):
        before = dict(ORDER, vatFileId="OLD", vatFileName="Rechnung.pdf")
        res, sent, uploads = self.run_ship([Resp()], before=before)
        self.assertEqual(uploads, [])
        self.assertNotIn("supplierVATObjectId", sent[0]["shippingTracking"][0])
        self.assertEqual(res["vat"]["state"], dk_vat.ALREADY)
        self.assertEqual(dk_vat.message(res["vat"], "1001")[0], "info")

    def test_upload_failed_still_ships(self):
        res, sent, _ = self.run_ship([Resp()], upload={"ok": False, "message": "❌ 400: bad file"})
        self.assertTrue(res["ok"])
        self.assertNotIn("supplierVATObjectId", sent[0]["shippingTracking"][0])
        self.assertEqual(res["vat"]["state"], dk_vat.UPLOAD_FAILED)
        self.assertIn("bad file", res["vat"]["detail"])

    def test_without_document_nothing_changes(self):
        res, sent, uploads = self.run_ship([Resp()], vat_document=None)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["vat"])
        self.assertEqual(uploads, [])
        self.assertNotIn("supplierVATObjectId", sent[0]["shippingTracking"][0])

    def test_parcels_get_the_invoice_on_every_tracking(self):
        after = dict(ORDER, vatFileId="F1")
        res, sent, _ = self.run_ship([Resp()], after=after, shipments=[
            {"tracking": "1Z1", "parts_by_sku": {"FT220407": 4}},
            {"tracking": "1Z2", "parts_by_sku": {"FT220407": 6}}])
        self.assertTrue(res["ok"])
        self.assertEqual([e["supplierVATObjectId"] for e in sent[0]["shippingTracking"]], ["F1", "F1"])
        self.assertEqual(res["vat"]["state"], dk_vat.LINKED)


class MediaBase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        ctx = override_settings(MEDIA_ROOT=Path(self.media))
        ctx.enable()
        self.addCleanup(ctx.disable)
        self.order = SalesOrder.objects.create(order_number="1001", source="digikey", client="R&D GmbH",
                                               addr_country="DE")

    def invoice(self, data=PDF, number="10240"):
        import os
        from shipping.models import Invoice
        os.makedirs(os.path.join(self.media, "invoices"), exist_ok=True)
        name = f"invoices/Invoice_{number}.pdf"
        if data is not None:
            with open(os.path.join(self.media, name), "wb") as fh:
                fh.write(data)
        return Invoice.objects.create(digikey_order_no="1001", invoice_number=number,
                                      order_date=datetime.date(2026, 11, 1), shipped_to_company="R&D GmbH",
                                      pdf_file=name)


class InvoicePdfTests(MediaBase):
    def test_pdf_of_the_order(self):
        self.invoice()
        self.assertEqual(dk_vat.invoice_pdf(self.order), (PDF, "Invoice_10240.pdf"))
        self.assertEqual(dk_vat.pdf_name(self.order), "Invoice_10240.pdf")

    def test_not_a_pdf_or_missing(self):
        self.invoice(data=b"PK\x03\x04 docx")
        self.assertIsNone(dk_vat.invoice_pdf(self.order))
        self.invoice(data=None, number="10241")
        self.assertIsNone(dk_vat.invoice_pdf(self.order, dk_vat.invoice_for(self.order)))

    def test_no_invoice(self):
        self.assertIsNone(dk_vat.invoice_pdf(self.order))
        self.assertTrue(dk_vat.is_eu(self.order))


class ConfirmTests(MediaBase):
    """Форма /bots/digikey/ship/<pk>/: PDF з «Інвойси DigiKey» → ship_marketplace_order, повідомлення, панель."""

    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.cfg = DigiKeyConfig.get()
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.calls = []

    def fake_ship(self, vat):
        def ship(config, order_id, **kw):
            self.calls.append(kw)
            return {"ok": True, "message": "✅ ok", "raw": {}, "parts": [], "remaining": 0, "vat": vat}
        return ship

    def post(self, vat=None, **data):
        data.setdefault("tracking_number", "1Z999")
        with mock.patch("bots.services.digikey.ship_marketplace_order", side_effect=self.fake_ship(vat)):
            return self.client.post(f"/bots/digikey/ship/{self.order.pk}/", data)

    def texts(self, r):
        from django.contrib.messages import get_messages
        return [str(m) for m in get_messages(r.wsgi_request)]

    def test_eu_order_sends_invoice_pdf(self):
        self.invoice()
        vat = {"state": dk_vat.LINKED, "file_id": "F1", "name": "Invoice_10240.pdf", "detail": ""}
        r = self.post(vat=vat)
        self.assertEqual(self.calls[0]["vat_document"], (PDF, "Invoice_10240.pdf"))
        self.assertTrue(any("прикріплено до замовлення 1001" in t for t in self.texts(r)))
        self.cfg.refresh_from_db()
        self.assertEqual((self.cfg.vat_link_last["state"], self.cfg.vat_link_last["order"]), (dk_vat.LINKED, "1001"))

    def test_eu_order_without_pdf_says_so(self):
        r = self.post()
        self.assertIsNone(self.calls[0]["vat_document"])
        self.assertTrue(any("немає PDF рахунку" in t for t in self.texts(r)))
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.vat_link_last, {})

    def test_switched_off_or_outside_eu(self):
        self.invoice()
        self.cfg.vat_attach_eu = False
        self.cfg.save()
        self.post()
        self.assertIsNone(self.calls[-1]["vat_document"])
        self.cfg.vat_attach_eu = True
        self.cfg.save()
        SalesOrder.objects.filter(pk=self.order.pk).update(addr_country="US")
        self.post()
        self.assertIsNone(self.calls[-1]["vat_document"])

    def test_file_from_the_form_wins(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.invoice()
        self.post(vat_invoice_file=SimpleUploadedFile("Rechnung.pdf", PDF, content_type="application/pdf"))
        self.assertEqual(self.calls[0]["vat_document"], (PDF, "Rechnung.pdf"))

    def test_form_and_settings_show_what_happens(self):
        self.invoice()
        r = self.client.get(f"/bots/digikey/ship/{self.order.pk}/")
        self.assertContains(r, "Без файлу тут Minerva сама прикріпить")
        self.assertContains(r, "Invoice_10240.pdf")
        dk_vat.remember(self.cfg, {"state": dk_vat.IGNORED, "file_id": "F1", "name": "Invoice_10240.pdf"}, "1001")
        r = self.client.get(f"/admin/bots/digikeyconfig/{self.cfg.pk}/change/")
        self.assertContains(r, "Прикріплювати PDF рахунку до замовлення (ЄС)")
        self.assertContains(r, "рахунку в замовленні не показує")
        from shipping.admin import ShipmentAdmin
        self.assertIn("PDF «Invoice_10240.pdf» Minerva прикріпить", ShipmentAdmin._dk_invoice_text(self.order))
