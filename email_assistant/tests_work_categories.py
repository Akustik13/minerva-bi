"""Свої категорії листів (DigiKey, магазин…): python manage.py test email_assistant --settings=tabele.settings_test"""
import datetime
from datetime import timezone as dt_tz

from django.contrib.auth import get_user_model
from django.test import TestCase

from email_assistant import work_categories
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, MailCategory
from email_assistant.sync import save_message

ME = "me@sevskiy.example"


class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", ME, "x", is_staff=True, is_superuser=True)
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX")
        self.shop_box = EmailAccount.objects.create(
            user=self.user, email_address="shop@sevskiy.example", imap_username="shop", imap_password="x",
            smtp_username="shop", smtp_password="x")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        self.uid = 0
        self.dk = MailCategory.objects.create(name="DigiKey", icon="🟥", senders="digikey\n", order=0)
        self.shop = MailCategory.objects.create(name="Магазин", icon="🛒", subject_words="bestellung", order=1)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()

    def mail(self, sender, subject="Hallo", body="Text", account=None, to=None, folder="INBOX", msg_id=None,
             reply_to=""):
        self.uid += 1
        return save_message(account or self.account, {
            "uid": self.uid, "message_id": msg_id or f"<w{self.uid}@x>", "in_reply_to": reply_to,
            "references": [reply_to] if reply_to else [], "subject": subject, "from_email": sender,
            "from_name": "", "to_emails": to or [ME], "cc_emails": [], "body_text": body, "body_html": "",
            "attachments": [], "is_read": True,
            "sent_at": datetime.datetime(2026, 10, 20, 9, self.uid % 60, tzinfo=dt_tz.utc)}, folder)


class RulesTests(Base):
    def test_sender_part_domain_and_address_rules(self):
        self.assertTrue(work_categories.sender_matches("orders@digikey.de", "digikey"))
        self.assertTrue(work_categories.sender_matches("a@eu.digikey.com", "@digikey.com"))
        self.assertFalse(work_categories.sender_matches("a@notdigikey.com", "@digikey.com"))
        self.assertTrue(work_categories.sender_matches("sales@digikey.com", "sales@digikey.com"))
        self.assertFalse(work_categories.sender_matches("other@digikey.com", "sales@digikey.com"))

    def test_new_mail_gets_category_by_sender_subject_account_and_order(self):
        m1 = self.mail("marketplace@digikey.com")
        m2 = self.mail("kunde@gmail.com", subject="Ihre Bestellung 4711")
        m3 = self.mail("someone@else.example", account=self.shop_box)
        m4 = self.mail("x@other.example")
        for m in (m1, m2, m3, m4):
            m.refresh_from_db()
        self.assertEqual(m1.work_category, self.dk)
        self.assertEqual(m2.work_category, self.shop)
        self.assertIsNone(m3.work_category)
        self.shop.accounts.add(self.shop_box)
        self.assertEqual(work_categories.match(m3), self.shop)
        self.assertIsNone(m4.work_category)

    def test_order_source_rule(self):
        from sales.models import SalesOrder, SalesSource
        src = SalesSource.objects.create(slug="sevskiy-shop", name="Webshop")
        SalesOrder.objects.create(source="sevskiy-shop", order_number="PO-31728-001",
                                  order_date=datetime.date(2026, 10, 1))
        self.shop.sources.add(src)
        m = self.mail("buyer@gmail.com", subject="Frage", body="zu Auftrag PO-31728-001 bitte")
        m.refresh_from_db()
        self.assertEqual(m.work_category, self.shop)

    def test_reply_in_thread_inherits_and_our_reply_to_digikey(self):
        first = self.mail("marketplace@digikey.com", subject="Order 99823894", msg_id="<dk1@x>")
        reply = self.mail(ME, subject="Re: Order 99823894", to=["marketplace@digikey.com"], folder="Sent",
                          reply_to="<dk1@x>")
        reply.refresh_from_db()
        self.assertEqual(reply.thread_id, first.thread_id)
        self.assertEqual(reply.work_category, self.dk)

    def test_set_for_message_adds_sender_and_recomputes(self):
        a = self.mail("anna@rfparts.example")
        b = self.mail("anna@rfparts.example", subject="Noch was")
        r = work_categories.set_for_message(a, self.dk, "domain")
        self.assertEqual(r["key"], "@rfparts.example")
        self.dk.refresh_from_db()
        self.assertIn("@rfparts.example", self.dk.lines("senders"))
        b.refresh_from_db()
        self.assertEqual(b.work_category, self.dk)
        work_categories.set_for_message(a, None, "domain")
        self.dk.refresh_from_db()
        self.assertNotIn("@rfparts.example", self.dk.lines("senders"))
        b.refresh_from_db()
        self.assertIsNone(b.work_category)

    def test_thread_only(self):
        a = self.mail("eva@partner.example")
        work_categories.set_for_message(a, self.shop, "thread")
        a.refresh_from_db()
        self.assertEqual(a.work_category, self.shop)
        self.assertEqual(a.thread.work_category, self.shop)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lines("senders"), [])

    def test_recompute_after_rule_change(self):
        m = self.mail("info@mouser.example")
        self.dk.senders += "\n@mouser.example"
        self.dk.save()
        n = work_categories.recompute(EmailMessage.objects.all())
        m.refresh_from_db()
        self.assertEqual((n, m.work_category), (1, self.dk))


class InboxTests(Base):
    def test_filter_tag_and_menu(self):
        self.client.force_login(self.user)
        dk = self.mail("marketplace@digikey.com", subject="DK order")
        self.mail("x@other.example", subject="Other mail")
        page = self.client.get("/email/", {"wcat": self.dk.pk})
        self.assertContains(page, "DK order")
        self.assertNotContains(page, "Other mail")
        self.assertContains(page, f'data-wcat="{self.dk.pk}"')   # категорія ліворуч — і ціль для перетягування
        self.assertContains(page, "Без своєї категорії")
        self.assertContains(page, "ctxSetWorkCategory")
        r = self.client.post(f"/email/message/{dk.pk}/work-category/",
                             {"category": "new", "name": "Митниця", "scope": "thread"},
                             content_type="application/json").json()
        self.assertTrue(r["ok"])
        self.assertTrue(MailCategory.objects.filter(name="Митниця").exists())
        dk.refresh_from_db()
        self.assertEqual(dk.work_category.name, "Митниця")

    def test_admin_pages(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/admin/email_assistant/mailcategory/"), "DigiKey")
        page = self.client.get(f"/admin/email_assistant/mailcategory/{self.dk.pk}/change/")
        self.assertContains(page, "askDANTE")


class NavTests(Base):
    def test_left_column_filters_and_no_category(self):
        self.client.force_login(self.user)
        self.mail("marketplace@digikey.com", subject="DK order")
        self.mail("x@other.example", subject="Other mail")
        page = self.client.get("/email/", {"wcat": 0})
        self.assertContains(page, "Other mail")
        self.assertNotContains(page, "DK order")
        self.assertContains(page, "em-chip")
        page = self.client.get("/email/")
        self.assertNotContains(page, 'class="em-p2-filter-row em-chips"')   # без фільтрів — без чипів
        self.assertNotContains(page, 'id="em-cat-sel"')                     # старих випадаючих списків немає
