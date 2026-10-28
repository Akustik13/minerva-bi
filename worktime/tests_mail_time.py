"""Час на пошту → askDANTE: python manage.py test worktime --settings=tabele.settings_test (askDANTE імітується)."""
import datetime
from datetime import timezone as dt_tz
from unittest import mock

from django.urls import reverse

from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, EmailThread, MailCategory
from worktime import mail_time
from worktime.admin import _activity_label
from worktime.models import Booking, DanteProject, MailCategoryTime, MailTimeEntry, ProjectMap, WorkDay
from worktime.tests import MON, Base

ME = "anna@example.com"
TUE = MON + datetime.timedelta(days=1)


class MailTimeBase(Base):
    def setUp(self):
        super().setUp()
        from core.shared_cache import cache
        cache.clear()   # описи AI кешуються спільно — між тестами не переносити
        self.st.mail_time = True
        self.st.save()
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x", smtp_username=ME,
            smtp_password="x")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        self.dp = DanteProject.objects.create(dante_id="77", name="DigiKey Marketplace")
        self.cat = MailCategory.objects.create(name="DigiKey", senders="digikey")
        self.ct = MailCategoryTime.objects.create(category=self.cat, dante_project=self.dp, reply_minutes=10,
                                                  read_minutes=3)
        self.n = 0
        today = mock.patch("worktime.services.local_today", return_value=TUE + datetime.timedelta(days=1))
        today.start()
        self.addCleanup(today.stop)

    def msg(self, day=MON, folder=EmailMessage.FOLDER_INBOX, read=True, cat=True, thread=None, **kw):
        self.n += 1
        return EmailMessage.objects.create(
            account=self.account, imap_uid=self.n, imap_folder_name=f"F{self.n}", folder=folder,
            subject=kw.get("subject", f"Order {self.n}"), from_email=kw.get("sender", "orders@digikey.com"),
            to_emails=kw.get("to", [ME]), is_read=read, thread=thread,
            sent_at=datetime.datetime.combine(day, datetime.time(9, self.n), tzinfo=dt_tz.utc),
            work_category=self.cat if cat else None)


