"""Звіти й сповіщення: відправлення за період, місячний звіт за повний місяць, екранування в Telegram і листі.

    python manage.py test dashboard --settings=tabele.settings_test
"""
import datetime
import io
import json
import urllib.error
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from dashboard import digest, notifications
from dashboard.notifications import esc, tg_cut
from sales.models import SalesOrder
from shop.models import ShopSettings

FIRST = datetime.date(2026, 10, 1)      # місячний звіт за замовчуванням — 1-го о 08:00


def order(n, **kw):
    values = dict(source="webshop", order_number=f"D{n}", client="Muster GmbH", currency="EUR",
                  order_date=datetime.date(2026, 9, 10))
    values.update(kw)
    return SalesOrder.objects.create(**values)


class DigestDataTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.default_currency = "EUR"
        ss.save()
        s = ShopSettings.get()
        s.fx_rates = {"USD": 0.5}
        s.save()

    def test_shipments_count_on_date_field(self):
        order(1, shipped_at=datetime.date(2026, 9, 3), status="shipped")
        order(2, shipped_at=datetime.date(2026, 9, 30), status="delivered")
        order(3, shipped_at=datetime.date(2026, 10, 1), status="shipped")
        order(4, shipped_at=datetime.date(2026, 9, 5), status="cancelled")
        self.assertEqual(digest._get_shipments_count(datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)), 2)

    def test_monthly_compares_two_full_months(self):
        order(1, total_price=Decimal("100"), order_date=datetime.date(2026, 9, 2))
        order(2, total_price=Decimal("200"), currency="USD", order_date=datetime.date(2026, 9, 30))   # 100 EUR
        order(3, total_price=Decimal("50"), order_date=datetime.date(2026, 8, 15))
        order(4, total_price=Decimal("999"), order_date=FIRST)                       # уже жовтень — не в звіті
        order(5, total_price=Decimal("700"), order_date=datetime.date(2026, 9, 5), status="cancelled")
        order(6, total_price=Decimal("10"), currency="GBP", order_date=datetime.date(2026, 9, 6))
        rc = digest._get_monthly_revenue_comparison(FIRST)
        self.assertEqual((rc["cur_start"], rc["cur_end"]), (datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)))
        self.assertEqual(rc["prev_start"], datetime.date(2026, 8, 1))
        self.assertEqual(rc["cur"]["revenue"], 200.0)
        self.assertEqual(rc["cur"]["count"], 3)
        self.assertEqual(rc["cur"]["missing"], "+ £10 без курсу")
        self.assertEqual(rc["prev"]["revenue"], 50.0)
        self.assertEqual(rc["diff_pct"], 300.0)
        self.assertEqual(rc["cur"]["label"], "Вересень 2026")

    def test_monthly_send_reports_previous_month(self):
        order(1, total_price=Decimal("100"), order_date=datetime.date(2026, 9, 2),
              shipped_at=datetime.date(2026, 9, 4), status="shipped")
        sent = {}
        with mock.patch("django.utils.timezone.localdate", return_value=FIRST), \
                mock.patch.object(digest, "_get_critical_stock", return_value=[]), \
                mock.patch("dashboard.notifications._send_event_email",
                           side_effect=lambda ns, subj, html: sent.setdefault("html", html)):
            from config.models import NotificationSettings
            ns = NotificationSettings.get()
            ns.email_enabled, ns.digest_email, ns.email_to = True, True, "team@firma.example"
            ns.save()
            digest._send_monthly(force=True)
        self.assertIn("Вересень 2026", sent["html"])
        self.assertIn("€100", sent["html"])
        self.assertIn("Відправлено за період:</b> 1", sent["html"])


class EscapingTests(TestCase):
    DATA = {
        "pending": [{"pk": 1, "order_number": "A&1", "client": "R&D GmbH", "deadline": datetime.date(2026, 11, 2),
                     "days_left": 3, "status": "<script>", "source": "web"}],
        "overdue": [], "delivered": [], "since_dt": None,
        "new_orders": [{"pk": 2, "order_number": "N1", "client": "SMA <-> N", "total": Decimal("5"),
                        "currency": "EUR", "source": "web"}],
        "stock": [{"sku": "SMA<->N", "name": "Kabel <b>", "stock": 1, "months_left": 0.2, "is_critical": True}],
    }

    def test_telegram_and_email_escape_values(self):
        tg = digest.build_digest_telegram(self.DATA, "R&D <Firma>")
        self.assertIn("R&amp;D GmbH", tg)
        self.assertIn("SMA &lt;-&gt; N", tg)
        self.assertNotIn("<script>", tg)
        self.assertIn("R&amp;D &lt;Firma&gt;", tg)
        html = digest.build_digest_email_html(self.DATA, "R&D")
        self.assertIn("R&amp;D GmbH", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("Kabel <b>", html)

    def test_alert_texts_escape_values(self):
        items = [{"sku": "A<B", "name": "R&D", "stock": 1, "months_left": 0.1, "is_critical": True}]
        overdue = [{"pk": 1, "order_number": "1&2", "client": "<i>x", "deadline": datetime.date(2026, 10, 1),
                    "days_late": 3, "status": "Отримано"}]
        tg = notifications._build_telegram_text(items, overdue, "Muster & Co")
        for raw in ("A<B", "R&D", "<i>x", "Muster & Co"):
            self.assertNotIn(raw, tg)
        html = notifications._build_html(items, overdue, "Muster & Co")
        self.assertIn("&lt;i&gt;x", html)

    def test_cut_never_splits_tag_or_entity(self):
        lines = [f"<b>{esc('R&D <' + str(i) + '>')}</b> | <code>{i}</code>" for i in range(400)]
        text = tg_cut("\n".join(lines), 1000)
        self.assertLessEqual(len(text), 1000)
        self.assertTrue(text.endswith("<i>... (обрізано)</i>"))
        self.assertEqual(text.count("<b>"), text.count("</b>"))
        self.assertEqual(text.count("<code>"), text.count("</code>"))
        self.assertNotRegex(text, r"&[a-z]*\n")
        # тег на кількох рядках — закривається
        self.assertTrue(tg_cut("<i>" + "x\n" * 600 + "</i>", 300).count("</i>") >= 2)
        # один довгий рядок — без розмітки
        one = tg_cut("<b>" + "R&amp;D " * 1000 + "</b>", 300)
        self.assertNotIn("<b>", one)
        self.assertLessEqual(len(one), 400)

    def test_telegram_rejection_is_logged_not_silent(self):
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "T", "1"
        ns.new_order_telegram, ns.new_order_email = True, False
        ns.save()
        err = urllib.error.HTTPError("https://api.telegram.org/x", 400, "Bad Request", {},
                                     io.BytesIO(json.dumps({"description": "can't parse entities"}).encode()))
        o = order(9, client="R&D GmbH")
        with mock.patch("urllib.request.urlopen", side_effect=err), \
                self.assertLogs("dashboard.notifications", level="WARNING") as logs:
            notifications.notify_new_order(o)
        self.assertIn("can't parse entities", "\n".join(logs.output))

    def test_new_order_telegram_text_escaped(self):
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.telegram_enabled, ns.new_order_telegram, ns.new_order_email = True, True, False
        ns.save()
        o = order(10, client="R&D GmbH", source="web<shop>")
        with mock.patch.object(notifications, "_send_telegram") as send:
            notifications.notify_new_order(o)
        text = send.call_args.args[1]
        self.assertIn("R&amp;D GmbH", text)
        self.assertIn("web&lt;shop&gt;", text)
