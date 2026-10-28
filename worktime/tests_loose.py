"""✉️ Час на пошту: листи без askDANTE (показати, віднести до категорії з проєктом askDANTE, 🙈 не рахувати) і
новий проєкт у клієнта askDANTE. python manage.py test worktime --settings=tabele.settings_test"""
from unittest import mock

from django.urls import reverse

from email_assistant.models import EmailMessage, MailCategory
from worktime import mail_time, services
from worktime.models import DanteProject, MailCategoryTime, MailTimeIgnore
from worktime.tests_mail_time import MailTimeBase, MON


class LooseTests(MailTimeBase):
    def test_loose_listed_and_ignore_scopes(self):
        a = self.msg(cat=False, sender="info@hrpcba.example", subject="Customer-Supplied Materials")
        b = self.msg(cat=False, sender="info@hrpcba.example", subject="AW: Customer-Supplied Materials")
        self.msg(cat=False, sender="noreply@askdante.example", subject="Fehlende Zeiteinträge")
        day = mail_time.estimate(self.du, MON, MON)[MON]
        self.assertEqual(([m["subject"] for m in day["loose"]], day["unmapped"]),
                         (["Customer-Supplied Materials", "AW: Customer-Supplied Materials", "Fehlende Zeiteinträge"], 3))
        self.assertEqual(day["loose"][0]["contact"], "info@hrpcba.example")
        mail_time.ignore(self.user, [a.pk], MailTimeIgnore.SENDER)   # усе від співрозмовника
        day = mail_time.estimate(self.du, MON, MON)[MON]
        self.assertEqual(([m["pk"] for m in day["ignored"]], day["unmapped"]), ([a.pk, b.pk], 1))
        mail_time.unignore(self.user, [b.pk])
        self.assertEqual(mail_time.estimate(self.du, MON, MON)[MON]["unmapped"], 3)

    def test_assign_to_category_with_dante_project(self):
        m = self.msg(cat=False, sender="info@hrpcba.example", subject="Materials")
        dp = DanteProject.objects.create(dante_id="90", name="Leiterplatten")
        cat = MailCategory.objects.create(name="HRPCBA")
        r = mail_time.assign(self.user, [m.pk], cat, "domain", dp)
        m.refresh_from_db()
        self.assertEqual((m.work_category, MailCategoryTime.objects.get(category=cat).dante_project), (cat, dp))
        self.assertEqual(r["changed"], 1)
        [row] = [x for x in mail_time.estimate(self.du, MON, MON)[MON]["rows"] if x["dp"] == dp]
        self.assertEqual(row["received"], 1)

    def test_page_filter_and_actions(self):
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        m = self.msg(cat=False, sender="info@hrpcba.example", subject="Customer-Supplied Materials")
        url = reverse("admin:worktime_workday_mailtime")
        q = {"u": self.du.pk, "from": MON.isoformat(), "to": MON.isoformat()}
        self.assertNotContains(self.client.get(url, q), "Customer-Supplied Materials")   # лише askDANTE
        page = self.client.get(url, {**q, "show": "all"})
        self.assertContains(page, "Customer-Supplied Materials")
        self.assertContains(page, f'name="mail" value="{m.pk}"')
        self.client.post(url, {**q, "show": "all", "action": "assign", "mail": [m.pk], "wcat": "new",
                               "wcat_name": "HRPCBA", "scope": "sender"})
        m.refresh_from_db()
        self.assertEqual(m.work_category.name, "HRPCBA")
        r = self.client.post(url, {**q, "show": "all", "action": "ignore", "mail": [m.pk], "ignore_scope": "thread"})
        self.assertIn("show=all", r["Location"])
        self.assertContains(self.client.get(url, {**q, "show": "ignored"}), "Customer-Supplied Materials")


class CustomerProjectTests(MailTimeBase):
    def test_new_project_for_dante_customer(self):
        DanteProject.objects.create(dante_id="300", name="Antenna design", customer_name="Eigene Projekte Sevskiy GmbH",
                                    customer_ref="7")
        self.assertEqual(services.dante_customers(), [("7", "Eigene Projekte Sevskiy GmbH")])
        self.fake.create_project = mock.Mock(return_value={"id": "301", "name": "DigiKey"})
        dp = services.create_target("c:7", "DigiKey")
        self.fake.create_project.assert_called_once_with("DigiKey", customer_id="7")
        self.assertEqual((dp.customer_name, dp.customer_ref, dp.parent), ("Eigene Projekte Sevskiy GmbH", "7", None))
        self.assertEqual(services.create_target("c:7", "digikey").pk, dp.pk)   # без дубля
        self.assertIsNone(services.create_target("", "x"))
        with self.assertRaises(services.DanteError):
            services.create_target("c:999", "x")

    def test_client_sends_customer(self):
        from worktime.client import DanteClient
        c = DanteClient(self.st)
        resp = mock.Mock(status_code=201)
        resp.json.return_value = {"id": 5, "name": "DigiKey"}
        with mock.patch.object(DanteClient, "_call", return_value=resp) as call:
            c.create_project("DigiKey", customer_id="7")
        self.assertEqual(call.call_args.kwargs["body"]["customer"], {"id": "7"})
