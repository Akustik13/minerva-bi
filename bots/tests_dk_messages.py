"""💬 Нові повідомлення DigiKey → Telegram і пошта (bots/services/dk_message_watch.py). DigiKey імітується.

    python manage.py test bots.tests_dk_messages --settings=tabele.settings_test
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from bots.models import DigiKeyConfig, DigiKeyMessageSeen
from bots.services import dk_message_watch as watch
from config.models import NotificationSettings
from crm.models import Customer
from sales.models import SalesOrder


def iso(minutes_ago=0, days_ago=0):
    return (timezone.now() - timedelta(minutes=minutes_ago, days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeDK:
    """Список тем і розмови DigiKey: topics {id: {topic, orderNumber, conversation: [...]}}."""

    def __init__(self):
        self.topics = {}

    def add(self, tid, order="1001", title="Lieferzeit", messages=()):
        self.topics[tid] = {"id": tid, "topic": title, "orderId": "uuid-" + tid, "orderNumber": order,
                            "lastUpdateDateUtc": "2026-10-01T10:00:00Z",   # DigiKey цю дату на повідомлення не міняє
                            "conversation": list(messages)}

    def say(self, tid, mid, sender, content, minutes_ago=1, days_ago=0):
        self.topics[tid]["conversation"].append({"id": mid, "sender": sender, "content": content,
                                                  "createDateUtc": iso(minutes_ago, days_ago), "files": []})

    def listing(self, config, token, max_total=0):
        return [{k: v for k, v in t.items() if k != "conversation"} for t in self.topics.values()]

    def topic(self, config, token, tid):
        t = self.topics[tid]
        return {k: (list(v) if k == "conversation" else v) for k, v in t.items() if k != "orderNumber"}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WatchTests(TestCase):
    def setUp(self):
        from core.shared_cache import cache
        cache.delete(watch.LOCK_KEY)
        watch.BACKGROUND = False
        self.addCleanup(setattr, watch, "BACKGROUND", True)
        self.cfg = DigiKeyConfig.get()
        self.cfg.marketplace_refresh_token = "r"
        self.cfg.marketplace_access_token = "a"
        self.cfg.public_base_url = "https://minerva.example.com"
        self.cfg.msg_check_enabled = self.cfg.msg_notify_telegram = self.cfg.msg_notify_email = True
        self.cfg.msg_notify_own = False                       # наші відповіді — окремий тест
        self.cfg.save()
        ns, _ = NotificationSettings.objects.get_or_create(pk=1)
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "bot", "chat"
        ns.email_enabled, ns.email_host, ns.email_to = True, "smtp.example.com", "team@example.com"
        ns.save()
        Customer.objects.create(name="Max Mustermann", company="R&D GmbH", email="max@example.com",
                                external_key="k-1")
        self.order = SalesOrder.objects.create(order_number="1001", source="digikey", customer_key="k-1")
        self.dk = FakeDK()
        self.tg, self.tg_chats = [], []
        patches = [
            mock.patch("bots.services.digikey_messages.get_all_topics_paginated", side_effect=self.dk.listing),
            mock.patch("bots.services.digikey_messages.get_topic", side_effect=self.dk.topic),
            mock.patch("bots.services.digikey.get_marketplace_token", return_value="tok"),
            mock.patch("dashboard.notifications._send_telegram",
                       side_effect=lambda ns, text, *a, **k: (self.tg.append(text),
                                                              self.tg_chats.append(k.get("chat_id_override")))),
            mock.patch("dashboard.notifications._smtp_connection", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_check(self, **kw):
        return watch.run(force=True, **kw)

    def test_new_customer_message_goes_to_telegram_and_mail(self):
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Wann kommt <die Ware> & wie?", minutes_ago=5)
        res = self.run_check()
        self.assertEqual(sum(len(f["messages"]) for f in res["found"]), 1)
        self.assertEqual(len(self.tg), 1)
        self.assertIn("Wann kommt &lt;die Ware&gt; &amp; wie?", self.tg[0])          # Telegram не зламається
        self.assertIn("R&amp;D GmbH · Max Mustermann", self.tg[0])                     # клієнт CRM замовлення
        self.assertIn(f"https://minerva.example.com/admin/sales/salesorder/{self.order.pk}/change/", self.tg[0])
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["team@example.com"])
        self.assertIn("DigiKey #1001", m.subject)
        html = m.alternatives[0][0]
        self.assertIn("Wann kommt &lt;die Ware&gt; &amp; wie?", html)                 # повний текст, екранований
        self.assertIn("/admin/bots/digikeyconfig/messages/", html)
        self.cfg.refresh_from_db()
        self.assertIn("нових повідомлень 1", self.cfg.msg_last_result)
        self.assertIn("Telegram ✓ 1", self.cfg.msg_last_result)
        seen = DigiKeyMessageSeen.objects.get(topic_id="t1")
        self.assertEqual(seen.notified_message_id, "m1")
        self.assertEqual(seen.last_message_id, "")                                     # у хабі лишається «нове»

        # та сама розмова — нічого; нове повідомлення в тій самій темі (дата теми не змінилась) — так
        self.run_check()
        self.assertEqual(len(self.tg), 1)
        self.dk.say("t1", "m2", "Supplier", "Morgen.", minutes_ago=3)
        self.run_check()
        self.assertEqual(len(self.tg), 1)                                              # наша відповідь — ні
        self.dk.say("t1", "m3", "Customer", "Danke!", minutes_ago=1)
        self.dk.say("t1", "m4", "Operator", "DigiKey: bitte antworten", minutes_ago=0)
        self.run_check()
        self.assertEqual(len(self.tg), 2)
        self.assertIn("нових повідомлень 2", self.tg[1])
        self.assertIn("покупець і оператор DigiKey", self.tg[1])
        self.assertIn("Danke!", mail.outbox[-1].alternatives[0][0])
        self.assertIn("Morgen.", mail.outbox[-1].alternatives[0][0])                  # «Раніше в розмові»

    def test_own_replies_option(self):
        """«І відповіді з нашого боку»: відповідь колеги (Supplier) — теж сповіщення; вимкнено — ні."""
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Frage", minutes_ago=5)
        self.run_check()
        self.cfg.msg_notify_own = True
        self.cfg.save()
        self.dk.say("t1", "m2", "Supplier", "Antwort vom Kollegen", minutes_ago=2)
        self.run_check()
        self.assertEqual(len(self.tg), 2)
        self.assertIn("наша відповідь", self.tg[1])
        self.assertIn("Antwort vom Kollegen", self.tg[1])
        self.cfg.msg_notify_own = False
        self.cfg.save()
        self.dk.say("t1", "m3", "Supplier", "Noch eine Antwort", minutes_ago=1)
        self.run_check()
        self.assertEqual(len(self.tg), 2)

    def login(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))

    def test_forward_whole_chat_from_hub(self):
        self.login()
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Erste Frage", days_ago=30)          # і старі — увесь чат
        self.dk.say("t1", "m2", "Supplier", "Unsere Antwort", days_ago=29)
        self.dk.say("t1", "m3", "Customer", "Letzte Frage", minutes_ago=3)
        url = "/admin/bots/digikeyconfig/messages/t1/forward/"
        r = self.client.post(url, '{"to": "chef@example.com"}', content_type="application/json")
        self.assertEqual(r.json(), {"ok": True, "to": ["chef@example.com"], "messages": 3})
        m = mail.outbox[-1]
        self.assertEqual(m.to, ["chef@example.com"])
        self.assertIn("↪ DigiKey #1001: Lieferzeit", m.subject)              # номер замовлення — зі списку тем
        html = m.alternatives[0][0]
        for text in ("Erste Frage", "Unsere Antwort", "Letzte Frage", "переслав root", "найновіше"):
            self.assertIn(text, html)
        self.assertEqual(self.tg, [])                                          # лише лист
        bad = self.client.post(url, '{"to": "not-an-address"}', content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        page = self.client.get("/admin/bots/digikeyconfig/messages/")
        self.assertContains(page, "✉️ Чат на пошту")
        self.assertContains(page, 'const FORWARD_TO     = "team@example.com"')
        ns = NotificationSettings.objects.get(pk=1)
        ns.email_host = ""
        ns.save()
        r = self.client.post(url, "{}", content_type="application/json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "у «Налаштування → Сповіщення» немає SMTP"))
        viewer = User.objects.create_user("vera", "v@x.y", "p", is_staff=True)
        self.client.force_login(viewer)
        self.assertIn(self.client.post(url, "{}", content_type="application/json").status_code, (302, 403))

    def test_forward_order_chat_from_order_card(self):
        self.login()
        self.dk.add("t1", title="Lieferzeit")
        self.dk.say("t1", "m1", "Customer", "Wann?", days_ago=2)
        self.dk.add("t2", title="Rechnung")
        self.dk.say("t2", "m2", "Operator", "Bitte Rechnung hochladen", minutes_ago=10)
        self.dk.add("t3", order="5555", title="Fremd")
        self.dk.say("t3", "m3", "Customer", "Anderes Auftrag", minutes_ago=1)
        base = f"/admin/sales/salesorder/{self.order.pk}/digikey-messages/"
        listed = self.client.get(base).json()
        self.assertEqual(sorted(t["id"] for t in listed["topics"]), ["t1", "t2"])  # лише теми замовлення
        r = self.client.post(base + "forward/", '{"to": ""}', content_type="application/json")
        self.assertEqual(r.json(), {"ok": True, "to": ["team@example.com"], "messages": 2})
        m = mail.outbox[-1]
        self.assertIn("чат (2 теми)", m.subject)
        html = m.alternatives[0][0]
        for text in ("Lieferzeit", "Rechnung", "Wann?", "Bitte Rechnung hochladen", "R&amp;D GmbH"):
            self.assertIn(text, html)
        self.assertNotIn("Anderes Auftrag", html)
        page = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/")
        self.assertContains(page, "✉️ Чат на пошту")
        empty = SalesOrder.objects.create(order_number="7777", source="digikey")
        r = self.client.post(f"/admin/sales/salesorder/{empty.pk}/digikey-messages/forward/", "{}",
                             content_type="application/json")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "у DigiKey немає розмов цього замовлення"))
        self.assertEqual(self.client.get(base + "forward/").status_code, 405)
        from core.models import UserProfile
        viewer = User.objects.create_user("vera", "v@x.y", "p", is_staff=True)
        UserProfile.objects.filter(user=viewer).update(role="readonly", allowed_modules=["sales"])
        self.client.force_login(User.objects.get(pk=viewer.pk))      # лише перегляд замовлень — не надсилає
        self.assertIn(self.client.post(base + "forward/", "{}", content_type="application/json").status_code,
                      (302, 403))

    def test_old_messages_of_an_unknown_topic_are_not_sent(self):
        self.dk.add("old")
        self.dk.say("old", "o1", "Customer", "Altes Thema", days_ago=watch.FIRST_DAYS + 3)
        self.dk.add("fresh", order="9999")                                             # замовлення немає в Minerva
        self.dk.say("fresh", "f1", "Customer", "Neu", minutes_ago=2)
        res = self.run_check()
        self.assertEqual([f["topic"]["id"] for f in res["found"]], ["fresh"])
        self.assertEqual(len(self.tg), 1)
        self.assertTrue(DigiKeyMessageSeen.objects.get(topic_id="old").notified_upto)

    def test_mark_read_in_hub_does_not_stop_and_check_does_not_mark_read(self):
        DigiKeyMessageSeen.objects.create(topic_id="t1", last_message_id="m1")          # людина прочитала в хабі
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Hallo", minutes_ago=2)
        self.run_check()
        self.assertEqual(len(self.tg), 1)                                              # сповіщення окремо від «прочитано»

    def test_channels_off_and_own_recipients(self):
        self.cfg.msg_notify_telegram = False
        self.cfg.msg_notify_email_to = "sales@example.com; boss@example.com"
        self.cfg.save()
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Hallo", minutes_ago=2)
        self.run_check()
        self.assertEqual(self.tg, [])
        self.assertEqual(mail.outbox[0].to, ["sales@example.com", "boss@example.com"])
        self.cfg.refresh_from_db()
        self.assertIn("Telegram — вимкнено для повідомлень DigiKey", self.cfg.msg_last_result)

    def test_schedule_disabled_and_lock(self):
        from core.shared_cache import cache
        self.cfg.msg_check_enabled = False
        self.cfg.save()
        self.assertIsNone(watch.run())                                                 # вимкнено — cron нічого
        self.cfg.msg_check_enabled = True
        self.cfg.msg_last_checked_at = timezone.now()
        self.cfg.save()
        self.assertIsNone(watch.run())                                                 # інтервал не минув
        cache.add(watch.LOCK_KEY, 1, 60)
        self.assertIsNone(watch.run(force=True))                                       # уже йде інша
        cache.delete(watch.LOCK_KEY)
        self.assertIsNotNone(watch.run(force=True))

    def test_own_telegram_chats(self):
        """«Telegram-чати» повідомлень DigiKey: спільна група й особистий чат; порожньо — основний чат."""
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Hallo", minutes_ago=2)
        self.run_check()
        self.assertEqual(self.tg_chats, ["chat"])
        self.cfg.msg_notify_tg_chats = "-1001234567890, 987654321"
        self.cfg.save()
        self.dk.say("t1", "m2", "Customer", "Noch da?", minutes_ago=1)
        self.run_check()
        self.assertEqual(self.tg_chats[1:], ["-1001234567890", "987654321"])
        self.assertEqual(watch.status(self.cfg)["telegram"]["to"], ["-1001234567890", "987654321"])
        ns = NotificationSettings.get()
        ns.telegram_chat_id = ""
        ns.save()
        self.cfg.msg_notify_tg_chats = ""
        self.cfg.save()
        self.assertEqual(watch.status(self.cfg)["telegram"]["why"], "не вказано чат Telegram")

    def test_settings_form_pickers_and_validation(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        url = f"/admin/bots/digikeyconfig/{self.cfg.pk}/change/"
        page = self.client.get(url)
        for text in ('name="msg_notify_tg_chats"', 'class="nt-pick" data-kind="tg"', 'data-kind="email"',
                     "mv_notify_pick.js", "основний чат сповіщень: chat"):
            self.assertContains(page, text)
        form = page.context["adminform"].form
        data = {}
        for name, f in form.fields.items():
            v = form.initial.get(name, f.initial)
            if isinstance(v, bool):
                if v:
                    data[name] = "on"
            elif v is not None and not isinstance(v, (dict, list)):
                data[name] = v.isoformat() if hasattr(v, "isoformat") else v
        data.update({"msg_notify_tg_chats": "-100123, kein chat", "msg_notify_email_to": "a@b.de"})
        r = self.client.post(url, data)
        self.assertEqual(r.status_code, 200)
        self.assertIn("msg_notify_tg_chats", r.context["adminform"].form.errors)
        data["msg_notify_tg_chats"] = "-100123, @lager_chan"
        r = self.client.post(url, data)
        self.assertEqual(r.status_code, 302, r.status_code == 200 and r.context["adminform"].form.errors)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.msg_notify_tg_chats, "-100123, @lager_chan")

    def test_list_failure_is_reported(self):
        with mock.patch("bots.services.digikey_messages.get_all_topics_paginated",
                        side_effect=RuntimeError("503 Service Unavailable")), \
                mock.patch("bots.services.api_health.notify_connection_failure") as failed, \
                mock.patch("time.sleep"):
            self.assertIsNone(self.run_check())
        self.assertTrue(failed.called)
        self.cfg.refresh_from_db()
        self.assertIn("Список тем не прочитано", self.cfg.msg_last_result)

    def test_command_and_settings_panel(self):
        from django.core.management import call_command
        self.dk.add("t1")
        self.dk.say("t1", "m1", "Customer", "Hallo", minutes_ago=2)
        call_command("check_digikey_messages", "--force", stdout=mock.MagicMock())
        self.assertEqual(len(self.tg), 1)
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        page = self.client.get(f"/admin/bots/digikeyconfig/{self.cfg.pk}/change/")
        self.assertContains(page, "🔄 Перевірити зараз")
        self.assertContains(page, "так — team@example.com")
        url = "/admin/bots/digikeyconfig/messages/check-now/"
        self.dk.say("t1", "m2", "Customer", "Noch da?", minutes_ago=1)
        self.assertEqual(self.client.post(url).json()["started"], True)
        self.assertEqual(len(self.tg), 2)
        self.assertFalse(self.client.get(url).json()["running"])
        viewer = User.objects.create_user("vera", "v@x.y", "p", is_staff=True)
        self.client.force_login(viewer)
        self.assertIn(self.client.post(url).status_code, (302, 403))
