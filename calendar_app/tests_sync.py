"""Календарі: підписки iCal і синхронізація Google / Outlook / Minerva (зовнішні API імітуються)."""
import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from calendar_app import feeds, sync
from calendar_app.models import (CalendarAccount, CalendarEvent, CalendarFeed, CalendarSettings, CalendarSyncSettings,
                                 ExternalEvent, RemoteCalendar, SyncLink, SyncRule)

BERLIN = ZoneInfo("Europe/Berlin")
WEEKLY = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:jour-fixe\r\nSUMMARY:Jour fixe\r\n"
          "DTSTART;TZID=Europe/Berlin:{start}\r\nDTEND;TZID=Europe/Berlin:{end}\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\n"
          "EXDATE;TZID=Europe/Berlin:{ex}\r\nEND:VEVENT\r\n"
          "BEGIN:VEVENT\r\nUID:jour-fixe\r\nRECURRENCE-ID;TZID=Europe/Berlin:{moved}\r\nSUMMARY:Jour fixe (verschoben)\r\n"
          "DTSTART;TZID=Europe/Berlin:{moved_to}\r\nDTEND;TZID=Europe/Berlin:{moved_end}\r\nEND:VEVENT\r\n"
          "BEGIN:VEVENT\r\nUID:allday\r\nSUMMARY:Messe\r\nDTSTART;VALUE=DATE:{day}\r\nDTEND;VALUE=DATE:{day2}\r\n"
          "END:VEVENT\r\nEND:VCALENDAR\r\n")


def week_ics():
    base = timezone.localdate() + datetime.timedelta(days=1)
    f = lambda d, h: f"{d:%Y%m%d}T{h}0000"   # noqa: E731
    w = datetime.timedelta(days=7)
    return WEEKLY.format(start=f(base, "09"), end=f(base, "10"), ex=f(base + w, "09"), moved=f(base + 2 * w, "09"),
                         moved_to=f(base + 2 * w, "14"), moved_end=f(base + 2 * w, "15"),
                         day=f"{base:%Y%m%d}", day2=f"{base + datetime.timedelta(days=1):%Y%m%d}"), base


class FeedTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        from calendar_app.tests_quick import public_dns
        public_dns(self)

    def test_expand_rrule_exdate_override(self):
        text, base = week_ics()
        now = timezone.now()
        items = feeds.expand(feeds.parse(text), now - datetime.timedelta(days=1), now + datetime.timedelta(days=60))
        jour = sorted((e["start"].astimezone(BERLIN), e["title"]) for e in items if e["uid"] == "jour-fixe")
        self.assertEqual([t for _, t in jour], ["Jour fixe", "Jour fixe (verschoben)", "Jour fixe"])   # EXDATE пропущено
        self.assertEqual(jour[1][0].hour, 14)
        self.assertTrue(next(e for e in items if e["uid"] == "allday")["all_day"])

    def test_fetch_and_calendar_layer(self):
        text, base = week_ics()
        resp = mock.Mock(status_code=200, encoding="utf-8")
        resp.raise_for_status = mock.Mock()
        resp.raw.read.return_value = text.encode()
        feed = CalendarFeed.objects.create(user=self.user, name="Google", url="webcal://calendar.google.com/x.ics")
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp) as get:
            self.assertEqual(feeds.fetch(feed), 4)
        self.assertTrue(get.call_args.args[0].startswith("https://"))
        self.assertEqual(ExternalEvent.objects.filter(feed=feed).count(), 4)
        resp.raw.read.return_value = b"<html>login</html>"
        with mock.patch("calendar_app.feeds.requests.get", return_value=resp):
            feeds.fetch(feed)
        feed.refresh_from_db()
        self.assertIn("не календар", feed.last_error)
        self.assertEqual(ExternalEvent.objects.filter(feed=feed).count(), 4)   # старі події лишились
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        r = self.client.get(f"/calendar/?year={base.year}&month={base.month}")
        self.assertContains(r, "cal-ext")

    def test_export_feed_token_busy(self):
        CalendarEvent.objects.create(user=self.user, title="Geheim, Projekt", start_at=timezone.now(),
                                     event_type=CalendarEvent.TYPE_MEETING)
        cfg = CalendarSettings.for_user(self.user)
        token = feeds.new_token(cfg)
        r = self.client.get(f"/calendar/feed/{token}.ics")   # без входу в Minerva
        self.assertEqual(r.status_code, 200)
        self.assertIn("SUMMARY:Geheim\\, Projekt", r.content.decode())
        cfg.feed_busy_only = True
        cfg.save()
        self.assertIn("SUMMARY:Зайнято", self.client.get(f"/calendar/feed/{token}.ics").content.decode())
        self.assertEqual(self.client.get("/calendar/feed/wrong-token-0000000000000.ics").status_code, 404)