class EstimateTests(MailTimeBase):
    def test_norms_rounding_and_unread_skipped(self):
        self.msg(folder=EmailMessage.FOLDER_SENT)   # наша відповідь 10
        self.msg()
        self.msg()                                 # 2 прочитані × 3
        self.msg(read=False)                       # непрочитаний — не рахується
        self.msg(cat=False, sender="x@other.example")   # без категорії
        [r] = mail_time.estimate(self.du, MON, MON)[MON]["rows"]
        self.assertEqual((r["replies"], r["received"], r["raw"], r["minutes"]), (1, 2, 16, 30))
        self.assertEqual(mail_time.estimate(self.du, MON, MON)[MON]["unmapped"], 1)

    def test_own_copy_in_inbox_is_reply_and_counted_once(self):
        dk = ["orders@digikey.com"]
        a = self.msg(sender=ME, to=dk, subject="Re: Order")                 # копія нашої відповіді у Вхідних
        b = self.msg(folder=EmailMessage.FOLDER_SENT, sender=ME, to=dk, subject="Re: Order")   # і в «Надісланих»
        EmailMessage.objects.filter(pk__in=[a.pk, b.pk]).update(message_id="<same@x>")
        [r] = mail_time.estimate(self.du, MON, MON)[MON]["rows"]
        self.assertEqual((r["replies"], r["received"]), (1, 0))

    def test_minerva_project_beats_category(self):
        from projects.models import Project, ProjectLink
        project = Project.objects.create(name="ACME Antenne")
        dp2 = DanteProject.objects.create(dante_id="88", name="ACME")
        ProjectMap.objects.create(project=project, dante_project=dp2)
        thread = EmailThread.objects.create(account=self.account, subject="ACME")
        ProjectLink.objects.create(project=project, thread=thread, status=ProjectLink.CONFIRMED)
        self.msg(thread=thread)
        self.msg()
        rows = {r["dp"].pk: r for r in mail_time.estimate(self.du, MON, MON)[MON]["rows"]}
        self.assertEqual(set(rows), {self.dp.pk, dp2.pk})
        self.assertTrue(rows[dp2.pk]["label"].startswith("📁 "))

    def test_book_writes_difference_only(self):
        self.msg(folder=EmailMessage.FOLDER_SENT)
        b = mail_time.book(self.du, MON, self.dp.pk, user=self.user)
        self.assertEqual((b.status, b.duration, b.source), (Booking.SENT, 15, Booking.MAIL))
        call = self.fake.calls[-1]
        self.assertEqual((call["duration"], call["projectData"]["projectId"]), (15, "77"))
        self.assertEqual(call["projectData"]["comment"], "E-Mails DigiKey: Order 1 (1 Antwort)")   # без емодзі
        with self.assertRaises(mail_time.DanteError):
            mail_time.book(self.du, MON, self.dp.pk)   # уже записано
        for _ in range(2):
            self.msg(folder=EmailMessage.FOLDER_SENT)   # ще 20 хв → 30 разом, дописати 15
        b2 = mail_time.book(self.du, MON, self.dp.pk)
        self.assertEqual(b2.duration, 15)
        self.assertEqual(MailTimeEntry.objects.get().minutes_booked, 30)

    def test_auto_only_auto_categories_and_skips_leave(self):
        self.msg(day=MON, folder=EmailMessage.FOLDER_SENT)
        self.msg(day=TUE, folder=EmailMessage.FOLDER_SENT)
        self.assertIn("записано 0", mail_time.auto(self.st, today=TUE + datetime.timedelta(days=1)))
        self.ct.auto = True
        self.ct.save()
        WorkDay.objects.create(dante_user=self.du, date=TUE, planned_state=WorkDay.LEAVE)
        text = mail_time.auto(self.st, today=TUE + datetime.timedelta(days=1))
        self.assertIn("записано 1", text)
        self.assertIn("пропущено (відсутність) 1", text)
        self.assertEqual([c["date"] for c in self.fake.calls], [MON.isoformat()])
        self.st.refresh_from_db()
        self.assertFalse(mail_time.due(self.st, datetime.datetime.combine(
            TUE + datetime.timedelta(days=1), datetime.time(23), tzinfo=dt_tz.utc)))

    def test_order_minutes_from_audit_log(self):
        from django.contrib.contenttypes.models import ContentType
        from core.models import AuditLog
        from sales.models import SalesOrder, SalesSource
        src = SalesSource.objects.create(slug="digikey-mp", name="DigiKey")
        self.cat.sources.add(src)
        self.ct.order_minutes = 5
        self.ct.save()
        order = SalesOrder.objects.create(source="digikey-mp", order_number="99823894", order_date=MON)
        ct = ContentType.objects.get_for_model(SalesOrder)
        for minute in (0, 5):   # дві зміни одного замовлення — одне замовлення
            AuditLog.objects.create(user=self.user, action=AuditLog.Action.UPDATE, content_type=ct,
                                    object_id=str(order.pk), object_repr="x")
        AuditLog.objects.filter(object_id=str(order.pk)).update(
            timestamp=datetime.datetime.combine(MON, datetime.time(10), tzinfo=dt_tz.utc))
        [r] = mail_time.estimate(self.du, MON, MON)[MON]["rows"]
        self.assertEqual((r["orders"], r["raw"]), (1, 5))


