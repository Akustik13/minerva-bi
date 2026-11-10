"""🔗 Сторони проєкту (хто кому постачає) і 📤 експорт діаграм: PDF, Excel, лист з файлами.
python manage.py test projects --settings=tabele.settings_test"""
import io
import json
import unittest
from datetime import timedelta
from importlib.util import find_spec

from django.urls import reverse

from email_assistant.models import EmailDraft
from inventory.models import PurchaseOrder, PurchaseOrderLine
from projects import export, gantt, parties, plan, schedule
from projects.models import ProjectLink, ProjectParty, ProjectStep
from projects.tests_plan import PlanBase

P = ProjectParty
HAS_PDF = find_spec("reportlab") is not None          # у requirements.txt; локальний venv може бути без нього


def png_bytes(w=400, h=160):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


class PartiesBase(PlanBase):
    def po(self, days=-3, status=PurchaseOrder.Status.ORDERED):
        po = PurchaseOrder.objects.create(supplier=self.supplier, status=status,
                                          expected_date=self.today + timedelta(days=days))
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.connector, qty_ordered=500)
        ProjectLink.objects.create(project=self.project, purchase_order=po, status=ProjectLink.CONFIRMED,
                                   source=ProjectLink.MANUAL)
        return po


class PartiesTests(PartiesBase):
    def test_suggest_builds_the_chain_from_project_data(self):
        self.po()
        n = parties.suggest(self.project)
        by_role = {p.role: p for p in self.project.parties.all()}
        self.assertEqual(n, len(by_role))
        we, sup = by_role[P.WE], by_role[P.SUPPLIER]
        self.assertEqual((sup.name, sup.delivers_to_id, sup.what), ("Parts Ltd", we.pk, "CN-MHF4"))
        if self.project.customer_id:
            self.assertEqual(we.delivers_to_id, by_role[P.CUSTOMER].pk)              # ми → замовник
        self.assertEqual(parties.suggest(self.project), 0)                         # удруге — нічого нового

    def test_statuses_and_drawing(self):
        self.po(days=-5)
        parties.suggest(self.project)
        sup = self.project.parties.get(role=P.SUPPLIER)
        stats = parties.statuses(self.project, list(self.project.parties.all()))
        self.assertEqual(parties.worst(stats[sup.pk]), "bad")                       # закупівлю чекали 5 днів тому
        svg = parties.render(list(self.project.parties.all()), stats, can_change=True)
        self.assertIn("Parts Ltd", svg)
        self.assertIn(f'data-party="{sup.pk}"', svg)
        self.assertIn("pjy-edge", svg)
        self.assertNotIn("data-party", parties.render(list(self.project.parties.all()), stats, can_change=False))

    def test_branches_cycles_and_delete(self):
        we = parties.save(self.project, {"role": P.WE, "name": "Sevskiy"})
        maker = parties.save(self.project, {"role": P.MANUFACTURER, "name": "PCB Fab", "delivers_to": we.pk})
        a = parties.save(self.project, {"role": P.SUPPLIER, "name": "Chips A", "delivers_to": maker.pk})
        parties.save(self.project, {"role": P.SUPPLIER, "name": "Housing B", "delivers_to": maker.pk})
        placed, depth, _rows = parties.layout(list(self.project.parties.all()))
        self.assertEqual(depth, 2)                                                  # гілка: двоє → виробник → ми
        with self.assertRaises(parties.PartyError):                                 # ми не можемо віддавати своєму
            parties.save(self.project, {"id": we.pk, "role": P.WE, "name": "Sevskiy", "delivers_to": a.pk})
        parties.delete(self.project, maker.pk)
        a.refresh_from_db()
        self.assertEqual(a.delivers_to_id, we.pk)                                   # постачальники віддають далі

    def test_step_belongs_to_a_party(self):
        sup = parties.save(self.project, {"role": P.MANUFACTURER, "name": "PCB Fab"})
        s = plan.save_step(self.project, {"title": "Виготовити плати", "duration": 10, "party": sup.pk,
                                          "side": "we"}, user=self.user)
        self.assertEqual((s.party_id, s.side), (sup.pk, ProjectStep.SUPPLIER))     # «Хто» — за роллю сторони
        p = schedule.compute(self.project, {}, self.today)
        self.assertIn("PCB Fab", gantt.render(self.project, p, self.today))
        self.assertEqual(plan.editor_data(self.project, p)["parties"][0]["id"], sup.pk)

    def test_views_and_cockpit_tab(self):
        self.po()
        r = self.client.post(reverse("projects:party_suggest", args=[self.project.pk]), "{}",
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("🔗 Сторони · ", r.json()["html"])
        self.assertIn("pjy-data", r.json()["html"])
        r = self.client.post(reverse("projects:party", args=[self.project.pk]), json.dumps({"name": ""}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)


class ExportTests(PartiesBase):
    def setUp(self):
        super().setUp()
        self.step("Зразки", days=5, start=self.today)

    def send(self, **data):
        from django.core.files.uploadedfile import SimpleUploadedFile
        body = {"what": "plan", "lang": "de", "formats": "pdf", "table": "1", **data}
        if "image" not in body:
            body["image"] = SimpleUploadedFile("diagram.png", png_bytes(), content_type="image/png")
        return self.client.post(reverse("projects:export", args=[self.project.pk]), body)

    @unittest.skipUnless(HAS_PDF, "reportlab не встановлено")
    def test_pdf_download(self):
        r = self.send()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertIn(f'{self.project.code}_Projektplan_', r["Content-Disposition"])

    def test_excel_download(self):
        r = self.send(formats="xlsx", image="")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"PK"))                                # xlsx = zip
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertIn("Зразки", [ws.cell(row=i, column=2).value for i in range(1, 12)])

    def test_send_makes_a_draft_with_files(self):
        r = self.send(formats="pdf,xlsx,png" if HAS_PDF else "xlsx,png", send="1")
        self.assertEqual(r.status_code, 200)
        url = r.json()["compose"]
        draft = EmailDraft.objects.get(pk=int(url.rsplit("=", 1)[1]))
        self.assertEqual(draft.account, self.account)
        self.assertEqual(sorted(a["name"].rsplit(".", 1)[1] for a in draft.attachments),
                         ["pdf", "png", "xlsx"] if HAS_PDF else ["png", "xlsx"])
        self.assertIn("Projektplan", draft.subject)

    def test_bad_image_and_no_mailbox(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.send(image=SimpleUploadedFile("x.png", b"<svg/>", content_type="image/png"))
        self.assertEqual(r.status_code, 400)
        self.account.delete()
        r = self.send(send="1", formats="png")
        self.assertEqual(r.status_code, 400)
        self.assertIn("немає вашої поштової скриньки", r.json()["error"])

    @unittest.skipUnless(HAS_PDF, "reportlab не встановлено")
    def test_pdf_strips_symbols_the_font_lacks(self):
        data = export.pdf(self.project, "chain", "uk", png_bytes(), table=False)
        self.assertTrue(data.startswith(b"%PDF"))