class FakeRemote:
    """Календарі Google / Outlook у пам'яті: {cal_id: {event_id: event}}."""
    store = {}
    counter = [0]

    def __init__(self, acc):
        self.acc = acc

    def email(self):
        return f"{self.acc.provider}@x.y"

    def calendars(self):
        return [{"id": f"{self.acc.provider}-main", "name": "Main", "color": "#123456", "primary": True,
                 "can_write": True}]

    def events(self, cal_id, start, end):
        return [dict(e, id=i) for i, e in self.store.setdefault(cal_id, {}).items() if start <= e["start"] <= end]

    def create(self, cal_id, ev):
        self.counter[0] += 1
        eid = f"e{self.counter[0]}"
        self.store.setdefault(cal_id, {})[eid] = {k: ev.get(k) for k in
                                                  ("title", "start", "end", "all_day", "location", "description")}
        return eid

    def update(self, cal_id, eid, ev):
        self.store[cal_id][eid].update({k: ev.get(k) for k in ("title", "start", "end", "all_day", "location",
                                                                "description")})

    def delete(self, cal_id, eid):
        self.store.get(cal_id, {}).pop(eid, None)


class SyncTests(TestCase):
    def setUp(self):
        FakeRemote.store = {}
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        p = mock.patch("calendar_app.sync.client", side_effect=FakeRemote)
        p.start()
        self.addCleanup(p.stop)
        self.g = CalendarAccount.objects.create(user=self.user, provider="google", email="g@x.y")
        self.o = CalendarAccount.objects.create(user=self.user, provider="microsoft", email="o@x.y")
        sync.refresh_calendars(self.g)
        sync.refresh_calendars(self.o)
        self.gc, self.oc = self.g.calendars.get(), self.o.calendars.get()

    def ev(self, title, hours=2):
        start = timezone.now() + datetime.timedelta(hours=hours)
        return {"title": title, "start": start, "end": start + datetime.timedelta(hours=1), "all_day": False,
                "location": "Teams", "description": ""}

    def test_two_way_without_loops_update_and_delete(self):
        o2g = SyncRule.objects.create(user=self.user, source=self.oc, target=self.gc)
        g2o = SyncRule.objects.create(user=self.user, source=self.gc, target=self.oc)
        oid = FakeRemote(self.o).create(self.oc.remote_id, self.ev("Kunde ACME"))
        FakeRemote(self.g).create(self.gc.remote_id, self.ev("Zahnarzt", 5))
        for _ in range(2):   # кілька проходів — без дублів
            sync.run(o2g)
            sync.run(g2o)
        g_titles = sorted(e["title"] for e in FakeRemote.store[self.gc.remote_id].values())
        o_titles = sorted(e["title"] for e in FakeRemote.store[self.oc.remote_id].values())
        self.assertEqual(g_titles, ["Kunde ACME", "Zahnarzt"])
        self.assertEqual(o_titles, ["Kunde ACME", "Zahnarzt"])
        FakeRemote.store[self.oc.remote_id][oid]["title"] = "Kunde ACME (neu)"
        self.assertEqual(len(sync.run(o2g)["update"]), 1)
        self.assertIn("Kunde ACME (neu)", [e["title"] for e in FakeRemote.store[self.gc.remote_id].values()])
        del FakeRemote.store[self.oc.remote_id][oid]
        plan = sync.run(o2g)
        self.assertEqual(len(plan["delete"]), 1)
        self.assertNotIn("Kunde ACME (neu)", [e["title"] for e in FakeRemote.store[self.gc.remote_id].values()])
        self.assertEqual(SyncLink.objects.filter(rule=o2g).count(), 0)

    def test_dry_run_busy_and_minerva_side(self):
        FakeRemote(self.o).create(self.oc.remote_id, self.ev("Privat"))
        rule = SyncRule.objects.create(user=self.user, source=self.oc, target=None, mode=SyncRule.BUSY)
        plan = sync.run(rule, dry_run=True)
        self.assertEqual((len(plan["create"]), CalendarEvent.objects.count()), (1, 0))
        sync.run(rule)
        self.assertEqual(CalendarEvent.objects.get().title, "Зайнято")
        back = SyncRule.objects.create(user=self.user, source=None, target=self.gc, what=SyncRule.MAIL)
        self.assertEqual(sync.run(back)["create"], [])   # копія в Minerva не джерело, і не з пошти
        CalendarEvent.objects.create(user=self.user, title="Termin aus Mail", start_at=timezone.now(),
                                     event_type=CalendarEvent.TYPE_MEETING)
        back.what = SyncRule.ALL
        back.save()
        self.assertEqual(len(sync.run(back)["create"]), 1)

    def test_oauth_url_state_and_page(self):
        s = CalendarSyncSettings.get()
        s.google_client_id, s.google_client_secret = "cid.apps.googleusercontent.com", "sec"
        s.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        r = self.client.get("/calendar/connections/oauth/google/start/")
        self.assertIn("accounts.google.com", r["Location"])
        self.assertIn("access_type=offline", r["Location"])
        self.assertEqual(self.client.get("/calendar/connections/oauth/google/callback/?state=bad&code=x").status_code,
                         400)
        r = self.client.get("/calendar/connections/")
        self.assertContains(r, "Redirect URI")
        self.assertContains(r, "＋ Google Calendar")
        self.assertNotContains(r, "sec\"")   # секрет не в сторінці
        r = self.client.post("/calendar/connections/sync/", {"action": "rule_add", "source": self.oc.pk,
                                                             "target": self.gc.pk, "days_back": "x"})
        rule = SyncRule.objects.get()
        self.assertEqual((rule.enabled, rule.days_back), (False, 7))   # спершу перегляд


