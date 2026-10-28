"""Календар без сміття і спам з пам'яттю: python manage.py test calendar_app --settings=tabele.settings_test"""
import datetime
from datetime import timezone as dt_tz
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from calendar_app import blocks, deadlines
from calendar_app.models import CalendarEvent, CalendarSenderBlock, FoundDeadline, MeetingInvite
from calendar_app.tests_invites import ME, ics
from email_assistant import spam
from email_assistant.models import EmailAccount, EmailMessage, EmailRule, EmailSettings
from email_assistant.sync import save_message


class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", ME, "x", is_staff=True, is_superuser=True)
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        self.uid = 0
        moved = mock.patch("email_assistant.spam.move_on_server")   # без IMAP
        self.moved = moved.start()
        self.addCleanup(moved.stop)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()

    def mail(self, sender="max@acme.example", body="Hallo", subject="Termin", attachments=None, name="Max"):
        self.uid += 1
        return save_message(self.account, {
            "uid": self.uid, "message_id": f"<b{self.uid}@x>", "in_reply_to": "", "references": [],
            "subject": subject, "from_email": sender, "from_name": name, "to_emails": [ME], "cc_emails": [],
            "body_text": body, "body_html": "", "attachments": attachments or [], "is_read": True,
            "sent_at": datetime.datetime(2026, 10, 20, 9, 0, tzinfo=dt_tz.utc)}, "INBOX")

    def event(self, msg, title="⏰ Liefertermin", days=3):
        return CalendarEvent.objects.create(user=self.user, title=title, email_message=msg,
                                            event_type=CalendarEvent.TYPE_DEADLINE,
                                            start_at=datetime.datetime(2026, 11, days, 9, tzinfo=dt_tz.utc))

    def invite_mail(self, sender="max@acme.example", uid="m-1@outlook"):
        return self.mail(sender, body=ics(uid=uid).replace("max@acme.example", sender), subject="Einladung")


class SpamTests(Base):
    def test_sender_key_address_for_freemail_domain_otherwise(self):
        self.assertEqual(spam.sender_key("Bob <Bob@GMAIL.com>"), "bob@gmail.com")
        self.assertEqual(spam.sender_key("x@spam-shop.example"), "@spam-shop.example")
        self.assertEqual(spam.sender_key(ME), "")   # свою адресу спамом не позначаємо
        from crm.models import Customer
        Customer.objects.create(name="ACME", email="buyer@acme.example")
        self.assertEqual(spam.sender_key("max@acme.example"), "max@acme.example")   # домен клієнта — лише адреса

    def test_mark_remembers_sweeps_and_cleans_calendar(self):
        first = self.mail("a@spam-shop.example")
        other = self.mail("b@spam-shop.example")
        keep = self.mail("max@acme.example")
        ev = self.event(other)
        result = spam.mark(self.account, [first])
        self.assertEqual(result["keys"], ["@spam-shop.example"])
        self.assertEqual(result["more"], 1)          # другий лист того ж домену теж у Спам
        self.assertEqual(result["calendar"], 1)      # його термін прибрано з календаря
        self.assertFalse(CalendarEvent.objects.filter(pk=ev.pk).exists())
        self.assertTrue(EmailMessage.objects.get(pk=other.pk).is_spam)
        self.assertFalse(EmailMessage.objects.get(pk=keep.pk).is_spam)
        self.assertTrue(EmailRule.objects.filter(condition_value="@spam-shop.example",
                                                 action=EmailRule.ACTION_MARK_SPAM).exists())
        self.assertTrue(CalendarSenderBlock.objects.filter(user=self.user, key="@spam-shop.example",
                                                           reason=blocks.SPAM).exists())
        self.assertTrue(self.moved.called)           # і на сервері

    def test_new_mail_from_spammer_goes_to_spam_before_signals(self):
        spam.remember(self.account, [EmailMessage(from_email="x@spam-shop.example")])
        msg = self.invite_mail("x@spam-shop.example")
        msg.refresh_from_db()
        self.assertTrue(msg.is_spam)
        self.assertEqual(msg.folder, EmailMessage.FOLDER_SPAM)
        self.assertFalse(MeetingInvite.objects.exists())   # запрошення від спамера не з'являється
        self.assertTrue(self.moved.called)

    def test_old_broad_freemail_rule_is_ignored(self):
        EmailRule.objects.create(account=self.account, name="Спам: @gmail.com", condition_value="@gmail.com",
                                 action=EmailRule.ACTION_MARK_SPAM)
        msg = self.mail("friend@gmail.com")
        msg.refresh_from_db()
        self.assertFalse(msg.is_spam)

    def test_unmark_forgets_rule_and_spam_block(self):
        msg = self.mail("a@spam-shop.example")
        spam.mark(self.account, [msg])
        spam.unmark(self.account, [EmailMessage.objects.get(pk=msg.pk)])
        self.assertFalse(EmailRule.objects.filter(condition_value="@spam-shop.example").exists())
        self.assertFalse(CalendarSenderBlock.objects.exists())
        self.assertFalse(EmailMessage.objects.get(pk=msg.pk).is_spam)

    def test_toggle_view_reports_more(self):
        self.client.force_login(self.user)
        a, _ = self.mail("a@spam-shop.example"), self.mail("b@spam-shop.example")
        d = self.client.post(f"/email/message/{a.pk}/spam/").json()
        self.assertEqual((d["spam"], d["more"], d["auto_rule"]), (True, 1, "@spam-shop.example"))


