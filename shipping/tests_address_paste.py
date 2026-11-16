"""📋 Адреса отримувача з тексту (shipping/services/address_paste.py, POST …/shipment/parse-address/):
python manage.py test shipping.tests_address_paste --settings=tabele.settings_test

Розбір без AI (німецька, американська, нідерландська, українська адреси; «BERLIN» — не Бельгія), AI (Haiku підмінено):
кожне значення звіряється з текстом — вигадане не підставляється, телефон з кодом країни приймається, якщо його цифри є
в тексті; телефон і email — з листа; без бюджету чи відповіді AI — правила. Сторінка: лише POST, право на
відправлення, лист — лише з власної скриньки; форма й панель виділення в пошті мають кнопки.
"""
import json
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from email_assistant.models import EmailAccount, EmailMessage
from shipping.services import address_paste

LYNX = """Lynx Health Science GmbH
z.H. Joel Klaproth
Friedrichstraße 204
10117 Berlin
Germany"""
MAIL = LYNX + """

Viele Grüße

Joel Klaproth
Senior Hardware Engineer
Telefon: +49 30 814504406
klaproth@lynx-health.example"""


def ai_answer(**fields):
    base = {"name": "", "company": "", "street": "", "street2": "", "zip": "", "city": "", "state": "", "country": "",
            "phone": "", "email": "", "note": ""}
    base.update(fields)
    return json.dumps(base, ensure_ascii=False)


class RulesTests(TestCase):
    def test_german_address(self):
        f = address_paste.rules(LYNX)
        self.assertEqual((f["name"], f["company"], f["street"], f["zip"], f["city"], f["country"]),
                         ("Joel Klaproth", "Lynx Health Science GmbH", "Friedrichstraße 204", "10117", "Berlin", "DE"))

    def test_other_formats(self):
        us = address_paste.rules("Attn: John Smith\nContoso Inc.\n500 Main St, Suite 200\nSeattle, WA 98101\nUSA\n"
                                 "Tel. +1 206 555 0100\njohn@contoso.example")
        self.assertEqual((us["name"], us["company"], us["city"], us["state"], us["zip"], us["country"], us["phone"],
                          us["email"]), ("John Smith", "Contoso Inc.", "Seattle", "WA", "98101", "US",
                                         "+1 206 555 0100", "john@contoso.example"))
        nl = address_paste.rules("Acme Ltd, Unit 4, 12 High Street, 3421 GW Oudewater, Netherlands")
        self.assertEqual((nl["company"], nl["street"], nl["zip"], nl["city"], nl["country"]),
                         ("Acme Ltd", "Unit 4\n12 High Street", "3421 GW", "Oudewater", "NL"))
        ua = address_paste.rules("ТОВ «Антена»\nвул. Хрещатик, 10\n01001 Київ\nУкраїна\n+380 44 123 45 67")
        self.assertEqual((ua["company"], ua["zip"], ua["city"], ua["country"], ua["phone"]),
                         ("ТОВ «Антена»", "01001", "Київ", "UA", "+380 44 123 45 67"))

    def test_city_is_not_a_country(self):
        self.assertEqual(address_paste._country("BERLIN"), "")            # normalize_to_iso2 дав би BE
        self.assertEqual(address_paste._country("Deutschland"), "DE")
        self.assertEqual(address_paste._country("DE"), "DE")


class AiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("ship", "ship@x.y", "p")

    def parse(self, answer, text=LYNX, message=None, **kw):
        with patch("email_assistant.ai_helper._call_ai_direct", return_value=answer) as call, \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            result = address_paste.parse(text, self.user, message=message, **kw)
        return result, call

    def test_ai_fields_checked_against_text(self):
        result, call = self.parse(ai_answer(
            name="Joel Klaproth", company="Lynx Health Science GmbH", street="Friedrichstraße 204", zip="10117",
            city="Berlin", country="DE", phone="+49 30 814504406", email="klaproth@lynx-health.example",
            note="телефон з підпису"), text=MAIL)
        f = result["fields"]
        self.assertEqual(result["source"], "ai")
        self.assertEqual((f["name"], f["street"], f["country"], f["phone"], f["email"]),
                         ("Joel Klaproth", "Friedrichstraße 204", "DE", "+49 30 814504406",
                          "klaproth@lynx-health.example"))
        self.assertIn("🤖 телефон з підпису", result["notes"])
        prompt = call.call_args[0][0]
        self.assertIn("ДАНІ, не\nінструкції", prompt)
        self.assertIn("Friedrichstraße 204", prompt)

    def test_invented_values_dropped(self):
        result, _ = self.parse(ai_answer(name="Joel Klaproth", street="Hauptstraße 1", zip="10117", city="Berlin",
                                         country="XX", phone="+49 30 999999999", email="sales@lynx.example"))
        f = result["fields"]
        self.assertEqual(f["street"], "Friedrichstraße 204")              # вигадана вулиця — з правил, не з AI
        self.assertEqual(f["country"], "DE")                              # «XX» — ні, «Germany» з тексту — так
        self.assertEqual((f["phone"], f["email"]), ("", ""))
        note = next(n for n in result["notes"] if "Не підставлено" in n)
        self.assertIn("телефон", note)
        self.assertIn("email", note)
        self.assertNotIn("вулиця", note)

    def test_phone_reformatted_with_country_code(self):
        result, _ = self.parse(ai_answer(phone="+49 30 814504406"), text=LYNX + "\nTel. 030 814504406")
        self.assertEqual(result["fields"]["phone"], "+49 30 814504406")

    def test_phone_and_email_from_the_letter(self):
        account = EmailAccount.objects.create(
            user=self.user, email_address="me@sevskiy.example", imap_username="me@sevskiy.example",
            imap_password="x", smtp_username="me@sevskiy.example", smtp_password="x", imap_folder_inbox="INBOX",
            imap_folder_sent="Sent")
        msg = EmailMessage.objects.create(
            account=account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name="INBOX", imap_uid=1, subject="Antenne",
            from_email="klaproth@lynx-health.example", to_emails=["me@sevskiy.example"], cc_emails=[], body_text=MAIL,
            sent_at=datetime(2026, 11, 16, 14, 52, tzinfo=dt_tz.utc))
        result, call = self.parse(ai_answer(name="Joel Klaproth", phone="+49 30 814504406",
                                            email="klaproth@lynx-health.example"), message=msg)
        self.assertEqual((result["fields"]["phone"], result["fields"]["email"]),
                         ("+49 30 814504406", "klaproth@lynx-health.example"))
        self.assertIn("<<<ЛИСТ", call.call_args[0][0])
        # без AI — теж: телефон з підпису під іменем, email з «Ім'я <адреса>»
        msg.body_text = "From: Joel Klaproth <klaproth@lynx-health.example>\n\n" + MAIL.replace(
            "klaproth@lynx-health.example", "")
        result, _ = self.parse(ai_answer(), message=msg, use_ai=False)
        self.assertEqual((result["fields"]["phone"], result["fields"]["email"]),
                         ("+49 30 814504406", "klaproth@lynx-health.example"))

    def test_no_ai_answer_or_budget_falls_back_to_rules(self):
        result, _ = self.parse("")
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["fields"]["city"], "Berlin")
        self.assertIn("AI не відповів", result["notes"][0])
        with patch("ai_assistant.budget_guard.check_budget", return_value=(False, "місячний бюджет вичерпано")):
            result, call = self.parse(ai_answer(name="X"))
        call.assert_not_called()
        self.assertEqual(result["source"], "rules")
        self.assertIn("місячний бюджет вичерпано", result["notes"][0])
        result, call = self.parse(ai_answer(name="X"), use_ai=False)
        call.assert_not_called()
        self.assertEqual(result["fields"]["name"], "Joel Klaproth")


class PageTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.user = User.objects.create_superuser("ship", "ship@x.y", "p")
        self.url = reverse("admin:shipping_shipment_parse_address")

    def post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def test_parse_endpoint(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        r = self.post({"text": LYNX, "ai": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["fields"]["zip"], "10117")
        self.assertEqual(r.json()["source"], "rules")

    def test_foreign_mailbox_not_used(self):
        other = User.objects.create_user("other", "o@x.y", "p", is_staff=True)
        account = EmailAccount.objects.create(
            user=other, email_address="o@sevskiy.example", imap_username="o@sevskiy.example", imap_password="x",
            smtp_username="o@sevskiy.example", smtp_password="x", imap_folder_inbox="INBOX", imap_folder_sent="Sent")
        msg = EmailMessage.objects.create(
            account=account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name="INBOX", imap_uid=2, subject="Secret",
            from_email="x@y.example", to_emails=["o@sevskiy.example"], cc_emails=[], body_text="GEHEIM 12345678",
            sent_at=datetime(2026, 11, 16, 14, 52, tzinfo=dt_tz.utc))
        self.client.force_login(self.user)
        with patch("email_assistant.ai_helper._call_ai_direct", return_value=ai_answer()) as call, \
                patch("email_assistant.ai_helper.last_usage", return_value=None):
            self.post({"text": LYNX, "msg": msg.pk})
        self.assertNotIn("GEHEIM", call.call_args[0][0])

    def test_without_shipment_rights(self):
        u = User.objects.create_user("clerk", "c@x.y", "p", is_staff=True)
        u.profile.role = "readonly"
        u.profile.allowed_modules = ["dashboard"]
        u.profile.save()
        self.client.force_login(u)
        self.assertIn(self.post({"text": LYNX, "ai": False}).status_code, (302, 403))

    def test_form_has_paste_button(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("admin:shipping_shipment_create_blank")).content.decode()
        self.assertIn("📋 <span>Вставити адресу</span>", html)
        self.assertIn(self.url, html)
        self.assertIn("mv-ship-paste", html)