class LayerAndTargetTests(SyncTests):
    def login(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)

    def test_connected_calendar_as_layer(self):
        self.login()
        FakeRemote(self.o).create(self.oc.remote_id, self.ev("Outlook Termin", 3))
        self.client.post("/calendar/connections/sync/", {"action": "layer", "calendar": self.oc.pk})
        self.oc.refresh_from_db()
        self.assertTrue(self.oc.show_in_minerva)
        self.assertEqual(ExternalEvent.objects.filter(remote=self.oc).count(), 1)
        now = timezone.localtime(timezone.now() + datetime.timedelta(hours=3))
        r = self.client.get(f"/calendar/?year={now.year}&month={now.month}")
        self.assertContains(r, "Outlook Termin")
        self.assertEqual(CalendarEvent.objects.count(), 0)   # лише показ, без копіювання
        self.client.post("/calendar/connections/sync/", {"action": "layer", "calendar": self.oc.pk})
        self.assertFalse(ExternalEvent.objects.filter(remote=self.oc).exists())

    def test_deadline_to_google_and_trash_mask(self):
        from calendar_app import deadlines
        from calendar_app.models import FoundDeadline
        from email_assistant.models import EmailAccount, EmailMessage
        self.login()
        acc = EmailAccount.objects.create(user=self.user, email_address="a@x.y", imap_username="a@x.y",
                                          imap_password="x", smtp_username="a@x.y", smtp_password="x")
        msg = EmailMessage.objects.create(account=acc, from_email="max@acme.example", subject="Lieferung",
                                          body_text="Bitte bis 30.11.2026 liefern.", folder=EmailMessage.FOLDER_TRASH,
                                          sent_at=timezone.now())
        self.assertEqual(deadlines.messages_for(self.user, {}).count(), 0)
        self.assertEqual(deadlines.messages_for(self.user, {"trash": "1"}).count(), 1)
        d = FoundDeadline.objects.create(user=self.user, email_message=msg, title="Lieferung",
                                         due_at=timezone.now() + datetime.timedelta(days=30), quote="bis 30.11.2026")
        r = self.client.get("/calendar/deadlines/")
        self.assertContains(r, "Крок 2")
        self.assertContains(r, "Google: Main")
        self.client.post("/calendar/deadlines/", {"action": "accept", "ids": [d.pk], "target": self.gc.pk})
        d.refresh_from_db()
        self.assertEqual((d.status, d.target, CalendarEvent.objects.count()), (FoundDeadline.ADDED, self.gc, 0))
        self.assertIn("⏰ Lieferung", [e["title"] for e in FakeRemote.store[self.gc.remote_id].values()])
        self.assertEqual(CalendarSettings.for_user(self.user).deadline_target, self.gc)   # запам'ятано


class RoadmapAndFullViewTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        FakeRemote.store = {}
        self.user = get_user_model().objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        p = mock.patch("calendar_app.sync.client", side_effect=FakeRemote)
        p.start()
        self.addCleanup(p.stop)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)

    def steps(self, provider="google"):
        req = mock.Mock(user=self.user)
        r = next(x for x in sync.roadmap(req) if x["provider"] == provider)
        return {s["key"]: s["state"] for s in r["steps"]}, r

    def test_roadmap_progress(self):
        st, r = self.steps()
        self.assertEqual((st["keys"], st["account"]), ("current", "todo"))
        s = CalendarSyncSettings.get()
        s.google_client_id, s.google_client_secret = "id", "secret"
        s.save()
        self.assertEqual(self.steps()[0]["account"], "current")
        acc = CalendarAccount.objects.create(user=self.user, provider="google", email="g@x.y")
        sync.refresh_calendars(acc)
        st, r = self.steps()
        self.assertEqual((st["account"], st["calendars"], st["show"]), ("done", "done", "current"))
        cal = acc.calendars.get()
        self.client.post("/calendar/connections/sync/", {"action": "layer", "calendar": cal.pk})
        st, r = self.steps()
        self.assertEqual((st["show"], st["compare"], st["rules"]), ("done", "done", "current"))
        self.assertEqual(r["first_shown"], cal)
        page = self.client.get("/calendar/connections/")
        self.assertContains(page, "Підключення по кроках")
        self.assertContains(page, f"?only=r:{cal.pk}")
        self.assertEqual(self.steps("microsoft")[0]["keys"], "current")

    def test_full_view_sources_and_events(self):
        acc = CalendarAccount.objects.create(user=self.user, provider="microsoft", email="o@x.y")
        sync.refresh_calendars(acc)
        cal = acc.calendars.get()
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        FakeRemote(acc).create(cal.remote_id, {"title": "Kunde ACME", "start": start,
                                               "end": start + datetime.timedelta(hours=1), "all_day": False,
                                               "location": "Teams", "description": "Agenda"})
        CalendarEvent.objects.create(user=self.user, title="Minerva Termin", start_at=start)
        r = self.client.get("/calendar/full/?only=r:%d" % cal.pk)
        self.assertContains(r, "fullcalendar@6.1.15")
        self.assertContains(r, '"id": "r:%d"' % cal.pk)
        rng = {"start": (start - datetime.timedelta(days=2)).isoformat(),
               "end": (start + datetime.timedelta(days=5)).isoformat()}
        d = self.client.get("/calendar/full/events/", {**rng, "src": f"r:{cal.pk}"}).json()
        self.assertEqual([e["title"] for e in d["events"]], ["Kunde ACME"])
        self.assertEqual(d["events"][0]["extendedProps"]["location"], "Teams")
        d = self.client.get("/calendar/full/events/", {**rng, "src": "minerva"}).json()
        self.assertEqual([e["title"] for e in d["events"]], ["Minerva Termin"])
        other = get_user_model().objects.create_user("bob", "b@x.y", "x", is_staff=True)
        self.client.force_login(other)
        d = self.client.get("/calendar/full/events/", {**rng, "src": f"r:{cal.pk}"}).json()
        self.assertEqual(d["events"], [])   # чужий календар
        self.assertEqual(self.client.get("/calendar/full/events/", {"start": rng["end"], "end": rng["start"],
                                                                   "src": "minerva"}).status_code, 400)
