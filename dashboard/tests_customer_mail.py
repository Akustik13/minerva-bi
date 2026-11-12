"""«👤 Клієнтам — з адреси» (dashboard/customer_mail.py): листи клієнтам «📧 посилку відправлено» і «📥 замовлення
отримано» — з SMTP сповіщень (як завжди) або зі скриньки пошти Minerva; скринька зникла / вимкнена — SMTP сповіщень
і попередження; вибрати скриньку — власник або адміністратор Minerva. SMTP підмінено.

python manage.py test dashboard.tests_customer_mail --settings=tabele.settings_test
"""
import email
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend
from django.http import QueryDict
from django.test import TestCase

from config.models import NotificationSettings
from dashboard import customer_mail as cm
from dashboard import notifications

NS_FROM = "minerva@firma.example"


def _ns(**kw):
    ns = NotificationSettings.get()
    ns.email_enabled, ns.email_host = True, "smtp.firma.example"
    ns.email_host_user = ns.email_from = NS_FROM
    ns.customer_notify_enabled = ns.order_confirm_notify_enabled = True
    ns.order_confirm_notify_sources = ""
    ns.customer_notify_subject, ns.customer_notify_body = "Versand {order_number}", "Tracking {tracking_number}"
    ns.order_confirm_notify_subject, ns.order_confirm_notify_body = "Bestellung {order_number}", "Danke {items}"
    ns.customer_notify_cc = ns.order_confirm_notify_cc = ""
    for key, value in kw.items():
        setattr(ns, key, value)
    ns.save()
    return ns


def _person(username, role="manager"):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    user.profile.save()
    return User.objects.get(pk=user.pk)


def _post(**values):
    q = QueryDict(mutable=True)
    q.update({"cm_seen": "1", **values})
    return q


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from email_assistant.models import EmailAccount
        from sales.models import SalesOrder
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.admin = User.objects.create_superuser("chef", "chef@x.y", "p")
        self.anna = _person("anna")
        self.box_admin = EmailAccount.objects.create(user=self.admin, email_address="chef@firma.example",
                                                     display_name="Chef", smtp_host="smtp.firma.example")
        self.box_anna = EmailAccount.objects.create(user=self.anna, email_address="anna@firma.example",
                                                    display_name="Anna", smtp_host="smtp.firma.example")
        self.order = SalesOrder.objects.create(source="manual", order_number="A-7", email="kunde@client.example",
                                               tracking_number="1Z999")
        p = mock.patch("dashboard.notifications._smtp_connection", side_effect=lambda ns: EmailBackend())
        p.start()
        self.addCleanup(p.stop)

    def smtp(self, ok=True):
        return mock.patch("email_assistant.smtp_client.SMTPClient.send", autospec=True,
                          return_value={"ok": ok, "error": "" if ok else "535 Authentication failed"})

    def route(self, acc):
        return f"acc:{acc.pk}"


class SenderTests(_Base):
    def test_default_is_notification_smtp(self):
        ns = _ns()
        for kind in cm.KINDS:
            s = cm.sender(ns, kind)
            self.assertEqual((s.account, s.address, s.problem), (None, NS_FROM, ""))

    def test_group_and_per_letter_choice(self):
        ns = _ns(customer_senders={"all": self.route(self.box_anna), "confirm": "ns"})
        ship, confirm = cm.sender(ns, "ship"), cm.sender(ns, "confirm")
        self.assertEqual((ship.account, ship.address), (self.box_anna, "anna@firma.example"))
        self.assertEqual((confirm.account, confirm.address), (None, NS_FROM))
        ns = _ns(customer_senders={"ship": self.route(self.box_admin)})       # лише один лист — зі скриньки
        self.assertEqual(cm.sender(ns, "ship").account, self.box_admin)
        self.assertIsNone(cm.sender(ns, "confirm").account)

    def test_gone_inactive_or_no_smtp_falls_back(self):
        ns = _ns(customer_senders={"all": self.route(self.box_anna)})
        self.box_anna.is_active = False
        self.box_anna.save()
        s = cm.sender(ns, "ship")
        self.assertEqual((s.account, s.address), (None, NS_FROM))
        self.assertIn("anna@firma.example", s.problem)
        self.box_anna.is_active, self.box_anna.smtp_host = True, ""
        self.box_anna.save()
        self.assertIn("SMTP", cm.sender(ns, "ship").problem)
        pk = self.box_anna.pk
        self.box_anna.delete()
        s = cm.sender(ns, "confirm")
        self.assertEqual((s.account, s.address), (None, NS_FROM))
        self.assertIn(f"№{pk} видалено", s.problem)


