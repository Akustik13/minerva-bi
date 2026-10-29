"""⏱ «Записати в askDANTE з листа»: python manage.py test worktime.tests_from_mail --settings=tabele.settings_test"""
from unittest import mock

from django.urls import reverse

from email_assistant.models import EmailMessage
from worktime.models import ProjectMap
from worktime.tests_mail_time import ME, ProjectBase


class FromMailTests(ProjectBase):
    def setUp(self):
        super().setUp()
        ProjectMap.objects.create(project=self.project, dante_project=self.dp3)
        self.sent = EmailMessage.objects.filter(folder=EmailMessage.FOLDER_SENT, from_email=ME).first()
        EmailMessage.objects.filter(pk=self.sent.pk).update(
            subject="Neue Testboards", body_text="Hallo,\nder angepasste Schaltplan ist fertig.\n"
                                                 "HOST_IRQ wurde von PC6 auf PB3 verlegt.",
            attachments=[{"name": "Schaltplan_v2.pdf"}, {"name": "BOM.xlsx"}])
        self.sent.refresh_from_db()

    def test_menu_leads_to_project_work_form_prefilled(self):
        r = self.client.get(reverse("admin:worktime_workday_from_mail", args=[self.sent.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"proj={self.project.pk}", r["Location"])
        self.assertIn(f"msg={self.sent.pk}", r["Location"])
        page = self.client.get(r["Location"])
        html = page.content.decode()
        self.assertIn('<details id="work" style="margin-top:8px" open>', html)
        self.assertIn(f'name="msg" value="{self.sent.pk}"', html)
        self.assertIn("Schaltplan_v2.pdf", html)
        self.assertIn('value="Neue Testboards"', html)
        stop = page.context["work_mail"]["stop"]
        self.assertTrue(stop.endswith(("0", "5")))                              # до 5 хв, «з» — на 30 хв раніше
        inbox = self.client.get("/email/")
        self.assertContains(inbox, "Записати в askDANTE з листа")

    def test_mail_without_project_or_category(self):
        lone = self.msg(cat=False, subject="Hallo", sender="someone@else.example")
        r = self.client.get(reverse("admin:worktime_workday_from_mail", args=[lone.pk]), HTTP_REFERER="/email/")
        self.assertEqual(r["Location"], "/email/")

    def test_ai_description_from_this_mail_with_sent_files(self):
        seen = {}

        def fake_ai(prompt, **kw):
            seen["prompt"] = prompt
            return "Schaltplan angepasst\n- HOST_IRQ auf PB3\n- Gesendet: Schaltplan_v2.pdf, BOM.xlsx"
        with mock.patch("email_assistant.ai_helper._call_ai_direct", side_effect=fake_ai):
            r = self.client.post(reverse("admin:worktime_workday_record_describe"), {
                "u": self.du.pk, "date": self.sent.sent_at.date().isoformat(), "project": self.dp3.pk,
                "start": "09:00", "stop": "09:30", "msg": self.sent.pk}).json()
        self.assertTrue(r["ok"], r)
        self.assertIn("[THIS E-MAIL", seen["prompt"])
        self.assertIn("Attachments: Schaltplan_v2.pdf, BOM.xlsx", seen["prompt"])
        self.assertIn("«- Gesendet: …»", seen["prompt"])
        self.assertIn("Gesendet: Schaltplan_v2.pdf", r["text"])


class CustomLogoTests(ProjectBase):
    """🖼 Свій значок відправника: адреса → домен → автоматичний; «буква» — без логотипа."""

    def setUp(self):
        super().setUp()
        import shutil
        import tempfile

        from django.test import override_settings
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        over = override_settings(MEDIA_ROOT=media)
        over.enable()
        self.addCleanup(over.disable)

    def test_set_serve_and_priority(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from email_assistant import logos
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        r = self.client.post("/email/logo-set/", {"key": "example.com", "action": "upload",
                                                  "file": SimpleUploadedFile("me.png", png)}).json()
        self.assertEqual((r["ok"], r["state"]), (True, "image"))
        key, ver = logos.logo_for(ME)
        self.assertEqual(key, "example.com")
        self.assertTrue(ver)
        got = self.client.get("/email/logo/example.com/")
        self.assertEqual((got.status_code, got["Content-Type"]), (200, "image/png"))
        self.client.post("/email/logo-set/", {"key": ME, "action": "letter"})
        self.assertEqual(logos.logo_for(ME), ("", ""))                          # адреса важливіша за домен
        self.assertEqual(self.client.get(f"/email/logo/{ME}/").status_code, 404)
        self.client.post("/email/logo-set/", {"key": ME, "action": "auto"})
        self.assertEqual(logos.logo_for(ME)[0], "example.com")
        bad = self.client.post("/email/logo-set/", {"key": "example.com", "action": "upload",
                                                    "file": SimpleUploadedFile("x.svg", b"<svg/>")}).json()
        self.assertFalse(bad["ok"])
        self.assertContains(self.client.get("/email/"), "Значок відправника…")

    def test_standard_icon_emoji_on_colour(self):
        from email_assistant import logos
        r = self.client.post("/email/logo-set/", {"key": "example.com", "action": "icon", "icon": "🏭",
                                                  "color": "#0f9d58"}).json()
        self.assertEqual((r["ok"], r["state"]), (True, "icon"))
        key, ver = logos.logo_for(ME)
        self.assertEqual(key, "example.com")
        self.assertTrue(ver.startswith("i"))                                   # кружок без білої рамки
        got = self.client.get("/email/logo/example.com/")
        self.assertEqual((got.status_code, got["Content-Type"]), (200, "image/svg+xml"))
        svg = got.content.decode()
        self.assertIn("🏭", svg)
        self.assertIn('fill="#0f9d58"', svg)
        bad = self.client.post("/email/logo-set/", {"key": "example.com", "action": "icon", "icon": "<b>x</b>"}).json()
        self.assertFalse(bad["ok"])
        self.assertTrue(self.client.post("/email/logo-set/", {"key": "example.com", "action": "icon", "icon": "🦊",
                                                              "color": "red"}).json()["ok"])   # свій емодзі, колір — типовий
        self.assertIn(f'fill="{logos.STANDARD_COLORS[0]}"', self.client.get("/email/logo/example.com/").content.decode())
        page = self.client.get("/email/").content.decode()
        self.assertIn('id="em-logo-icons"', page)
        self.assertIn("emLogoIcon(this.textContent)\">🏢</button>", page)


class MailTimeActivityTests(FromMailTests.__mro__[1]):
    """«✉️ Час на пошту»: askDANTE вимагає вид роботи — рядки проєктів без нього позначено, кнопка їх не надішле."""

    def setUp(self):
        super().setUp()
        from worktime.models import DanteActivity, DanteSettings
        ProjectMap.objects.create(project=self.project, dante_project=self.dp3)
        DanteActivity.objects.create(dante_id="190", name="Kommunikation")
        DanteActivity.objects.create(dante_id="191", name="Messungen")
        DanteSettings.objects.update(activity_required=True, default_activity=None)

    def test_rows_without_work_type_are_marked(self):
        sent = EmailMessage.objects.filter(folder=EmailMessage.FOLDER_SENT, from_email=ME).first()
        day = sent.sent_at.date().isoformat()
        r = self.client.get(reverse("admin:worktime_workday_mailtime"),
                            {"u": self.du.pk, "proj": self.project.pk, "from": day, "to": day})
        html = r.content.decode()
        self.assertIn('data-noact="1"', html)
        self.assertIn("вид роботи не вибрано — виберіть унизу", html)
        self.assertIn("[data-noact]:checked", html)                           # кнопка не надішле такі рядки


class PreviewLogoTests(CustomLogoTests):
    def test_open_letter_header_shows_the_same_icon(self):
        self.client.post("/email/logo-set/", {"key": ME, "action": "icon", "icon": "🏭", "color": "#0f9d58"})
        sent = EmailMessage.objects.filter(from_email=ME).exclude(thread=None).first()
        page = self.client.get(f"/email/thread/{sent.thread_id}/preview/").content.decode()
        self.assertIn(f'class="em-av-logo em-av-icon" src="/email/logo/{ME}/?v=i', page)
