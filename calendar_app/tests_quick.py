"""⚡ Швидке підключення календарів (одне посилання, без ключів): python manage.py test calendar_app
--settings=tabele.settings_test"""
from unittest import mock
from urllib.parse import unquote

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase

from calendar_app import feeds
from calendar_app.models import CalendarFeed, CalendarSettings, ExternalEvent

GOOGLE = "https://calendar.google.com/calendar/ical/anna%40gmail.com/private-abc123/basic.ics"
OUTLOOK = "https://outlook.office365.com/owa/calendar/1f2e@sevskiy.de/9a8b/calendar.ics"
ICS = ("BEGIN:VCALENDAR\r\nX-WR-CALNAME:Anna privat\r\nBEGIN:VEVENT\r\nUID:a1\r\nSUMMARY:Zahnarzt\r\n"
       "DTSTART:20261103T090000Z\r\nDTEND:20261103T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def resp(body=ICS, status=200):
    r = mock.Mock(status_code=status, encoding="utf-8")
    if status >= 400:
        r.raise_for_status = mock.Mock(side_effect=requests.HTTPError(response=mock.Mock(status_code=status)))
    else:
        r.raise_for_status = mock.Mock()
    r.raw.read.return_value = body.encode()
    return r


class DetectTests(TestCase):
    def test_providers_and_wrong_links(self):
        self.assertEqual(feeds.detect(GOOGLE), {"provider": "google", "error": "", "warning": ""})
        self.assertEqual(feeds.detect(OUTLOOK)["provider"], "microsoft")
        self.assertIn("HTML", feeds.detect(OUTLOOK.replace(".ics", ".html"))["error"])
        self.assertIn("Секретна адреса", feeds.detect("https://calendar.google.com/calendar/u/0/r")["error"])
        self.assertIn("публічна", feeds.detect(GOOGLE.replace("private-abc123", "public"))["warning"])
        self.assertIn("https://", feeds.detect("calendar.google.com/x")["error"])

    def test_add_links(self):
        links = feeds.add_links("https://sevskiy.minerva-bi.com/calendar/feed/tok.ics")
        self.assertEqual(unquote(links["google"].split("cid=")[1]), "webcal://sevskiy.minerva-bi.com/calendar/feed/tok.ics")
        self.assertIn("outlook.office.com/calendar/0/addfromweb?url=https%3A%2F%2Fsevskiy", links["outlook"])
        self.assertTrue(links["outlook_live"].startswith("https://outlook.live.com/"))


def public_dns(test):
    """Без мережі: хости календарів вважаються публічними (перевірку адрес — feeds._public — тестує tests_security)."""
    p = mock.patch("calendar_app.feeds._public", return_value=True)
    p.start()
    test.addCleanup(p.stop)


class QuickAddTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        public_dns(self)

    def test_name_color_and_events_from_one_link(self):
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp()):
            feed, n, error, warning = feeds.quick_add(self.user, GOOGLE)
        self.assertEqual((n, error, feed.name, feed.color, feed.refresh_minutes), (1, "", "Anna privat", "#1a73e8", 15))
        self.assertEqual(ExternalEvent.objects.get(feed=feed).title, "Zahnarzt")
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp()):
            again, _, _, warning = feeds.quick_add(self.user, GOOGLE)
        self.assertEqual((again.pk, CalendarFeed.objects.count()), (feed.pk, 1))
        self.assertIn("уже підключено", warning)

    def test_bad_links_leave_nothing(self):
        _, _, error, _ = feeds.quick_add(self.user, OUTLOOK.replace(".ics", ".html"))
        self.assertIn("HTML", error)
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp(status=404)):
            _, _, error, _ = feeds.quick_add(self.user, OUTLOOK)
        self.assertIn("не діє", error)
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp(body="<html>login</html>")):
            _, _, error, _ = feeds.quick_add(self.user, GOOGLE)
        self.assertIn("не календар", error)
        self.assertFalse(CalendarFeed.objects.exists())

    def test_no_calname_uses_provider(self):
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp(ICS.replace("X-WR-CALNAME:Anna privat\r\n", ""))):
            feed, *_ = feeds.quick_add(self.user, OUTLOOK)
        self.assertEqual((feed.name, feed.color), ("Outlook", "#f97316"))


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        public_dns(self)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)

    def test_quick_card_connect_and_add_to(self):
        page = self.client.get("/calendar/connections/")
        self.assertContains(page, "Просте підключення по кроках")
        self.assertContains(page, 'data-default="simple"')   # ключів і акаунтів ще немає — спершу просте
        self.assertContains(page, "Підключення по кроках — із записом")   # повне лишається на сторінці
        self.assertContains(page, "calendar.google.com/calendar/r/settings")
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp()):
            r = self.client.post("/calendar/connections/", {"action": "quick_add", "url": GOOGLE}, follow=True)
        self.assertContains(r, "Підключено «Anna privat»")
        self.assertContains(r, "?only=f:")   # крок «звірити з оригіналом» — лише цей календар
        r = self.client.post("/calendar/connections/", {"action": "add_to", "to": "google"})
        token = CalendarSettings.for_user(self.user).feed_token
        self.assertTrue(token)   # адреси ще не було — створено
        self.assertTrue(r["Location"].startswith("https://calendar.google.com/calendar/r?cid=webcal"))
        self.assertIn(token, unquote(r["Location"]))


class RoadmapTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True)

    def states(self, provider="google", token=""):
        r = next(x for x in feeds.quick_roadmap(self.user, token) if x["provider"] == provider)
        return {s["key"]: s["state"] for s in r["steps"]}

    def test_steps(self):
        self.assertEqual(self.states()["link"], "current")
        feed = CalendarFeed.objects.create(user=self.user, name="Anna", url=GOOGLE, last_error="HTTP 404")
        st = self.states()
        self.assertEqual((st["link"], st["show"]), ("done", "current"))
        CalendarFeed.objects.filter(pk=feed.pk).update(last_error="", events_count=12)
        st = self.states()
        self.assertEqual((st["show"], st["compare"], st["back"]), ("done", "done", "current"))
        st = self.states(token="tok")
        self.assertEqual((st["back"], st["decide"]), ("done", "current"))
        self.assertEqual(self.states("microsoft")["link"], "current")   # Outlook — окремо

