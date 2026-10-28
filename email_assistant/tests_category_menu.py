"""Правий клік на своїй категорії в скриньці (views_categories.py):
python manage.py test email_assistant --settings=tabele.settings_test"""
import json
from unittest import mock

from django.contrib.auth import get_user_model

from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, MailCategory
from email_assistant.tests_work_categories import ME as ME_SENT, Base


class MenuBase(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.a = self.mail("orders@digikey.com", subject="Order 1")
        self.b = self.mail("orders@digikey.com", subject="Order 2")
        self.c = self.mail(ME_SENT, subject="Re: Order 2", to=["orders@digikey.com"], folder="Sent")
        EmailMessage.objects.filter(pk__in=[self.a.pk, self.b.pk]).update(is_read=False)
        EmailMessage.objects.filter(pk=self.c.pk).update(folder=EmailMessage.FOLDER_SENT)
        # чужа скринька з тією самою категорією — дії її не чіпають
        other = get_user_model().objects.create_user("bob", "bob@x.example", "x", is_staff=True)
        self.other_box = EmailAccount.objects.create(
            user=other, email_address="bob@x.example", imap_username="bob", imap_password="x",
            smtp_username="bob", smtp_password="x")
        self.foreign = self.mail("orders@digikey.com", subject="Bob order", account=self.other_box)
        EmailMessage.objects.filter(pk=self.foreign.pk).update(is_read=False)

    def post(self, action, **data):
        return self.client.post(f"/email/category/{self.dk.pk}/action/", json.dumps({"action": action, **data}),
                                content_type="application/json")


class CategoryMenuTests(MenuBase):
    def test_info_lists_rules_counts_and_links(self):
        d = self.client.get(f"/email/category/{self.dk.pk}/").json()
        self.assertEqual((d["mails"], d["unread"], d["sent"]), (3, 2, 1))
        self.assertTrue(any("digikey" in r for r in d["rules"]))
        self.assertIn(f"/admin/email_assistant/mailcategory/{self.dk.pk}/change/", d["card_url"])
        self.assertIn(f"?cat={self.dk.pk}", d["dante"]["url"])
        self.assertTrue(d["can_change"] and d["can_delete"])

    def test_inbox_has_menu_and_hide_unhide(self):
        page = self.client.get("/email/")
        self.assertContains(page, 'oncontextmenu="return showWcCtx(event, this)"')
        self.assertContains(page, 'id="em-wc-ctx"')
        self.assertTrue(self.post("hide").json()["ok"])
        self.assertEqual(EmailSettings.get_for_user(self.user).hidden_work_categories, [self.dk.pk])
        page = self.client.get("/email/")
        self.assertContains(page, "em-nav-hid")
        self.assertContains(page, "Сховані")
        self.post("unhide")
        self.assertEqual(EmailSettings.get_for_user(self.user).hidden_work_categories, [])

    def test_mark_all_read_only_own_mailbox(self):
        self.assertEqual(self.post("read").json()["changed"], 2)
        self.assertFalse(EmailMessage.objects.filter(pk__in=[self.a.pk, self.b.pk], is_read=False).exists())
        self.foreign.refresh_from_db()
        self.assertFalse(self.foreign.is_read)

    def test_trash_moves_category_mails_of_this_mailbox(self):
        with mock.patch("email_assistant.views._imap_bg_many") as imap:
            self.assertEqual(self.post("trash").json()["moved"], 3)
        self.assertEqual(len(imap.call_args[0][1]), 3)
        self.assertEqual(EmailMessage.objects.filter(folder=EmailMessage.FOLDER_TRASH).count(), 3)
        self.foreign.refresh_from_db()
        self.assertNotEqual(self.foreign.folder, EmailMessage.FOLDER_TRASH)

    def test_save_rules_and_recount(self):
        with mock.patch("email_assistant.work_categories.start_recompute", return_value=True) as rc:
            d = self.post("save", name="DigiKey Marketplace", icon="🟥", color="#ff0000",
                          senders="digikey\n@digikey.de\nDIGIKEY\n", subject_words="Marketplace").json()
        self.assertTrue(d["recompute"])
        rc.assert_called_once_with(365)
        self.dk.refresh_from_db()
        self.assertEqual((self.dk.name, self.dk.color), ("DigiKey Marketplace", "#ff0000"))
        self.assertEqual(self.dk.senders, "digikey\n@digikey.de")   # без повторів
        with mock.patch("email_assistant.work_categories.start_recompute") as rc:
            d = self.post("save", name="DK", color="red", senders=self.dk.senders,
                          subject_words="Marketplace").json()
        rc.assert_not_called()   # правила ті самі — без перерахунку
        self.dk.refresh_from_db()
        self.assertEqual((self.dk.name, self.dk.color), ("DK", "#ff0000"))   # «red» — не HEX, лишається старий

    def test_delete_keeps_mails(self):
        with mock.patch("email_assistant.work_categories.start_recompute"):
            self.assertTrue(self.post("delete").json()["ok"])
        self.assertFalse(MailCategory.objects.filter(pk=self.dk.pk).exists())
        self.assertEqual(EmailMessage.objects.filter(pk__in=[self.a.pk, self.b.pk], work_category=None).count(), 2)

    def test_without_rights_only_own_view_actions(self):
        clerk = get_user_model().objects.create_user("clerk", "clerk@x.example", "x", is_staff=True)
        self.client.force_login(clerk)
        self.assertEqual(self.post("save", name="X").status_code, 403)
        self.assertEqual(self.post("delete").status_code, 403)
        self.assertTrue(self.post("hide").json()["ok"])
        self.assertTrue(MailCategory.objects.filter(pk=self.dk.pk, name="DigiKey").exists())



class WhyTests(MenuBase):
    """«🔍 Чому лист тут»: правило, що спрацювало, інші категорії, швидка зміна."""

    def why(self, msg):
        return self.client.get(f"/email/message/{msg.pk}/why/").json()

    def test_sender_rule_and_other_matches(self):
        self.shop.senders = "@digikey.com"
        self.shop.save()
        d = self.why(self.a)
        self.assertEqual((d["category"]["pk"], d["how"], d["rule"], d["field"]), (self.dk.pk, "sender", "digikey", "senders"))
        self.assertIn("orders@digikey.com", d["text"])
        self.assertEqual([c["pk"] for c in d["also"]], [self.shop.pk])   # теж підходить, але нижче за порядком
        self.assertFalse(d["stale"])
        self.assertEqual(d["contact"], "orders@digikey.com")

    def test_subject_word_manual_thread_and_none(self):
        m = self.mail("kunde@gmail.com", subject="Ihre Bestellung 7")
        d = self.why(m)
        self.assertEqual((d["how"], d["rule"], d["field"]), ("word", "bestellung", "subject_words"))
        from email_assistant.models import EmailThread
        EmailThread.objects.filter(pk=m.thread_id).update(work_category=self.dk)
        d = self.why(m)
        self.assertEqual(d["how"], "thread")
        self.assertTrue(d["stale"])   # мітка ще «Магазин», після перерахунку — DigiKey
        self.assertEqual(d["expected"]["pk"], self.dk.pk)
        none = self.mail("x@nowhere.example", subject="Hallo")
        self.assertIsNone(self.why(none)["category"])

    def test_remove_matched_rule(self):
        with mock.patch("email_assistant.work_categories.start_recompute", return_value=True):
            d = self.post("remove_line", field="senders", line="DigiKey").json()
        self.assertTrue(d["ok"])
        self.dk.refresh_from_db()
        self.assertEqual(self.dk.senders, "")
        self.assertFalse(self.post("remove_line", field="senders", line="digikey").json()["ok"])

    def test_foreign_mail_is_404(self):
        self.assertEqual(self.client.get(f"/email/message/{self.foreign.pk}/why/").status_code, 404)