class PagesTests(MailTimeBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)

    def test_page_and_book(self):
        self.msg(folder=EmailMessage.FOLDER_SENT)
        url = reverse("admin:worktime_workday_mailtime")
        page = self.client.get(url, {"u": self.du.pk, "from": MON.isoformat(), "to": TUE.isoformat()})
        self.assertContains(page, "DigiKey Marketplace")
        self.assertContains(page, f'value="{MON.isoformat()}|{self.dp.pk}"')
        self.client.post(url, {"u": self.du.pk, "from": MON.isoformat(), "to": TUE.isoformat(),
                               "pick": [f"{MON.isoformat()}|{self.dp.pk}"]})
        self.assertEqual(MailTimeEntry.objects.get().minutes_booked, 15)
        page = self.client.get(url, {"u": self.du.pk, "from": MON.isoformat(), "to": TUE.isoformat()})
        self.assertContains(page, "✓ 0:15")

    def test_category_card_has_dante_block(self):
        page = self.client.get(reverse("admin:email_assistant_mailcategory_change", args=[self.cat.pk]))
        self.assertContains(page, "Час на ці листи в askDANTE")
        self.assertContains(page, "DigiKey Marketplace")

    def test_activity_label(self):
        t = datetime.datetime(2026, 10, 28, 8, 28)
        self.assertEqual(_activity_label((t, t, 1)), "08:28 · 1 дія")
        self.assertEqual(_activity_label((t, t.replace(hour=17, minute=40), 52)), "08:28–17:40 · 52 дії")
        self.assertEqual(_activity_label((t, t.replace(hour=9), 5)), "08:28–09:28 · 5 дій")


class CategoryFilterTests(MailTimeBase):
    """«⏱ Перенести в askDANTE» з категорії: сторінка лише з її проєктом askDANTE за період."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        self.dp2 = DanteProject.objects.create(dante_id="78", name="Webshop Kunden")
        self.shop = MailCategory.objects.create(name="Shop", subject_words="bestellung", order=1)
        MailCategoryTime.objects.create(category=self.shop, dante_project=self.dp2, reply_minutes=10, read_minutes=3)
        self.url = reverse("admin:worktime_workday_mailtime")

    def test_only_rows_of_the_category_project(self):
        self.msg(folder=EmailMessage.FOLDER_SENT)
        m = self.msg(subject="Bestellung 5", sender="kunde@shop.example", cat=False)
        EmailMessage.objects.filter(pk=m.pk).update(work_category=self.shop)
        args = {"u": self.du.pk, "from": MON.isoformat(), "to": TUE.isoformat()}
        everything = self.client.get(self.url, args)
        self.assertContains(everything, f'value="{MON.isoformat()}|{self.dp2.pk}"')
        page = self.client.get(self.url, {**args, "cat": self.shop.pk})
        self.assertContains(page, f'value="{MON.isoformat()}|{self.dp2.pk}"')
        self.assertNotContains(page, f'value="{MON.isoformat()}|{self.dp.pk}"')
        self.assertContains(page, f'name="cat" value="{self.shop.pk}"')
        self.assertContains(page, "Webshop Kunden</b>")
        r = self.client.post(self.url, {**args, "cat": self.shop.pk, "pick": [f"{MON.isoformat()}|{self.dp2.pk}"]})
        self.assertIn(f"cat={self.shop.pk}", r["Location"])
        self.assertEqual(MailTimeEntry.objects.get().dante_project, self.dp2)

    def test_category_without_project_explains(self):
        other = MailCategory.objects.create(name="Zoll", order=2)
        page = self.client.get(self.url, {"cat": other.pk})
        self.assertContains(page, "ще немає проєкту askDANTE")

    def test_links_and_month_presets(self):
        page = self.client.get(reverse("admin:email_assistant_mailcategory_changelist"))
        self.assertContains(page, f"?cat={self.cat.pk}&amp;from=")
        self.assertContains(page, "перенести в askDANTE")
        page = self.client.get(self.url, {"preset": "last_month", "cat": self.cat.pk})
        first = (TUE + datetime.timedelta(days=1)).replace(day=1)
        self.assertEqual(page.context["date_to"], first - datetime.timedelta(days=1))
        self.assertEqual(page.context["date_from"].day, 1)
        setup = self.client.get(reverse("admin:worktime_danteproject_categories"))
        self.assertContains(setup, f"?cat={self.cat.pk}&amp;from=")
