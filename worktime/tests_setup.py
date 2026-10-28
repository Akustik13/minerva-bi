"""🗂 Категорії листів → askDANTE: пропозиції груп, підпроєкти askDANTE, замовлення в описі.
python manage.py test worktime --settings=tabele.settings_test"""
import datetime
import json
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from email_assistant import work_categories
from email_assistant.models import EmailAccount, EmailMessage, MailCategory
from sales.models import SalesOrder, SalesSource
from worktime import mail_time, services
from worktime.models import DanteProject, MailCategoryTime
from worktime.tests import MON, Base
from worktime.tests_mail_time import MailTimeBase


class SuggestBase(Base):
    def setUp(self):
        super().setUp()
        self.acc = EmailAccount.objects.create(user=self.user, email_address="anna@example.com", imap_username="a",
                                               imap_password="x", smtp_username="a", smtp_password="x")
        SalesSource.objects.create(slug="digikey", name="DigiKey")
        for i in range(6):
            SalesOrder.objects.create(source="digikey", order_number=f"10112197{i}", order_date=MON)
        self.n = 0

    def mail(self, sender, subject, body="", category=""):
        self.n += 1
        m = EmailMessage.objects.create(account=self.acc, imap_uid=self.n, imap_folder_name=f"F{self.n}",
                                        subject=subject, from_email=sender, to_emails=["anna@example.com"],
                                        body_text=body, is_read=True, sent_at=timezone.now())
        if category:
            EmailMessage.objects.filter(pk=m.pk).update(category=category)
        return m


class SuggestTests(SuggestBase):
    def test_groups_from_orders_and_service_domains(self):
        for i in range(6):
            self.mail("marketplace@digikey.com", f"Your order #10112197{i} has shipped", category="service")
        for i in range(5):
            self.mail(f"noreply{i}@ups.com", "UPS Update", category="service")
        for i in range(3):
            self.mail("x@rare.example", "selten", category="service")   # менше 5 — не пропонується
        groups = {g["key"]: g for g in work_categories.suggest(30)}
        self.assertEqual(set(groups), {"digikey", "ups"})
        self.assertEqual((groups["digikey"]["sources"], groups["digikey"]["senders"], groups["digikey"]["mails"],
                          groups["digikey"]["name"]), (["digikey"], ["digikey"], 6, "DigiKey"))
        cat = work_categories.create_from_suggestion("DigiKey", ["digikey"], ["digikey"])
        self.assertEqual((cat.lines("senders"), list(cat.sources.values_list("slug", flat=True))),
                         (["digikey"], ["digikey"]))
        self.assertNotIn("digikey", {g["key"] for g in work_categories.suggest(30)})   # уже є категорія

    def test_page_flow_creates_category_and_dante_item(self):
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        for i in range(6):
            self.mail("marketplace@digikey.com", f"Order 10112197{i}", category="service")
        url = reverse("admin:worktime_danteproject_categories")
        page = self.client.get(url, {"scan": "1", "days": 30})
        self.assertContains(page, 'value="DigiKey"')
        with mock.patch("email_assistant.work_categories.start_recompute"):
            self.client.post(url, {"action": "create", "days": 30, "pick": ["digikey"], "n_digikey": "DigiKey",
                                   "s_digikey": json.dumps({"senders": ["digikey"], "sources": ["digikey"]})})
        cat = MailCategory.objects.get(name="DigiKey")
        intern = DanteProject.objects.create(dante_id="500", name="Intern")
        self.fake.create_project_item = mock.Mock(return_value={"id": "501", "name": "DigiKey"})
        self.client.post(url, {"action": "save", f"dp_{cat.pk}": "", f"new_{cat.pk}": f"p:{intern.pk}",
                               f"sub_{cat.pk}": "DigiKey", f"reply_{cat.pk}": "8", f"read_{cat.pk}": "2",
                               f"order_{cat.pk}": "0"})
        self.fake.create_project_item.assert_called_once_with("DigiKey", "500", parent_is_item=False)
        ct = MailCategoryTime.objects.get(category=cat)
        self.assertEqual((ct.dante_project.name, ct.dante_project.parent, ct.reply_minutes), ("DigiKey", intern, 8))


class DanteItemTests(Base):
    def test_create_dante_no_duplicates_and_nesting(self):
        intern = DanteProject.objects.create(dante_id="500", name="Intern")
        self.fake.create_project_item = mock.Mock(return_value={"id": "501", "name": "DigiKey"})
        a = services.create_dante("DigiKey", intern)
        b = services.create_dante("digikey", intern)
        self.assertEqual((a.pk, a.parent, self.fake.create_project_item.call_count), (b.pk, intern, 1))
        self.fake.create_project_item = mock.Mock(return_value={"id": "502", "name": "Orders"})
        services.create_dante("Orders", a)
        self.fake.create_project_item.assert_called_once_with("Orders", "501", parent_is_item=True)


class OrderRefsTests(MailTimeBase):
    def test_orders_in_description_with_time(self):
        SalesOrder.objects.create(source="digikey", order_number="101121975", order_date=MON)
        SalesOrder.objects.create(source="digikey", order_number="101121980", order_date=MON,
                                  tracking_number="1Z0R6B300411366943")
        self.msg(subject="Order 101121975", folder=EmailMessage.FOLDER_SENT)
        self.msg(subject="Re: Order 101121975")
        self.msg(subject="UPS 1Z0R6B300411366943")
        [r] = mail_time.estimate(self.du, MON, MON)[MON]["rows"]
        self.assertEqual(r["refs"], {"101121975": 2, "101121980": 1})
        text = mail_time.default_comment(r, "de")   # день 0:30 на 3 листи: 2 листи → 0:20, 1 → 0:10
        self.assertTrue(text.startswith("E-Mails DigiKey: Aufträge: 101121975 ~0:20, 101121980 ~0:10 · "), text)


class PeriodTests(SuggestBase):
    def test_suggest_only_inside_from_to(self):
        old = timezone.now() - datetime.timedelta(days=20)
        for i in range(6):
            m = self.mail("marketplace@digikey.com", f"Order 10112197{i}", category="service")
            EmailMessage.objects.filter(pk=m.pk).update(sent_at=old)
        today = timezone.localdate()
        self.assertEqual(work_categories.suggest(date_from=today - datetime.timedelta(days=7), date_to=today), [])
        found = work_categories.suggest(date_from=today - datetime.timedelta(days=30), date_to=today)
        self.assertEqual([g["key"] for g in found], ["digikey"])