class SendTests(_Base):
    """Обидва листи, автоматично й кнопкою в картці замовлення, — з вибраної адреси."""

    def test_auto_letters_from_mailbox(self):
        _ns(customer_senders={"all": self.route(self.box_anna)}, customer_notify_cc="lager@firma.example")
        with self.smtp() as send:
            self.assertTrue(notifications.send_ship_notification(self.order))
            self.assertTrue(notifications.send_order_confirm_notification(self.order))
        self.assertEqual(mail.outbox, [])                                    # не через SMTP сповіщень
        self.assertEqual([c.args[0].account for c in send.call_args_list], [self.box_anna, self.box_anna])
        args, kw = send.call_args_list[0]
        self.assertEqual(args[1], ["kunde@client.example"])
        self.assertEqual(args[2], "Versand A-7")
        self.assertEqual(kw["cc_emails"], ["lager@firma.example"])
        self.assertFalse(kw["signature"])                                    # текст — як у шаблоні
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.ship_notify_sent_at)
        self.assertIsNotNone(self.order.order_confirm_sent_at)

    def test_auto_letters_from_notification_smtp_by_default_and_per_letter(self):
        _ns(customer_senders={"all": self.route(self.box_anna), "confirm": "ns"})
        with self.smtp() as send:
            self.assertTrue(notifications.send_order_confirm_notification(self.order))
        send.assert_not_called()
        self.assertEqual([(m.from_email, m.to) for m in mail.outbox], [(NS_FROM, ["kunde@client.example"])])

    def test_fallback_when_mailbox_inactive(self):
        _ns(customer_senders={"ship": self.route(self.box_anna)})
        self.box_anna.is_active = False
        self.box_anna.save()
        with self.smtp() as send, self.assertLogs("dashboard", "WARNING"):
            self.assertTrue(notifications.send_ship_notification(self.order))
        send.assert_not_called()
        self.assertEqual([m.from_email for m in mail.outbox], [NS_FROM])

    def test_mailbox_error_is_not_sent_and_not_marked(self):
        _ns(customer_senders={"all": self.route(self.box_anna)})
        with self.smtp(ok=False):
            self.assertFalse(notifications.send_ship_notification(self.order))
        self.assertEqual(mail.outbox, [])                                    # не з іншої адреси без питання
        self.order.refresh_from_db()
        self.assertIsNone(self.order.ship_notify_sent_at)

    def test_order_card_buttons(self):
        _ns(customer_senders={"confirm": self.route(self.box_admin)})
        self.client.force_login(self.admin)
        base = f"/admin/sales/salesorder/{self.order.pk}"
        r = self.client.get(f"{base}/notify-order-confirm/")
        self.assertEqual((r.json()["from"], r.json()["from_note"]), ("chef@firma.example", "скринька пошти Minerva"))
        self.assertEqual(self.client.get(f"{base}/notify-customer/").json()["from"], NS_FROM)
        body = json.dumps({"to": "kunde@client.example", "cc": "", "subject": "Ihre Bestellung", "body": "Hallo"})
        with self.smtp() as send:
            r1 = self.client.post(f"{base}/notify-order-confirm/", body, content_type="application/json")
            r2 = self.client.post(f"{base}/notify-customer/", body, content_type="application/json")
        self.assertEqual((r1.json(), r2.json()), ({"ok": True}, {"ok": True}))
        self.assertEqual([c.args[0].account for c in send.call_args_list], [self.box_admin])
        self.assertEqual([m.from_email for m in mail.outbox], [NS_FROM])
        with self.smtp(ok=False):
            r = self.client.post(f"{base}/notify-order-confirm/", body, content_type="application/json")
        self.assertEqual(r.json(), {"ok": False, "error": "535 Authentication failed"})

    def test_mailbox_smtp_message(self):
        """Справжній SMTPClient: From — скринька, без її підпису, копія — у «Надіслані»."""
        from email_assistant.smtp_client import SMTPClient
        self.box_anna.signature = "Anna Muster<br>Firma GmbH"
        self.box_anna.save()
        ns = _ns(customer_senders={"all": self.route(self.box_anna)})
        sent, saved = {}, []

        class Conn:
            def login(self, *a):
                pass

            def sendmail(self, frm, to, data):
                sent.update(frm=frm, to=to, data=data)

            def quit(self):
                pass

        imap = mock.MagicMock()
        imap.return_value.__enter__.return_value.append_to_sent.side_effect = saved.append
        with mock.patch.object(SMTPClient, "_open", return_value=Conn()), \
                mock.patch("email_assistant.imap_client.IMAPClient", imap):
            s = cm.send(ns, "ship", ["kunde@client.example"], "Versand", "Text", "<p>Text</p>")
        self.assertEqual(s.account, self.box_anna)
        msg = email.message_from_bytes(sent["data"])
        self.assertIn("anna@firma.example", msg["From"])
        self.assertEqual((sent["frm"], sent["to"]), ("anna@firma.example", ["kunde@client.example"]))
        self.assertNotIn("Firma GmbH", sent["data"].decode())
        self.assertEqual(saved, [sent["data"]])


