"""📦 Дедлайни замовлень у календарі (calendar_app/order_deadlines.py): подія ↔ замовлення, відправлене /
скасоване прибирається само, «Лише критичні», дочищення старих подій.

python manage.py test calendar_app.tests_order_deadlines --settings=tabele.settings_test
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from calendar_app import order_deadlines
from calendar_app.models import CalendarEvent, CalendarSettings
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings
from sales.models import SalesOrder

ME = "anna@sevskiy.de"


class OrderDeadlineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", ME, "x", is_staff=True, is_superuser=True)
        self.today = timezone.localdate()
        self.order = self._order("102330189", self.today + datetime.timedelta(days=15))

    def _order(self, number, deadline, **kw):
        return SalesOrder.objects.create(source="digikey", order_number=number, client="AEVEX", status="received",
                                         affects_stock=True, shipping_deadline=deadline, **kw)

    def _event(self, title="Дедлайн замовлення 102330189 (AEVEX)", days=15, **kw):
        start = timezone.make_aware(datetime.datetime.combine(self.today + datetime.timedelta(days=days),
                                                              datetime.time(9)))
        return CalendarEvent.objects.create(user=self.user, title=title, event_type=CalendarEvent.TYPE_DEADLINE,
                                            start_at=start, **kw)

    def _ship(self, order, status="shipped"):
        order.status = status
        order.shipped_at = timezone.now() if status != "cancelled" else None
        order.save()

    def _mode(self, mode, days=3):
        cfg = CalendarSettings.for_user(self.user)
        cfg.order_deadlines, cfg.order_deadline_days = mode, days
        cfg.save()

    def test_new_deadline_is_linked_to_its_order(self):
        ev = self._event()
        ev.refresh_from_db()
        self.assertEqual((ev.sales_order, ev.order_state, ev.is_done), (self.order, CalendarEvent.ORDER_OPEN, False))

    def test_shipping_closes_and_reverting_reopens(self):
        ev = self._event()
        self._ship(self.order)
        ev.refresh_from_db()
        self.assertEqual((ev.is_done, ev.order_state), (True, CalendarEvent.ORDER_CLOSED))
        self.order.status, self.order.shipped_at = "processing", None    # помилково позначили — повернули
        self.order.save()
        ev.refresh_from_db()
        self.assertEqual((ev.is_done, ev.order_state), (False, CalendarEvent.ORDER_OPEN))

    def test_cancelled_order_closes_too(self):
        ev = self._event()
        self._ship(self.order, "cancelled")
        ev.refresh_from_db()
        self.assertTrue(ev.is_done)

    def test_deadline_of_already_shipped_order_is_closed_at_once(self):
        self._ship(self.order)
        ev = self._event()
        ev.refresh_from_db()
        self.assertEqual((ev.is_done, ev.order_state), (True, CalendarEvent.ORDER_CLOSED))

    def test_mode_all_keeps_everything_as_before(self):
        self._mode(CalendarSettings.ORDER_DEADLINES_ALL)
        ev = self._event()
        self._ship(self.order)
        ev.refresh_from_db()
        self.assertEqual((ev.sales_order, ev.is_done), (self.order, False))

    def test_done_by_hand_is_not_reopened(self):
        ev = self._event()
        CalendarEvent.objects.filter(pk=ev.pk).update(is_done=True)
        self.order.status = "processing"
        self.order.save()
        ev.refresh_from_db()
        self.assertTrue(ev.is_done)

    def test_order_found_through_the_mail_of_the_event(self):
        account = EmailAccount.objects.create(user=self.user, email_address=ME, imap_username=ME, imap_password="x",
                                              smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        mail = EmailMessage.objects.create(account=account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name="INBOX",
                                           subject="Minerva: Нове замовлення 102330189", from_email=ME,
                                           to_emails=[ME], cc_emails=[], body_text="Клієнт: AEVEX",
                                           sent_at=timezone.now())
        ev = self._event(title="Відправити AEVEX", email_message=mail)
        ev.refresh_from_db()
        self.assertEqual(ev.sales_order, self.order)

    def test_other_events_and_unknown_numbers(self):
        meeting = CalendarEvent.objects.create(user=self.user, title="Call 102330189", start_at=timezone.now(),
                                               event_type=CalendarEvent.TYPE_MEETING)
        meeting.refresh_from_db()
        self.assertEqual((meeting.sales_order, meeting.order_state), (None, ""))     # лише дедлайни
        ev = self._event(title="Дедлайн звіту 2026")
        ev.refresh_from_db()
        self.assertEqual((ev.sales_order, ev.order_state), (None, CalendarEvent.ORDER_NONE))

    def test_sweep_links_old_events_and_closes_shipped_without_signal(self):
        ev = self._event()
        CalendarEvent.objects.filter(pk=ev.pk).update(sales_order=None, order_state="")   # подія з минулого
        SalesOrder.objects.filter(pk=self.order.pk).update(status="shipped", shipped_at=timezone.now())   # без сигналу
        result = order_deadlines.sweep(user=self.user)
        ev.refresh_from_db()
        self.assertEqual((result["linked"], result["closed"], result["left"]), (1, 1, 0))
        self.assertEqual((ev.sales_order, ev.is_done, ev.order_state), (self.order, True, CalendarEvent.ORDER_CLOSED))

    def test_critical_mode_shows_only_near_and_overdue(self):
        self._mode(CalendarSettings.ORDER_DEADLINES_CRITICAL, days=3)
        near = self._order("102330190", self.today + datetime.timedelta(days=2))
        late = self._order("102330191", self.today - datetime.timedelta(days=2))
        far = self._event()                                                     # 102330189 — через 15 днів
        soon = self._event(title=f"Дедлайн замовлення {near.order_number}", days=2)
        overdue = self._event(title=f"Дедлайн замовлення {late.order_number}", days=-2)
        other = self._event(title="Дедлайн звіту", days=20)                     # не замовлення — як завжди
        shown = set(order_deadlines.visible(CalendarEvent.objects.filter(user=self.user), self.user)
                    .values_list("pk", flat=True))
        self.assertEqual(shown, {soon.pk, overdue.pk, other.pk})
        self.assertNotIn(far.pk, shown)
        self.assertEqual(order_deadlines.hidden_count(self.user), 1)

    def test_feed_has_no_closed_deadlines(self):
        from calendar_app import feeds
        self._event(title="Дедлайн замовлення 102330189 (AEVEX)")
        self.assertIn("102330189", feeds.export(self.user, "minerva.test"))
        self._ship(self.order)
        self.assertNotIn("102330189", feeds.export(self.user, "minerva.test"))

    def test_settings_page_saves_rule_and_cleans_up(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        ev = self._event()
        CalendarEvent.objects.filter(pk=ev.pk).update(sales_order=None, order_state="")
        SalesOrder.objects.filter(pk=self.order.pk).update(status="delivered", shipped_at=timezone.now())
        self.client.force_login(self.user)
        r = self.client.post("/calendar/settings/", {"order_deadlines": "critical", "order_deadline_days": "5",
                                                     "default_remind_minutes": "60"})
        self.assertContains(r, "закрито відправлених / скасованих 1")
        cfg = CalendarSettings.for_user(self.user)
        self.assertEqual((cfg.order_deadlines, cfg.order_deadline_days), ("critical", 5))
        ev.refresh_from_db()
        self.assertTrue(ev.is_done)
        self.assertContains(self.client.get("/calendar/settings/"), 'id="orders"')