class BlockTests(Base):
    def test_preview_then_block_purges_only_that_sender(self):
        acme = self.invite_mail()
        inv = MeetingInvite.objects.get()
        ev1 = self.event(acme)
        other = self.mail("eva@partner.example")
        ev2 = self.event(other)
        FoundDeadline.objects.create(user=self.user, email_message=acme, title="Angebot",
                                     due_at=datetime.datetime(2026, 11, 5, 9, tzinfo=dt_tz.utc))
        p = blocks.preview(self.user, "max@acme.example")
        self.assertEqual((p["n_events"], p["n_invites"], p["n_deadlines"]), (1, 1, 1))
        self.assertTrue(CalendarEvent.objects.filter(pk=ev1.pk).exists())   # перегляд нічого не змінює
        r = blocks.block(self.user, "max@acme.example")
        self.assertEqual(r["events"], 1)
        self.assertFalse(CalendarEvent.objects.filter(pk=ev1.pk).exists())
        self.assertTrue(CalendarEvent.objects.filter(pk=ev2.pk).exists())
        inv.refresh_from_db()
        self.assertEqual(inv.status, MeetingInvite.DISMISSED)
        self.assertEqual(FoundDeadline.objects.get().status, FoundDeadline.DISMISSED)

    def test_blocked_sender_gives_no_new_invites_or_deadline_candidates(self):
        blocks.block(self.user, "@acme.example")
        self.invite_mail(uid="m-9@outlook")
        self.assertFalse(MeetingInvite.objects.exists())
        self.mail("max@acme.example", body="Bitte bis 05.11.2026 liefern")
        ok = self.mail("eva@partner.example", body="Bitte bis 06.11.2026 liefern")
        _, found = deadlines.candidates(self.user, {})
        self.assertEqual([m.pk for m in found], [ok.pk])

    def test_popup_actions(self):
        self.client.force_login(self.user)
        acme = self.invite_mail()
        from calendar_app import invites
        inv = MeetingInvite.objects.get()
        invites.accept(inv, self.user)
        inv.refresh_from_db()
        d = self.client.get(f"/calendar/event/{inv.event_id}/").json()
        self.assertEqual(d["sender"]["address"], "max@acme.example")
        r = self.client.get("/calendar/blocks/preview/", {"event": inv.event_id, "scope": "domain"}).json()
        self.assertEqual((r["key"], r["n_events"]), ("@acme.example", 1))
        self.assertTrue(self.client.post(f"/calendar/event/{inv.event_id}/not-needed/").json()["ok"])
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.event_id), (MeetingInvite.DISMISSED, None))
        ev = self.event(acme)
        r = self.client.post("/calendar/blocks/", {"event": ev.pk, "scope": "sender"},
                             content_type="application/json").json()
        self.assertEqual((r["key"], r["removed"]), ("max@acme.example", 1))
        other = get_user_model().objects.create_user("bob", "b@x.y", "x", is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get("/calendar/blocks/preview/", {"message": acme.pk}).status_code, 404)

    def test_popup_spam_marks_mail(self):
        self.client.force_login(self.user)
        msg = self.mail("a@spam-shop.example")
        ev = self.event(msg)
        r = self.client.post("/calendar/blocks/", {"event": ev.pk, "scope": "sender", "reason": "spam"},
                             content_type="application/json").json()
        self.assertTrue(r["spam"])
        self.assertTrue(EmailMessage.objects.get(pk=msg.pk).is_spam)
        self.assertFalse(CalendarEvent.objects.filter(pk=ev.pk).exists())


class CleanupPageTests(Base):
    def test_lists_groups_and_removes_only_checked(self):
        self.client.force_login(self.user)
        junk = self.mail("a@spam-shop.example")
        EmailMessage.objects.filter(pk=junk.pk).update(is_spam=True)
        ev_spam = self.event(junk, "Gewinnspiel")
        blocks.block(self.user, "@acme.example", do_purge=False)
        acme = self.mail("max@acme.example")
        ev_a, ev_b = self.event(acme, "A"), self.event(acme, "B", days=4)
        keep = self.event(self.mail("eva@partner.example"), "Partner")
        page = self.client.get("/calendar/cleanup/")
        self.assertContains(page, "Спам — @spam-shop.example")
        self.assertContains(page, "Gewinnspiel")
        self.assertContains(page, "@acme.example")
        self.assertNotContains(page, "Partner")
        self.client.post("/calendar/cleanup/", {"action": "clean", "ev": [ev_spam.pk, ev_a.pk]})
        left = set(CalendarEvent.objects.values_list("pk", flat=True))
        self.assertEqual(left, {ev_b.pk, keep.pk})

    def test_new_filter_preview_and_block(self):
        self.client.force_login(self.user)
        ev = self.event(self.mail("news@shop.example"), "Rabatt bis Freitag")
        page = self.client.get("/calendar/cleanup/", {"add": "shop.example"})
        self.assertContains(page, "Rabatt bis Freitag")
        self.assertContains(page, 'name="block_new" value="@shop.example"')
        self.client.post("/calendar/cleanup/", {"action": "clean", "ev": [ev.pk], "block_new": ["@shop.example"]})
        self.assertTrue(CalendarSenderBlock.objects.filter(user=self.user, key="@shop.example").exists())
        self.assertFalse(CalendarEvent.objects.filter(pk=ev.pk).exists())
        b = CalendarSenderBlock.objects.get()
        self.client.post("/calendar/cleanup/", {"action": "unblock", "block": b.pk})
        self.assertFalse(CalendarSenderBlock.objects.exists())
