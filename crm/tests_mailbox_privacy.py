"""Картка клієнта: «📬 Листи зі скриньки» й листи для AI-аналізу — лише з власних скриньок.

    python manage.py test crm --settings=tabele.settings_test
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from config.models import SystemSettings
from crm.models import Customer
from crm.views import _gather_email_context
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings

User = get_user_model()
CLIENT = "einkauf@client.example"


class CustomerMailboxPrivacyTests(TestCase):
    def setUp(self):
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.boss = User.objects.create_superuser("boss", "boss@x.y", "p")
        self.anna = User.objects.create_user("anna", "anna@x.y", "p", is_staff=True)
        self.customer = Customer.objects.create(name="Client GmbH", email=CLIENT)
        self.uid = 0
        self.boss_acc = self.account(self.boss, "boss@firma.example")
        self.anna_acc = self.account(self.anna, "anna@firma.example")
        self.mine = self.mail(self.boss_acc, "Boss: Angebot")
        self.mail(self.boss_acc, "Boss: spam", is_spam=True)
        self.theirs = self.mail(self.anna_acc, "Anna: privat")

    def account(self, user, address):
        acc = EmailAccount.objects.create(user=user, email_address=address, imap_username=address,
                                          imap_password="x", smtp_username=address, smtp_password="x")
        es = EmailSettings.get_for_user(user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        return acc

    def mail(self, acc, subject, **extra):
        self.uid += 1
        return EmailMessage.objects.create(
            account=acc, imap_uid=self.uid, folder=EmailMessage.FOLDER_INBOX, subject=subject,
            from_email=CLIENT, to_emails=[acc.email_address], body_text=subject,
            sent_at=timezone.now() - datetime.timedelta(hours=self.uid), **extra)

    def test_panel_lists_only_own_mailboxes_even_for_superuser(self):
        self.client.force_login(self.boss)
        r = self.client.get(f"/crm/customer/{self.customer.pk}/mailbox-emails/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([e["subject"] for e in r.json()["emails"]], ["Boss: Angebot"])

        self.client.force_login(self.anna)
        r = self.client.get(f"/crm/customer/{self.customer.pk}/mailbox-emails/")
        self.assertEqual([e["subject"] for e in r.json()["emails"]], ["Anna: privat"])

    def test_ai_context_uses_only_own_mailboxes(self):
        self.assertEqual([e["subject"] for e in _gather_email_context(self.customer, self.boss)],
                         ["Boss: Angebot"])
        self.assertEqual(_gather_email_context(self.customer, None), [])

    def test_selected_ids_of_other_mailbox_ignored(self):
        """Чужий лист, вписаний у mailbox_ids, у промпт AI не потрапляє."""
        import sys
        import types
        from unittest import mock
        fake = types.ModuleType("ai_assistant.service")
        fake.chat = mock.Mock(return_value="Analyse")
        self.client.force_login(self.boss)
        with mock.patch.dict(sys.modules, {"ai_assistant.service": fake}), \
                mock.patch("ai_assistant.tools.execute_tool", return_value={}):
            r = self.client.get(f"/crm/customer/{self.customer.pk}/ai-analysis/",
                                {"include": "customer", "mailbox_ids": f"{self.mine.pk},{self.theirs.pk}"})
        self.assertEqual(r.status_code, 200, r.content)
        prompt = fake.chat.call_args.args[0]
        self.assertIn("Boss: Angebot", prompt)
        self.assertNotIn("Anna: privat", prompt)