class ChoiceRightsTests(_Base):
    """Скриньки особисті: свою вибирає кожен, чужу — лише адміністратор Minerva; уже вибрана чужа лишається."""

    def test_options(self):
        ns = _ns()
        self.assertEqual([v for v, _ in cm.options(ns, self.anna)], [self.route(self.box_anna)])
        self.assertEqual({v for v, _ in cm.options(ns, self.admin)},
                         {self.route(self.box_anna), self.route(self.box_admin)})
        ns = _ns(customer_senders={"all": self.route(self.box_admin)})        # вибрав адміністратор — видно
        self.assertEqual([v for v, _ in cm.options(ns, self.anna)],
                         [self.route(self.box_anna), self.route(self.box_admin)])
        self.assertEqual([v for v, _ in cm.options(ns, self.anna, "ship")], [self.route(self.box_anna)])
        self.assertEqual(cm.options(ns, None), [(self.route(self.box_admin), "chef@firma.example — chef")])

    def test_non_admin_picks_only_own(self):
        ns = _ns()
        refused = cm.save(ns, _post(cm_all=self.route(self.box_admin)), self.anna)
        self.assertEqual((refused, ns.customer_senders), (["chef@firma.example"], {}))
        refused = cm.save(ns, _post(cm_all=self.route(self.box_anna), cm_confirm="ns", cm_ship=""), self.anna)
        self.assertEqual(refused, [])
        self.assertEqual(ns.customer_senders, {"all": self.route(self.box_anna), "confirm": "ns"})

    def test_admin_picks_any_and_choice_survives_colleague_save(self):
        ns = _ns()
        self.assertEqual(cm.save(ns, _post(cm_ship=self.route(self.box_anna)), self.admin), [])
        self.assertEqual(ns.customer_senders, {"ship": self.route(self.box_anna)})
        ns = _ns(customer_senders={"all": self.route(self.box_admin)})
        # колега зберігає сторінку, не чіпаючи меню — чужа скринька лишається
        self.assertEqual(cm.save(ns, _post(cm_all=self.route(self.box_admin), cm_ship="", cm_confirm=""),
                                 self.anna), [])
        self.assertEqual(ns.customer_senders, {"all": self.route(self.box_admin)})
        cm.save(ns, _post(cm_all=""), self.anna)                              # прибрати — можна
        self.assertEqual(ns.customer_senders, {})

    def test_inactive_or_unknown_refused(self):
        ns = _ns()
        self.box_anna.is_active = False
        self.box_anna.save()
        self.assertEqual(cm.save(ns, _post(cm_all=self.route(self.box_anna)), self.admin), ["anna@firma.example"])
        self.assertEqual(cm.save(ns, _post(cm_ship="acc:999", cm_confirm="evil"), self.admin), ["acc:999", "evil"])
        self.assertEqual(ns.customer_senders, {})
        self.assertEqual(cm.save(ns, QueryDict("cm_all=acc:1"), self.admin), [])   # без cm_seen — не таблиця


class PageTests(_Base):
    def test_header_select_and_warning(self):
        _ns(customer_senders={"all": self.route(self.box_anna), "ship": "ns"})
        self.client.force_login(self.admin)
        page = self.client.get("/admin/config/notificationsettings/1/change/").content.decode()
        self.assertIn('👤 Клієнтам — з адреси <select name="cm_all"', page)
        self.assertIn(f'<option value="{self.route(self.box_anna)}" selected>anna@firma.example — anna', page)
        self.assertIn('name="cm_ship"', page)
        self.assertIn('<option value="ns" selected>', page)
        self.assertIn('→ піде з <b translate="no">anna@firma.example</b>', page)
        self.assertIn(f'→ піде з <b translate="no">{NS_FROM}</b> · SMTP сповіщень', page)
        self.assertNotIn('&lt;div class=&quot;nt-when', page)
        self.box_anna.delete()
        page = self.client.get("/admin/config/notificationsettings/1/change/").content.decode()
        self.assertIn("видалено — листи клієнтам ідуть з SMTP сповіщень", page)

    def test_save_through_page(self):
        ns = _ns()
        self.client.force_login(self.admin)
        url = "/admin/config/notificationsettings/1/change/"
        form = self.client.get(url).context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None and not isinstance(v, (list, dict))}
        data.update({k: "on" for k, v in form.initial.items() if v is True})
        for k in [k for k, v in data.items() if v is False]:
            data.pop(k)
        data.update({"cm_seen": "1", "cm_all": self.route(self.box_anna), "cm_ship": "", "cm_confirm": "ns"})
        r = self.client.post(url, data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        ns.refresh_from_db()
        self.assertEqual(ns.customer_senders, {"all": self.route(self.box_anna), "confirm": "ns"})
