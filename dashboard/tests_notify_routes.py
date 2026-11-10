"""«🗺 Хто що отримує»: свої адреси й Telegram-чати для кожного сповіщення, «🚫 Не надсилати клієнтам», таблиця
в «Налаштування → Сповіщення» (dashboard/notify_routes.py, config/admin_notify_map.py).

python manage.py test dashboard.tests_notify_routes --settings=tabele.settings_test
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend
from django.http import QueryDict
from django.test import TestCase

from config.models import NotificationSettings
from dashboard import notifications, notify_routes

CRITICAL = [{"pk": 1, "sku": "AN220203-02A", "name": "Antenna", "stock": 1.0, "monthly": 4.0, "months_left": 0.25,
             "is_critical": True}]


def _ns(**kw):
    ns = NotificationSettings.get()
    ns.email_enabled, ns.email_host, ns.email_to = True, "smtp.example", "team@firma.example"
    ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "123:abc", "-100"
    for k, v in kw.items():
        setattr(ns, k, v)
    ns.save()
    return ns


class RouteTests(TestCase):
    def test_event_routes_replace_defaults(self):
        ns = _ns(routes={"new_order": {"to": ["sales@firma.example"], "tg": ["555", "@lager_chan"]}})
        self.assertEqual(notifications.recipients(ns), ["team@firma.example"])     # поза сповіщенням — як було
        self.assertEqual(notifications.tg_chats(ns), ["-100"])
        with notifications.event("new_order"):
            self.assertEqual(notifications.recipients(ns), ["sales@firma.example"])
            self.assertEqual(notifications.tg_chats(ns), ["555", "@lager_chan"])
            with notifications.send_only_to(["me@firma.example"], True):          # «Надіслати зараз» — сильніше
                self.assertEqual(notifications.recipients(ns), ["me@firma.example"])
        with notifications.event("status_change"):                               # свого немає — за замовчуванням
            self.assertEqual(notifications.recipients(ns), ["team@firma.example"])
            self.assertEqual(notifications.tg_chats(ns), ["-100"])

    def test_telegram_goes_to_every_chat(self):
        ns = _ns(routes={"shipment": {"tg": ["111", "222"]}})
        sent = []

        def call(ns_, method, payload, timeout=10):
            if payload["chat_id"] == "222":
                raise RuntimeError("Telegram 400: chat not found")
            sent.append(payload["chat_id"])
            return {"ok": True}

        with mock.patch("dashboard.notifications._tg_call", side_effect=call), \
                notifications.event("shipment"):
            self.assertEqual(notifications._send_telegram(ns, "hi"), {"ok": True})   # один чат не прийняв — решта йде
            self.assertEqual(sent, ["111"])
            notifications._send_telegram(ns, "hi", chat_id_override="999")           # окремий чат (JLCPCB, агент)
            self.assertEqual(sent, ["111", "999"])
        with mock.patch("dashboard.notifications._tg_call", side_effect=RuntimeError("Telegram 401: Unauthorized")):
            with self.assertRaisesMessage(RuntimeError, "Telegram 401"):
                notifications._send_telegram(ns, "hi")

    def test_for_event_decorator_and_jlc_shipments(self):
        ns = _ns(routes={"shipment": {"to": ["lager@firma.example"]}})
        seen = []

        @notifications.for_event(lambda shipment, *a: "" if getattr(shipment, "jlc_order_id", None) else "shipment")
        def f(shipment):
            seen.append(notifications.recipients(ns))

        f(mock.Mock(jlc_order_id=None))
        f(mock.Mock(jlc_order_id=5))
        self.assertEqual(seen, [["lager@firma.example"], ["team@firma.example"]])
        self.assertEqual(notifications.current_event(), "")

    def test_save_from_table(self):
        ns = _ns()
        post = QueryDict(mutable=True)
        post.update({"nr_new_order_seen": "1", "nr_new_order_to": "a@firma.example, wrong, A@firma.example",
                     "nr_new_order_tg": "-1001234567890, x y", "nr_alerts_seen": "1", "nr_alerts_telegram": "on"})
        changed, bad = notify_routes.save(ns, post)
        self.assertEqual(changed, ["new_order", "alerts"])
        self.assertEqual(bad, ["wrong", "x", "y"])
        self.assertEqual(ns.routes["new_order"], {"to": ["a@firma.example"], "tg": ["-1001234567890"]})
        self.assertEqual(ns.routes["alerts"], {"email": False, "telegram": True})   # ✉️ знято, 💬 лишилось
        self.assertFalse(notify_routes.flag(ns, "alerts", "email"))
        self.assertTrue(notify_routes.flag(ns, "alerts", "telegram"))
        post = QueryDict(mutable=True)
        post.update({"nr_new_order_seen": "1", "nr_new_order_to": "", "nr_new_order_tg": ""})
        notify_routes.save(ns, post)
        self.assertNotIn("new_order", ns.routes)                                  # порожньо — як за замовчуванням
        self.assertIn("alerts", ns.routes)                                        # рядка не було у формі — без змін


class AlertsRouteTests(TestCase):
    def setUp(self):
        for target, kw in (("dashboard.notifications._smtp_connection", {"side_effect": lambda ns: EmailBackend()}),
                           ("django.core.mail.get_connection", {"side_effect": lambda *a, **k: EmailBackend()}),
                           ("dashboard.notifications._get_critical_stock", {"return_value": CRITICAL}),
                           ("dashboard.notifications._get_overdue_orders", {"return_value": []})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_alerts_to_own_addresses_and_off_switch(self):
        _ns(stock_alerts_enabled=True, routes={"alerts": {"to": ["lager@firma.example"], "tg": ["777"]}})
        with mock.patch("dashboard.notifications._tg_call", return_value={"ok": True}) as tg:
            res = notifications.run_alerts(force=True)
        self.assertTrue(res["sent"])
        self.assertEqual([m.to for m in mail.outbox], [["lager@firma.example"]])
        self.assertEqual([c.args[2]["chat_id"] for c in tg.call_args_list], ["777"])
        mail.outbox.clear()
        _ns(stock_alerts_enabled=True, routes={"alerts": {"email": False, "telegram": False}})
        with mock.patch("dashboard.notifications._tg_call") as tg:
            res = notifications.run_alerts(force=True)
        self.assertFalse(res["sent"])
        self.assertEqual(mail.outbox, [])
        tg.assert_not_called()


class CustomerBlockTests(TestCase):
    def test_blocked_rules(self):
        ns = _ns(customer_block="anna@firma.example\n@spam.example; ")
        self.assertEqual(notify_routes.blocked(ns, "Anna@Firma.example"), "anna@firma.example")
        self.assertEqual(notify_routes.blocked(ns, "x@mail.spam.example"), "@spam.example")
        self.assertEqual(notify_routes.blocked(ns, "x@nospam.example"), "")
        self.assertEqual(notify_routes.blocked(ns, "bob@firma.example"), "")

    def test_customer_letters_are_not_sent(self):
        from sales.admin import _notify_blocked
        from sales.models import SalesOrder
        ns = _ns(customer_block="@kunde.example", customer_notify_enabled=True, order_confirm_notify_enabled=True)
        order = SalesOrder.objects.create(source="manual", order_number="B-1", email="einkauf@kunde.example",
                                          tracking_number="1Z999")
        with mock.patch("dashboard.notifications._smtp_connection", side_effect=lambda ns: EmailBackend()):
            self.assertFalse(notifications.send_ship_notification(order))
            self.assertFalse(notifications.send_order_confirm_notification(order))
        self.assertEqual(mail.outbox, [])
        self.assertIn("🚫", _notify_blocked(ns, order, "customer"))
        self.assertIn("@kunde.example", _notify_blocked(ns, order, "order_confirm"))
        ns.customer_block = ""
        self.assertEqual(_notify_blocked(ns, order, "customer"), "")


class MapPageTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.ns = _ns(routes={"shipment": {"tg": ["4242"]}})
        self.user = User.objects.create_superuser("root", "me@firma.example", "p", first_name="Anna")
        self.client.force_login(self.user)
        self.url = "/admin/config/notificationsettings/1/change/"

    def test_page_shows_table(self):
        r = self.client.get(self.url)
        for text in ("🗺 Хто що отримує", 'name="nr_new_order_to"', 'name="nr_alerts_email"', 'data-sync="id_new_order_email"',
                     'data-sync="id_customer_block"', "4242", 'id="nt-opts"', "mv_notify_pick.js", "Налаштовується деінде"):
            self.assertContains(r, text)
        import json
        import re
        opts = json.loads(re.search(r'id="nt-opts" type="application/json">(.*?)</script>', r.content.decode()).group(1))
        self.assertIn(["me@firma.example", "👤 Anna"], opts["email"])            # «＋ з Minerva»: співробітники
        self.assertIn(["-100", "📢 основний чат сповіщень (Chat ID)"], opts["tg"])

    def test_post_saves_routes_with_the_form(self):
        r = self.client.get(self.url)
        form = r.context["adminform"].form
        data = {}
        for name, f in form.fields.items():
            v = form.initial.get(name, f.initial)
            if isinstance(v, bool):
                if v:
                    data[name] = "on"
            elif v is not None:
                data[name] = v.strftime("%H:%M") if hasattr(v, "strftime") else v
        data.update({"nr_status_change_seen": "1", "nr_status_change_to": "chef@firma.example",
                     "nr_status_change_tg": "", "customer_block": "@kunde.example", "_save": "1"})
        r = self.client.post(self.url, data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        ns = NotificationSettings.get()
        self.assertEqual(ns.routes["status_change"], {"to": ["chef@firma.example"]})
        self.assertEqual(ns.routes["shipment"], {"tg": ["4242"]})                  # рядка не було в POST — без змін
        self.assertEqual(ns.customer_block, "@kunde.example")


class SendNowRouteTests(TestCase):
    def test_send_now_prefills_own_addresses(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        _ns(routes={"digest_weekly": {"to": ["chef@firma.example"]}})
        self.client.force_login(User.objects.create_superuser("root", "me@firma.example", "p"))
        r = self.client.get("/admin/config/notificationsettings/1/send-weekly-digest/")
        self.assertContains(r, "chef@firma.example</textarea>", html=False)
        r = self.client.get("/admin/config/notificationsettings/1/send-digest/")
        self.assertContains(r, "team@firma.example</textarea>", html=False)
