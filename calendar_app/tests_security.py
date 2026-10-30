"""Календар: вікно події без чужого HTML, місцевий час, повторювані події на весь день, адреси підписок лише в
інтернет. python manage.py test calendar_app.tests_security --settings=tabele.settings_test (мережі немає)."""
import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from calendar_app import feeds
from calendar_app.models import CalendarEvent, CalendarFeed

BERLIN = ZoneInfo("Europe/Berlin")


class PopupTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def test_popup_escapes_mail_data(self):
        html = self.client.get("/calendar/").content.decode()
        popup = html[html.index("async function showEventPopup"):]
        popup = popup[:popup.index("cal-popup-meta').innerHTML = meta")]
        for field in ("d.customer", "d.source_email.subject", "d.description"):
            self.assertIn(f"_blkEsc({field}", popup, field)
        self.assertNotIn("+ d.description;", popup)
        self.assertNotIn("+ d.customer +", popup)

    def test_custom_category_names_cannot_close_script(self):
        from calendar_app.models import CalendarCategory
        CalendarCategory.objects.create(user=self.user, name="</script><script>alert(1)</script>", color="#123456",
                                        emoji="x")
        html = self.client.get("/calendar/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_event_times_are_local(self):
        start = datetime.datetime(2026, 7, 1, 7, 30, tzinfo=datetime.timezone.utc)      # 09:30 у Берліні (літо)
        ev = CalendarEvent.objects.create(user=self.user, title="T", start_at=start,
                                          end_at=start + datetime.timedelta(hours=1))
        d = self.client.get(f"/calendar/event/{ev.pk}/").json()
        self.assertEqual((d["start"], d["end"]), ("01.07.2026 09:30", "01.07.2026 10:30"))


def ics(*events):
    return "BEGIN:VCALENDAR\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def vevent(uid, start_line, rrule, end_line=""):
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{uid}\r\n{start_line}\r\n" + (end_line + "\r\n" if end_line else "")
            + f"RRULE:{rrule}\r\nEND:VEVENT\r\n")


class RecurringAllDayTests(TestCase):
    def window(self):
        now = timezone.now()
        return now - datetime.timedelta(days=1), now + datetime.timedelta(days=60)

    def starts(self, text, uid):
        return sorted(e["start"] for e in feeds.expand(feeds.parse(text), *self.window()) if e["uid"] == uid)

    def test_all_day_with_date_until(self):
        first = timezone.localdate() + datetime.timedelta(days=1)
        until = first + datetime.timedelta(days=21)
        text = ics(vevent("weekly-allday", f"DTSTART;VALUE=DATE:{first:%Y%m%d}", f"FREQ=WEEKLY;UNTIL={until:%Y%m%d}",
                          f"DTEND;VALUE=DATE:{first + datetime.timedelta(days=1):%Y%m%d}"))
        got = self.starts(text, "weekly-allday")
        self.assertEqual([s.astimezone(BERLIN).date() for s in got],
                         [first + datetime.timedelta(days=7 * i) for i in range(4)])   # UNTIL включно
        self.assertTrue(all(s.astimezone(BERLIN).hour == 0 for s in got))

    def test_all_day_with_utc_until_from_outlook(self):
        first = timezone.localdate() + datetime.timedelta(days=1)
        last = first + datetime.timedelta(days=14)
        until = datetime.datetime.combine(last, datetime.time(0, 0), BERLIN).astimezone(datetime.timezone.utc)
        text = ics(vevent("outlook", f"DTSTART;VALUE=DATE:{first:%Y%m%d}", f"FREQ=WEEKLY;UNTIL={until:%Y%m%dT%H%M%SZ}"))
        self.assertEqual(len(self.starts(text, "outlook")), 3)

    def test_timed_event_with_date_until(self):
        first = timezone.localdate() + datetime.timedelta(days=1)
        until = first + datetime.timedelta(days=2)
        text = ics(vevent("daily", f"DTSTART;TZID=Europe/Berlin:{first:%Y%m%d}T090000",
                          f"FREQ=DAILY;UNTIL={until:%Y%m%d}"))
        got = self.starts(text, "daily")
        self.assertEqual(len(got), 3)
        self.assertTrue(all(s.astimezone(BERLIN).hour == 9 for s in got))

    def test_broken_rule_outside_window_is_dropped(self):
        text = ics(vevent("old", "DTSTART;TZID=Europe/Berlin:20190107T090000", "FREQ=NONSENSE"))
        self.assertEqual(self.starts(text, "old"), [])                # не «подія з 2019 року» у вікні


def resp(status=200, body="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", location=""):
    r = mock.Mock(status_code=status, encoding="utf-8", headers={"Location": location} if location else {})
    r.raise_for_status = mock.Mock() if status < 400 else mock.Mock(side_effect=requests.HTTPError())
    r.raw.read.return_value = body.encode()
    return r


def dns(mapping):
    def fake(host, *a, **kw):
        ip = mapping.get(host, "93.184.216.34")
        return [(2, 1, 6, "", (ip, 443))]
    return mock.patch("email_assistant.logos.socket.getaddrinfo", side_effect=fake)


class FeedAddressTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True)

    def test_private_address_is_not_fetched(self):
        feed = CalendarFeed.objects.create(user=self.user, name="x", url="https://nas.example/cal.ics")
        with dns({"nas.example": "192.168.2.123"}), mock.patch("calendar_app.feeds.requests.get") as get:
            self.assertEqual(feeds.fetch(feed), 0)
        get.assert_not_called()
        feed.refresh_from_db()
        self.assertIn("не в інтернет", feed.last_error)

    def test_redirect_to_private_address_is_refused(self):
        feed = CalendarFeed.objects.create(user=self.user, name="x", url="webcal://cal.example/a.ics")
        with dns({"cal.example": "93.184.216.34", "127.0.0.1": "127.0.0.1"}), \
                mock.patch("calendar_app.feeds.requests.get",
                           return_value=resp(302, location="http://127.0.0.1:8000/admin/")) as get:
            feeds.fetch(feed)
        self.assertEqual(get.call_count, 1)                           # за переадресацію не пішли
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        feed.refresh_from_db()
        self.assertIn("не в інтернет", feed.last_error)

    def test_public_redirect_is_followed(self):
        feed = CalendarFeed.objects.create(user=self.user, name="x", url="https://cal.example/a.ics")
        with dns({}), mock.patch("calendar_app.feeds.requests.get",
                                 side_effect=[resp(301, location="https://cdn.example/b.ics"), resp()]) as get:
            feeds.fetch(feed)
        self.assertEqual(get.call_args_list[1].args[0], "https://cdn.example/b.ics")
        feed.refresh_from_db()
        self.assertEqual(feed.last_error, "")
