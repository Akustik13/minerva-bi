"""Робочий час askDANTE: python manage.py test worktime --settings=tabele.settings_test (askDANTE імітується)."""
import datetime
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog
from worktime import mapping, services
from worktime.client import DanteClient, DanteError, minutes_between
from worktime.models import Booking, DanteProject, DanteSettings, DanteUser, ProjectMap, WorkDay

MON = datetime.date(2026, 9, 7)          # понеділок
D = lambda n: MON + datetime.timedelta(days=n)   # noqa: E731


def at(day, h, m=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(h, m)))


class FakeClient:
    """askDANTE в пам'яті: записи днів, план/відсутності, журнал track()."""

    def __init__(self):
        self.times, self.att, self.calls, self.fail = {}, {}, [], {}
        self.att_error, self.cal, self.records, self.periods = None, None, [], {}

    def working_time(self, a, b, dante_id):
        return {d: [dict(t) for t in v] for d, v in self.times.items() if a <= d <= b}

    def calendar(self, a, b, dante_id):
        if self.cal is None:   # як askDANTE без календаря — запасний шлях attendances
            raise DanteError("not_found", "немає календаря")
        return {d: v for d, v in self.cal.items() if a <= d <= b}

    def attendances(self, a, b, email="", personnel_number=""):
        if self.att_error:
            raise self.att_error
        out, d = {}, a   # askDANTE віддає кожен день періоду
        while d <= b:
            out[d] = self.att.get(d, {"planned": "BUSINESS_DAY", "absences": []})
            d += datetime.timedelta(days=1)
        return out

    def time_records(self, a, b):
        return [r for r in self.records if a <= r["day"] <= b]

    def performance_period(self, name):
        return self.periods.get(name, {})

    def track(self, dante_id, record):
        d = datetime.date.fromisoformat(record["date"])
        if d in self.fail:
            raise self.fail[d]
        self.calls.append(record)
        if "projectData" in record:   # час на проєкт — без приходу / відходу
            return
        self.times.setdefault(d, []).append({"coming": record["start"], "leaving": record["stop"],
                                             "source": "MANUAL",
                                             "minutes": minutes_between(record["start"], record["stop"])})

    def users(self):
        return [{"id": "1108", "first_name": "Anna", "last_name": "Muster", "email": "anna@example.com",
                 "personnel_number": "7", "archived": False}]

    def projects(self):
        return [{"id": "1", "name": "Support", "reference_number": "", "customer_name": "", "archived": False,
                 "time_trackable": True, "parent": None},
                {"id": "2", "name": "Sub", "reference_number": "", "customer_name": "", "archived": False,
                 "time_trackable": True, "parent": "1"}]

    def activities(self):
        return [{"id": "190", "name": "Entwicklung", "archived": False}]


class Base(TestCase):
    def setUp(self):
        self.st = DanteSettings.get()
        self.st.enabled, self.st.api_key = True, "secret-token-123456"
        self.st.save()
        self.user = get_user_model().objects.create_user("anna", "anna@example.com", "x", is_staff=True)
        self.du = DanteUser.objects.create(dante_id="1108", first_name="Anna", email="anna@example.com",
                                           user=self.user)
        self.fake = FakeClient()
        p = mock.patch("worktime.services.client", return_value=self.fake)
        p.start()
        self.addCleanup(p.stop)

    def days(self, a, b):
        return services.refresh_days(self.du, a, b)


# ── клієнт: формат askDANTE ──────────────────────────────────────
class Resp:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data = status, data
        self.text = text if text is not None else ("" if data is None else str(data))

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class ClientTests(TestCase):
    def setUp(self):
        self.st = DanteSettings.get()
        self.st.api_key = "secret-token-123456"
        self.st.save()
        self.c = DanteClient(self.st)

    def test_users_take_email_from_online_access(self):
        data = {"users": [{"id": "1380", "firstName": "Anton", "lastName": "Aal", "archived": False,
                           "onlineAccess": {"email": "anton@aal.com"}}]}
        with mock.patch("requests.request", return_value=Resp(data=data)) as req:
            users = self.c.users()
        self.assertEqual(users[0]["email"], "anton@aal.com")
        self.assertEqual(req.call_args.kwargs["headers"]["apiAuthenticationToken"], "secret-token-123456")
        self.assertTrue(req.call_args.args[1].endswith("/rest/api/users"))

    def test_projects_flatten_subprojects(self):
        data = {"projects": [{"id": "363", "name": "Main", "referenceNumber": "R1", "archived": False,
                              "customer": {"name": "ACME"},
                              "subProjects": {"projects": [{"id": "364", "name": "Child", "archived": True}]}}]}
        with mock.patch("requests.request", return_value=Resp(data=data)):
            projects = self.c.projects()
        self.assertEqual([p["id"] for p in projects], ["363", "364"])
        self.assertEqual(projects[1]["parent"], "363")
        self.assertEqual(projects[0]["customer_name"], "ACME")
        self.assertTrue(projects[1]["archived"])

    def test_working_time_parses_open_and_midnight(self):
        data = {"days": [{"date": "2026-09-07", "dayOfWeek": "MONDAY", "employee": [{"id": "1108", "times": [
            {"coming": {"time": "21:00:00", "source": "MANUAL"}, "leaving": {"time": "00:00:00"}, "duration": 180},
            {"coming": {"time": "08:00:00", "source": "TERMINAL"}, "leaving": None}]}]}]}
        with mock.patch("requests.request", return_value=Resp(data=data)) as req:
            days = self.c.working_time(MON, MON, "1108")
        self.assertIn("/v1/workingTime/20260907-20260907", req.call_args.args[1])
        self.assertEqual(days[MON][0], {"coming": "08:00", "leaving": None, "source": "TERMINAL", "minutes": 0})
        self.assertEqual(days[MON][1]["minutes"], 180)

    def test_working_time_splits_long_period(self):
        with mock.patch("requests.request", return_value=Resp(data={"days": []})) as req:
            self.c.working_time(datetime.date(2026, 1, 1), datetime.date(2026, 6, 30), "1")
        self.assertEqual(req.call_count, 2)

    def test_attendances_xml(self):
        xml = ('<attendancePeriod><personAttendancePeriod><attendanceDays>'
               '<attendanceDay><date>2026-09-12</date><plannedState>NONEBUSINESS_DAY</plannedState><absences>'
               '<absence><absenceType>WEEKEND</absenceType>'
               '<reason xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>'
               '<dayRatio>1</dayRatio></absence></absences></attendanceDay>'
               '<attendanceDay><date>2026-09-07</date><plannedState>BUSINESS_DAY</plannedState><absences/>'
               '</attendanceDay></attendanceDays></personAttendancePeriod></attendancePeriod>')
        with mock.patch("requests.request", return_value=Resp(text=xml)) as req:
            att = self.c.attendances(MON, D(6), email="a@b.c")
        self.assertEqual(req.call_args.kwargs["params"]["login"], "a@b.c")
        self.assertEqual(att[D(5)]["absences"][0]["type"], "WEEKEND")
        self.assertEqual(att[MON], {"planned": "BUSINESS_DAY", "absences": []})

    def test_attendances_json_any_nesting(self):
        data = {"attendancePeriod": {"personAttendancePeriod": [{"attendanceDays": [
            {"date": "2026-09-08", "plannedState": "LEAVE",
             "absences": [{"absenceType": "LEAVE", "dayRatio": 0.5}]}]}]}}
        import json
        with mock.patch("requests.request", return_value=Resp(text=json.dumps(data))):
            att = self.c.attendances(D(1), D(1), personnel_number="7")
        self.assertEqual(att[D(1)]["planned"], "LEAVE")
        self.assertEqual(att[D(1)]["absences"][0]["ratio"], 0.5)

    def test_token_falls_back_to_query_parameter(self):
        with mock.patch("requests.request", side_effect=[Resp(401), Resp(data={"activities": []})]) as req:
            self.c.activities()
        self.assertEqual(req.call_args.kwargs["params"]["apiAuthenticationToken"], "secret-token-123456")
        self.assertTrue(DanteSettings.get().key_in_query)

    def test_field_errors_and_no_token_in_messages(self):
        bad = Resp(400, data={"fieldErrors": [{"message": "Input is required.", "on": "stop"}]})
        with mock.patch("requests.request", return_value=bad):
            with self.assertRaises(DanteError) as e:
                self.c.track("1", {"date": "2026-09-07", "start": "08:00"})
        self.assertIn("stop: Input is required.", e.exception.message)
        with mock.patch("requests.request", side_effect=requests.exceptions.Timeout("…?apiAuthenticationToken=secret")):
            with self.assertRaises(DanteError) as e:
                self.c.users()
        self.assertNotIn("secret", e.exception.message)
        self.assertIn("невідомо", e.exception.message)


# ── рішення й заповнення ─────────────────────────────────────────
class ProposeTests(Base):
    def test_week_plan(self):
        f = self.fake
        f.times[D(1)] = [{"coming": "09:00", "leaving": "17:00", "source": "MANUAL", "minutes": 480}]
        f.att[D(2)] = {"planned": "LEAVE", "absences": [{"type": "LEAVE", "reason": "", "ratio": 1}]}
        f.att[D(3)] = {"planned": "NONEBUSINESS_DAY", "absences": [{"type": "HOLIDAY", "reason": "", "ratio": 1}]}
        f.att[D(4)] = {"planned": "BUSINESS_DAY", "absences": [{"type": "LEAVE", "reason": "", "ratio": 0.5}]}
        f.att[D(5)] = {"planned": "BUSINESS_DAY", "absences": []}   # субота: askDANTE каже робочий, Minerva — ні
        plan = services.propose(self.st, self.du, self.days(MON, D(6)), MON, D(6), DanteSettings.MODE_FIXED)
        got = {p.date: (p.book, p.reason) for p in plan}
        self.assertEqual(got[MON], (True, "стандартний день"))
        self.assertEqual((plan[0].start, plan[0].stop), (datetime.time(8), datetime.time(16, 30)))
        self.assertIn("вже є: 09:00–17:00", got[D(1)][1])
        self.assertEqual(got[D(2)], (False, "відпустка"))
        self.assertEqual(got[D(3)], (False, "свято"))
        self.assertIn("частина дня", got[D(4)][1])
        self.assertEqual(got[D(5)], (False, "вихідний"))
        self.assertEqual(got[D(6)], (False, "вихідний"))

    def test_user_override_times(self):
        self.du.start, self.du.stop = datetime.time(7, 30), datetime.time(15, 45)
        plan = services.propose(self.st, self.du, self.days(MON, MON), MON, MON, DanteSettings.MODE_FIXED)
        self.assertEqual((plan[0].start, plan[0].stop), (datetime.time(7, 30), datetime.time(15, 45)))

    def test_activity_mode_rounds_and_falls_back(self):
        AuditLog.objects.create(user=self.user, action="update", timestamp=at(MON, 8, 47))
        AuditLog.objects.create(user=self.user, action="update", timestamp=at(MON, 17, 32))
        AuditLog.objects.create(user=self.user, action="update", timestamp=at(D(1), 9, 0))   # одна дія
        plan = services.propose(self.st, self.du, self.days(MON, D(2)), MON, D(2), DanteSettings.MODE_ACTIVITY)
        self.assertEqual((plan[0].start, plan[0].stop), (datetime.time(8, 45), datetime.time(17, 35)))
        self.assertIn("2 дій", plan[0].reason)
        self.assertEqual((plan[1].book, plan[1].reason), (False, "замало активності в Minerva"))
        self.assertEqual((plan[2].book, plan[2].reason), (False, "немає активності в Minerva"))
        plan = services.propose(self.st, self.du, self.days(D(2), D(2)), D(2), D(2), DanteSettings.MODE_ACTIVITY,
                                fallback=True)
        self.assertTrue(plan[0].book)
        self.assertEqual(plan[0].start, datetime.time(8))

    def test_long_day_and_missing_holidays_warn(self):
        self.fake.att_error = DanteError("auth", "немає прав")
        plan = services.propose(self.st, self.du, self.days(MON, MON), MON, MON, DanteSettings.MODE_FIXED,
                                start=datetime.time(6), stop=datetime.time(18))
        self.assertTrue(plan[0].book)
        self.assertEqual(plan[0].warnings, ["понад 10 годин", "свята й відпустки askDANTE не прочитано"])


class FillTests(Base):
    def test_fill_writes_only_empty_confirmed_days(self):
        r = services.fill(self.du, MON, D(4), DanteSettings.MODE_FIXED, only_dates={MON, D(1)}, user=self.user)
        self.assertEqual(self.fake.calls, [{"date": "2026-09-07", "start": "08:00", "stop": "16:30"},
                                           {"date": "2026-09-08", "start": "08:00", "stop": "16:30"}])
        self.assertEqual(len(r["sent"]), 2)
        b = Booking.objects.get(date=MON)
        self.assertEqual((b.status, b.source, b.duration, b.created_by), (Booking.SENT, Booking.FILL, 510, self.user))
        self.assertEqual(WorkDay.objects.get(dante_user=self.du, date=MON).minutes, 510)
        # повтор — askDANTE вже має ці дні
        r = services.fill(self.du, MON, D(4), DanteSettings.MODE_FIXED, only_dates={MON, D(1)})
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(len(r["skipped"]), 2)

    def test_day_filled_after_preview_is_skipped(self):
        plan = services.propose(self.st, self.du, self.days(MON, MON), MON, MON, DanteSettings.MODE_FIXED)
        self.assertTrue(plan[0].book)
        self.fake.times[MON] = [{"coming": "08:10", "leaving": None, "source": "TERMINAL", "minutes": 0}]
        r = services.fill(self.du, MON, MON, DanteSettings.MODE_FIXED, only_dates={MON})
        self.assertEqual(self.fake.calls, [])
        self.assertIn("вже є", r["skipped"][0][1])

    def test_failed_day_logged_and_timeout_stops(self):
        self.fake.fail[MON] = DanteError("bad_request", "askDANTE не прийняв запит: stop: bad")
        self.fake.fail[D(1)] = DanteError("network", "askDANTE не відповів вчасно — невідомо, чи запит виконано.")
        r = services.fill(self.du, MON, D(2), DanteSettings.MODE_FIXED, only_dates={MON, D(1), D(2)})
        self.assertEqual(len(r["failed"]), 2)
        self.assertEqual(self.fake.calls, [])   # D(2) не пішов після невідомого результату
        self.assertEqual(Booking.objects.filter(status=Booking.FAILED).count(), 2)

    def test_lock_blocks_parallel_fill(self):
        DanteUser.objects.filter(pk=self.du.pk).update(busy_until=timezone.now() + datetime.timedelta(minutes=5))
        with self.assertRaises(DanteError) as e:
            services.fill(self.du, MON, MON, DanteSettings.MODE_FIXED, only_dates={MON})
        self.assertEqual(e.exception.code, "busy")
        self.assertEqual(self.fake.calls, [])

    def test_lock_released_after_fill(self):
        services.fill(self.du, MON, MON, DanteSettings.MODE_FIXED, only_dates={MON})
        self.assertIsNone(DanteUser.objects.get(pk=self.du.pk).busy_until)

    def test_book_day_rejects_overlap_and_future(self):
        self.fake.times[MON] = [{"coming": "08:00", "leaving": "12:00", "source": "MANUAL", "minutes": 240}]
        with self.assertRaises(DanteError):
            services.book_day(self.du, MON, datetime.time(11), datetime.time(13))
        b = services.book_day(self.du, MON, datetime.time(12, 30), datetime.time(16))
        self.assertEqual(b.status, Booking.SENT)
        with self.assertRaises(DanteError):
            services.book_day(self.du, services.local_today() + datetime.timedelta(days=1),
                              datetime.time(8), datetime.time(9))
        with self.assertRaises(DanteError):
            services.book_day(self.du, D(1), datetime.time(10), datetime.time(9))


class ClockTests(Base):
    def test_clock_in_out_same_day(self):
        with mock.patch("django.utils.timezone.now", return_value=at(MON, 8, 3)):
            services.clock_in(self.du)
        with mock.patch("django.utils.timezone.now", return_value=at(MON, 16, 41)):
            services.clock_out(self.du)
        self.assertEqual(self.fake.calls, [{"date": "2026-09-07", "start": "08:03", "stop": "16:41"}])
        self.assertIsNone(DanteUser.objects.get(pk=self.du.pk).open_since)

    def test_clock_out_after_midnight_two_records_and_retry_safe(self):
        with mock.patch("django.utils.timezone.now", return_value=at(MON, 21, 0)):
            services.clock_in(self.du)
        self.fake.fail[D(1)] = DanteError("unavailable", "askDANTE повернув помилку HTTP 500.")
        with mock.patch("django.utils.timezone.now", return_value=at(D(1), 1, 30)):
            services.clock_out(self.du)
        self.assertEqual(self.fake.calls, [{"date": "2026-09-07", "start": "21:00", "stop": "00:00"}])
        du = DanteUser.objects.get(pk=self.du.pk)
        self.assertEqual(timezone.localtime(du.open_since), at(D(1), 0, 0))   # повтор — лише решта
        del self.fake.fail[D(1)]
        with mock.patch("django.utils.timezone.now", return_value=at(D(1), 1, 31)):
            services.clock_out(du)
        self.assertEqual(self.fake.calls[1], {"date": "2026-09-08", "start": "00:00", "stop": "01:31"})
        self.assertIsNone(DanteUser.objects.get(pk=self.du.pk).open_since)

    def test_forgotten_clock_in_refused(self):
        with mock.patch("django.utils.timezone.now", return_value=at(MON, 8)):
            services.clock_in(self.du)
        with mock.patch("django.utils.timezone.now", return_value=at(D(1), 9)):
            with self.assertRaises(DanteError):
                services.clock_out(self.du)
        self.assertEqual(self.fake.calls, [])

    def test_clock_in_refused_when_open_in_askdante(self):
        with mock.patch("django.utils.timezone.now", return_value=at(MON, 9)):
            self.fake.times[MON] = [{"coming": "08:00", "leaving": None, "source": "TERMINAL", "minutes": 0}]
            with self.assertRaises(DanteError):
                services.clock_in(self.du)


class AutofillTests(Base):
    def test_due_rules(self):
        self.st.autofill_mode, self.st.autofill_hour = DanteSettings.MODE_FIXED, 7
        self.assertFalse(services.autofill_due(self.st, at(MON, 6)))
        self.assertTrue(services.autofill_due(self.st, at(MON, 7)))
        self.st.last_autofill_at = at(MON, 7, 5)
        self.assertFalse(services.autofill_due(self.st, at(MON, 22)))
        self.assertTrue(services.autofill_due(self.st, at(D(1), 7)))
        self.st.autofill_mode = DanteSettings.MODE_OFF
        self.assertFalse(services.autofill_due(self.st, at(D(1), 7)))

    def test_autofill_only_opted_in_since_date_and_skips_warnings(self):
        self.st.autofill_mode, self.st.autofill_lookback_days = DanteSettings.MODE_FIXED, 30
        self.st.autofill_since, self.st.last_sync_at = MON, timezone.now()
        self.st.save()
        other = DanteUser.objects.create(dante_id="2", first_name="Bob")   # без галочки
        self.du.autofill = True
        self.du.save()
        with mock.patch("worktime.services.local_today", return_value=D(9)):   # середа наступного тижня
            result = services.autofill(self.st)
        self.assertEqual([c["date"] for c in self.fake.calls],
                         ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14",
                          "2026-09-15"])
        self.assertIn("записано днів: 7", result)
        self.assertFalse(Booking.objects.filter(dante_user=other).exists())
        self.assertTrue(Booking.objects.filter(source=Booking.AUTO).exists())
        # без свят askDANTE — нічого не пишеться, дні на перевірку
        self.fake.calls.clear()
        self.fake.times.clear()
        self.fake.att_error = DanteError("auth", "немає прав")
        with mock.patch("worktime.services.local_today", return_value=D(9)):
            result = services.autofill(self.st)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("на перевірку в календарі: 7", result)

    def test_dry_run_writes_nothing(self):
        self.st.autofill_mode, self.st.last_sync_at = DanteSettings.MODE_FIXED, timezone.now()
        self.st.autofill_since = MON
        self.du.autofill = True
        self.du.save()
        with mock.patch("worktime.services.local_today", return_value=D(2)):
            result = services.autofill(self.st, dry_run=True)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("записалось би днів: 2", result)
        self.assertIsNone(DanteSettings.get().last_autofill_at)

    def test_report_per_day(self):
        self.st.autofill_mode, self.st.last_sync_at = DanteSettings.MODE_FIXED, timezone.now()
        self.st.autofill_since, self.st.autofill_lookback_days = MON, 30
        self.st.save()
        self.du.autofill = True
        self.du.save()
        self.fake.times[D(1)] = [{"coming": "07:55", "leaving": "16:10", "source": "TERMINAL", "minutes": 495}]
        self.fake.times[D(2)] = [{"coming": "08:00", "leaving": None, "source": "APP", "minutes": 0}]
        self.fake.att[D(3)] = {"planned": "LEAVE", "absences": [{"type": "LEAVE", "ratio": 1}]}
        self.fake.fail[D(4)] = DanteError("bad_request", "Zeitraum gesperrt")
        with mock.patch("worktime.services.local_today", return_value=D(7)):
            report = services.autofill_report(self.st, manual=True)
        self.assertEqual(self.fake.calls, [{"date": "2026-09-07", "start": "08:00", "stop": "16:30"}])
        rows = {r["date"]: r for r in report["users"][0]["days"]}
        self.assertEqual(rows["2026-09-07"]["status"], "sent")
        self.assertEqual(rows["2026-09-08"], {"date": "2026-09-08", "status": "had", "text": "07:55–16:10"})
        self.assertIn("немає відходу", rows["2026-09-09"]["text"])   # прихід без відходу не перезаписується
        self.assertEqual(rows["2026-09-10"]["text"], "відпустка")
        self.assertEqual(rows["2026-09-11"]["status"], "failed")
        self.assertIn("Zeitraum gesperrt", rows["2026-09-11"]["text"])
        self.assertEqual(rows["2026-09-12"]["text"], "вихідний")
        self.assertEqual(report["counts"], {"sent": 1, "would": 0, "had": 2, "review": 0, "skipped": 3, "failed": 1})
        st = DanteSettings.get()
        self.assertTrue(st.last_autofill_report["manual"])
        self.assertIn("вже було в askDANTE: 2", st.last_autofill_result)
        # повторний запуск: усе вже є — нічого не пише
        self.fake.calls.clear()
        del self.fake.fail[D(4)]
        with mock.patch("worktime.services.local_today", return_value=D(7)):
            report = services.autofill_report(self.st, manual=True)
        self.assertEqual([c["date"] for c in self.fake.calls], ["2026-09-11"])
        self.assertEqual(report["counts"]["had"], 3)

    def test_report_explains_empty_run(self):
        self.st.autofill_mode = DanteSettings.MODE_FIXED
        self.st.last_sync_at = timezone.now()
        self.assertIn("немає працівників", services.autofill(self.st))


class SyncTests(Base):
    def test_sync_links_users_by_email_and_projects(self):
        self.du.user = None
        self.du.save()
        r = services.sync_directory(self.st)
        self.assertEqual(r["linked"], 1)
        self.assertEqual(DanteUser.objects.get(dante_id="1108").user, self.user)
        self.assertEqual(DanteProject.objects.get(dante_id="2").parent.dante_id, "1")
        DanteProject.objects.create(dante_id="99", name="Old")
        services.sync_directory(self.st)
        self.assertTrue(DanteProject.objects.get(dante_id="99").gone)


# ── проєкти ──────────────────────────────────────────────────────
class MappingTests(TestCase):
    def setUp(self):
        from crm.models import Customer
        from projects.models import Project
        self.customer = Customer.objects.create(name="Max Muster", company="ACME GmbH", email="max@acme.de")
        self.project = Project.objects.create(name="Antenne 5G", customer=self.customer)

    def dp(self, **kw):
        return DanteProject.objects.create(dante_id=str(DanteProject.objects.count() + 1), **kw)

    def test_suggestions(self):
        other = self.dp(name="Intern")
        by_code = self.dp(name=f"{self.project.code} Antenne")
        self.assertEqual(mapping.suggest(self.project, [other, by_code])[0], by_code)
        same = self.dp(name="antenne 5g")
        self.assertEqual(mapping.suggest(self.project, [other, same])[1], "та сама назва")
        cust = self.dp(name="Support", customer_name="ACME")
        self.assertIn("клієнт", mapping.suggest(self.project, [other, cust])[1])
        similar = self.dp(name="Antenne 5G-Test")
        self.assertIn("схожа назва", mapping.suggest(self.project, [other, similar])[1])
        self.assertIsNone(mapping.suggest(self.project, [other]))

    def test_create_in_dante_links(self):
        fake = mock.Mock()
        fake.create_project.return_value = {"id": "500", "name": "X", "reference_number": self.project.code}
        with mock.patch("worktime.services.client", return_value=fake):
            dp = mapping.create_in_dante(self.project)
        fake.create_project.assert_called_once_with(f"{self.project.code} Antenne 5G", self.project.code)
        self.assertEqual(ProjectMap.objects.get(project=self.project).dante_project, dp)


# ── сторінки ─────────────────────────────────────────────────────
class AdminPageTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.admin = get_user_model().objects.create_superuser("boss", "boss@example.com", "x")
        self.client.force_login(self.admin)
        self.url = reverse("admin:worktime_workday_changelist")

    def test_calendar_month_and_preview(self):
        self.fake.times[MON] = [{"coming": "08:00", "leaving": "16:30", "source": "MANUAL", "minutes": 510}]
        r = self.client.get(self.url, {"u": self.du.pk, "m": "2026-09"})
        self.assertContains(r, "Вересень 2026")
        self.assertContains(r, "08:00–16:30")
        self.assertContains(r, "Порожніх робочих днів")
        r = self.client.get(self.url, {"u": self.du.pk, "m": "2026-09", "fill_from": "2026-09-07",
                                       "fill_to": "2026-09-13", "mode": "fixed", "start": "08:00", "stop": "16:30"})
        self.assertEqual(r.context["plan_book"], 4)
        self.assertContains(r, "Записати в askDANTE")
        self.assertEqual(self.fake.calls, [])   # перегляд нічого не пише

    def test_post_fill_and_book(self):
        r = self.client.post(self.url, {"action": "fill", "u": self.du.pk, "m": "2026-09",
                                        "fill_from": "2026-09-07", "fill_to": "2026-09-13", "mode": "fixed",
                                        "start": "09:00", "stop": "17:00", "dates": ["2026-09-08", "2026-09-12"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.fake.calls, [{"date": "2026-09-08", "start": "09:00", "stop": "17:00"}])
        self.client.post(self.url, {"action": "book", "u": self.du.pk, "date": "2026-09-12",
                                    "start": "10:00", "stop": "12:00"})
        self.assertEqual(self.fake.calls[-1], {"date": "2026-09-12", "start": "10:00", "stop": "12:00"})

    def test_employee_sees_only_own_calendar(self):
        other = DanteUser.objects.create(dante_id="2", first_name="Bob")
        self.client.force_login(self.user)
        r = self.client.get(self.url, {"u": other.pk})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["du"])
        self.client.post(self.url, {"action": "book", "u": other.pk, "date": "2026-09-08",
                                    "start": "10:00", "stop": "12:00"})
        self.assertEqual(self.fake.calls, [])
        r = self.client.get(self.url)
        self.assertEqual(r.context["du"], self.du)

    def test_mapping_page(self):
        from projects.models import Project
        pr = Project.objects.create(name="Antenne")
        dp = DanteProject.objects.create(dante_id="7", name="Antenne")
        url = reverse("admin:worktime_danteproject_changelist")
        r = self.client.get(url + "?view=minerva")
        self.assertContains(r, "та сама назва")
        self.client.post(url + "?view=minerva&show=open", {f"map_{pr.pk}": dp.pk})
        self.assertEqual(ProjectMap.objects.get(project=pr).dante_project, dp)
        self.client.post(url, {f"map_{pr.pk}": ""})
        self.assertFalse(ProjectMap.objects.exists())

    def test_fill_now_and_report(self):
        self.st.autofill_mode, self.st.last_sync_at = DanteSettings.MODE_FIXED, timezone.now()
        self.st.autofill_since = MON
        self.st.save()
        self.du.autofill = True
        self.du.save()
        self.fake.times[D(1)] = [{"coming": "07:55", "leaving": "16:10", "source": "TERMINAL", "minutes": 495}]
        url = reverse("admin:worktime_dantesettings_fillnow")
        with mock.patch("worktime.services.local_today", return_value=D(3)):
            self.assertEqual(self.client.get(url).status_code, 302)   # лише POST
            self.assertEqual(self.fake.calls, [])
            r = self.client.post(reverse("admin:worktime_dantesettings_dryrun"))
            self.assertContains(r, "нічого не записано")
            self.assertContains(r, "запишеться")
            self.assertEqual(self.fake.calls, [])
            self.assertEqual(DanteSettings.get().last_autofill_report, {})
            r = self.client.post(url)
        self.assertRedirects(r, reverse("admin:worktime_dantesettings_report"))
        self.assertEqual([c["date"] for c in self.fake.calls], ["2026-09-07", "2026-09-09"])
        r = self.client.get(reverse("admin:worktime_dantesettings_report"))
        self.assertContains(r, "Заповнено вручну")
        self.assertContains(r, "07:55–16:10")
        self.assertContains(r, "вже було")
        r = self.client.get(reverse("admin:worktime_dantesettings_changelist"), follow=True)
        self.assertContains(r, "Звіт по днях")
        self.assertContains(r, "Заповнити зараз")

    def test_fill_now_needs_mode(self):
        self.du.autofill = True
        self.du.save()
        r = self.client.post(reverse("admin:worktime_dantesettings_fillnow"), follow=True)
        self.assertContains(r, "Автозаповнення вимкнено")
        self.assertEqual(self.fake.calls, [])

    def test_settings_pages(self):
        r = self.client.get(reverse("admin:worktime_dantesettings_changelist"), follow=True)
        self.assertContains(r, "збережено: ")
        self.assertNotContains(r, "secret-token-123456")
        self.client.post(reverse("admin:worktime_dantesettings_sync"))
        self.assertTrue(DanteProject.objects.filter(dante_id="2").exists())


# ── години, відсутності, баланс, ☆ і час на проєкти ───────────────
class ReadFormatsTests(ClientTests):
    def test_calendar_absences_with_state_and_half_day(self):
        data = {"id": 1108, "timeAccountAttendanceDays": [
            {"date": "2026-09-08", "targetTime": 28800000, "workingTime": 0, "absenceRatio": 1,
             "absences": [{"ratio": 1, "approvalState": "SUBMITTED", "absenceReason": "DEFAULT_LEAVE"}]},
            {"date": "2026-09-09", "targetTime": 28800000, "workingTime": 14400000,
             "absences": [{"ratio": 0.5, "approvalState": "APPROVED", "absenceReason": "DISEASE"}]},
            {"date": "2026-09-12", "targetTime": 0, "absences": [{"ratio": 1, "absenceReason": "WEEKEND"}]},
            {"date": "2026-09-14", "targetTime": 28800000, "absences": [
                {"ratio": 1, "approvalState": "APPROVED", "absenceReason": "CUSTOM", "absenceName": "AWH"}]}]}
        with mock.patch("requests.request", return_value=Resp(data=data)) as req:
            cal = self.c.calendar(D(1), D(7), "1108")
        self.assertIn("/calendar/1108", req.call_args.args[1])
        self.assertEqual(req.call_args.kwargs["params"]["start"], "2026-09-08")
        self.assertEqual(cal[D(1)]["planned"], "LEAVE")
        self.assertEqual(cal[D(1)]["target"], 480)
        self.assertEqual(cal[D(2)]["planned"], "BUSINESS_DAY")   # пів дня лікарняного — робочий день
        self.assertEqual(cal[D(5)]["planned"], "NONEBUSINESS_DAY")
        self.assertEqual(services.absence_label(cal[D(1)]["absences"][0]), "відпустка (подано)")
        self.assertEqual(services.absence_label(cal[D(2)]["absences"][0]), "лікарняний ½")
        self.assertEqual(services.absence_label(cal[D(7)]["absences"][0]), "AWH")

    def test_time_records_xml(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?><timerecords><timerecord id="270"><personId>1108</personId>'
               '<firstName>Anna</firstName><start>2026-09-07T09:00:00.000</start><stop>2026-09-07T10:30:00.000</stop>'
               '<duration>90</duration><day>2026-09-07</day><description>Abstimmung</description>'
               '<hourlyRate>80.00</hourlyRate><activity id="378">meeting</activity>'
               '<project id="363" finalElement="false" creator="x">Main</project>'
               '<project_item id="364">Child</project_item></timerecord>'
               '<timerecord id="271"><personId>1108</personId><duration>30</duration><day>2026-09-08</day>'
               '</timerecord></timerecords>')
        with mock.patch("requests.request", return_value=Resp(text=xml)) as req:
            recs = self.c.time_records(MON, D(1))
        self.assertEqual(req.call_args.args[0], "POST")
        self.assertEqual(req.call_args.kwargs["params"], {"start": "07.09.2026", "stop": "08.09.2026"})
        self.assertEqual(req.call_args.kwargs["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        r = recs[0]
        self.assertEqual((r["id"], r["duration"], r["start"], r["stop"], r["item_id"], r["project_id"],
                          r["activity_name"], str(r["hourly_rate"])),
                         ("270", 90, "09:00", "10:30", "364", "363", "meeting", "80.00"))
        self.assertEqual(recs[1]["project_id"], "")

    def test_performance_period_xml_and_missing(self):
        xml = ('<performancePeriodUsers period-name="09-2026" period-start="2026-09-01" period-stop="2026-09-30" '
               'period-status="OPEN"><performancePeriodDetail personId="1108"><targetTime>10080</targetTime>'
               '<workingTime>10200</workingTime><overTime>120</overTime><overTimeTotal>-90</overTimeTotal>'
               '<remainingLeave>4.50</remainingLeave><diseaseDays>1</diseaseDays><email>a@b.c</email>'
               '<customAbsences><customAbsence><name>AWH</name></customAbsence></customAbsences>'
               '</performancePeriodDetail></performancePeriodUsers>')
        with mock.patch("requests.request", return_value=Resp(text=xml)) as req:
            rows = self.c.performance_period("09-2026")
        self.assertEqual(req.call_args.kwargs["data"], {"name": "09-2026"})
        self.assertEqual(rows["1108"]["remainingLeave"], 4.5)
        self.assertEqual(rows["1108"]["email"], "a@b.c")
        with mock.patch("requests.request", return_value=Resp(text="<error>No period</error>")):
            with self.assertRaises(DanteError):
                self.c.performance_period("13-2026")


class PlanTests(Base):
    def test_calendar_first_then_attendances_fallback(self):
        self.fake.cal = {D(1): {"planned": "LEAVE", "target": 480, "worked": 0,
                                "absences": [{"type": "DEFAULT_LEAVE", "name": "", "ratio": 1, "state": "SUBMITTED"}]}}
        days = self.days(D(1), D(1))
        self.assertEqual(days[D(1)].target_minutes, 480)
        self.assertEqual(services.day_blocker(self.st, days[D(1)], D(1)), "відпустка (подано)")
        self.fake.cal = None   # немає календаря → attendances
        self.assertEqual(self.days(D(1), D(1))[D(1)].planned_state, "BUSINESS_DAY")
        self.fake.cal = {}     # порожньо → теж attendances
        self.assertFalse(self.days(D(1), D(1)).attendance_error)
        self.fake.cal, self.fake.att_error = None, DanteError("auth", "немає прав")
        self.assertIn("немає прав", self.days(D(1), D(1)).attendance_error)


class RecordsTests(Base):
    def setUp(self):
        super().setUp()
        self.main = DanteProject.objects.create(dante_id="363", name="Antenne")
        self.child = DanteProject.objects.create(dante_id="364", name="Prototyp", parent=self.main)

    def rec(self, rid, day, minutes, project="363", item="", rate=None):
        return {"id": rid, "person_id": "1108", "day": day, "duration": minutes, "start": "", "stop": "",
                "description": "Abstimmung", "project_id": project, "project_name": "Antenne", "item_id": item,
                "item_name": "Prototyp" if item else "", "activity_id": "", "activity_name": "meeting",
                "hourly_rate": rate, "cost_center": ""}

    def test_sync_hours_and_removed_records(self):
        from decimal import Decimal
        from crm.models import Customer
        from projects.models import Project
        from worktime import panels
        from worktime.models import TimeRecord
        self.fake.records = [self.rec("1", MON, 90, rate=Decimal("80")), self.rec("2", D(1), 30, item="364"),
                             self.rec("3", D(2), 60, project="")]
        r = services.sync_records(self.st, MON, D(6))
        self.assertEqual((r["records"], r["removed"]), (3, 0))
        self.assertEqual(TimeRecord.objects.get(dante_id="2").dante_project, self.child)
        self.assertEqual(TimeRecord.objects.get(dante_id="1").dante_user, self.du)
        h = services.hours([self.main])   # з підпроєктом
        self.assertEqual(h["total"], 120)
        self.assertEqual(h["cost"], Decimal("120.00"))
        customer = Customer.objects.create(name="Max", company="ACME GmbH")
        pr = Project.objects.create(name="Antennen-Entwicklung", customer=customer)
        mapping.link(pr, self.main)
        self.assertEqual(services.project_hours(pr)["total"], 120)
        self.assertEqual(services.customer_hours(customer)["total"], 120)
        self.child.crm_customer = customer
        self.child.save()
        self.assertEqual(services.customer_hours(customer)["total"], 120)   # без подвійного рахунку
        self.fake.records = [self.rec("1", MON, 90)]   # 2 і 3 видалили в askDANTE
        self.assertEqual(services.sync_records(self.st, MON, D(6))["removed"], 2)
        self.assertIn("1:30", panels.project_panel(pr))

    def test_balance_cached_for_all_users(self):
        from worktime.models import PeriodBalance
        other = DanteUser.objects.create(dante_id="2", first_name="Bob")
        self.fake.periods = {"09-2026": {"1108": {"targetTime": 10080.0, "workingTime": 10200.0,
                                                  "overTimeTotal": -90.0, "remainingLeave": 4.5},
                                         "2": {"targetTime": 5040.0}}}
        pb = services.balance(self.du, 2026, 9)
        lines = dict(services.balance_lines(pb))
        self.assertEqual(lines["Баланс переробок"][-4:], "1:30")
        self.assertTrue(lines["Баланс переробок"].startswith("−"))
        self.assertEqual(lines["Відпустка: залишок, днів"], "4.5")
        self.assertTrue(PeriodBalance.objects.filter(dante_user=other, period="2026-09").exists())
        self.fake.periods = {}
        self.assertEqual(services.balance(self.du, 2026, 9).data["remainingLeave"], 4.5)   # кеш на годину
        self.assertIn("не повернув", services.balance(self.du, 2026, 9, max_age_minutes=0).error)

    def test_book_project_payload_and_guards(self):
        from worktime.models import DanteActivity
        act = DanteActivity.objects.create(dante_id="190", name="Entwicklung")
        self.st.default_activity = act
        self.st.save()
        b = services.book_project(self.du, MON, self.child, 90, comment="Prototyp prüfen", user=self.user, st=self.st)
        self.assertEqual(b.status, Booking.SENT)
        self.assertEqual(self.fake.calls, [{"date": "2026-09-07", "duration": 90, "projectData": {
            "projectId": "364", "activityId": "190", "comment": "Prototyp prüfen"}}])
        with self.assertRaises(DanteError):   # подвійне натискання
            services.book_project(self.du, MON, self.child, 90, comment="Prototyp prüfen", st=self.st)
        with self.assertRaises(DanteError):
            services.book_project(self.du, services.local_today() + datetime.timedelta(days=1), self.child, 30)
        self.child.archived = True
        self.child.save()
        with self.assertRaises(DanteError):
            services.book_project(self.du, MON, self.child, 30)
        self.assertEqual(len(self.fake.calls), 1)

    def test_pins_and_parse_minutes(self):
        from worktime.admin import parse_minutes
        self.assertTrue(services.toggle_pin(self.user, self.main))
        self.assertEqual(list(services.my_projects(self.user)), [self.main])
        self.assertFalse(services.toggle_pin(self.user, self.main))
        self.assertEqual([parse_minutes(x) for x in ("1:30", "90", "1,5", "2h", "45m", "", "abc")],
                         [90, 90, 90, 120, 45, 0, 0])

    def test_customer_suggest_find_and_create_in_minerva(self):
        from crm.models import Customer
        acme = Customer.objects.create(name="Max Muster", company="ACME GmbH", email="max@acme.example")
        Customer.objects.create(name="Other", company="Beta AG")
        self.main.customer_name = "ACME"
        self.assertEqual(mapping.suggest_customer(self.main), acme)
        self.assertEqual(mapping.find_customer("max@acme.example"), acme)
        self.assertEqual(mapping.find_customer(f"#{acme.pk}"), acme)
        self.assertIsNone(mapping.find_customer("e"))   # кілька збігів
        self.main.crm_customer = acme
        pr = mapping.create_in_minerva(self.main, self.user)
        self.assertEqual((pr.name, pr.customer, pr.dante_map.dante_project), ("Antenne", acme, self.main))


class NewPagesTests(AdminPageTests):
    def test_dante_projects_view_pin_crm_and_minerva(self):
        from crm.models import Customer
        acme = Customer.objects.create(name="Max", company="ACME")
        dp = DanteProject.objects.create(dante_id="363", name="Antenne", customer_name="ACME GmbH")
        url = reverse("admin:worktime_danteproject_changelist")
        r = self.client.get(url)
        self.assertContains(r, "Antenne")
        self.assertContains(r, 'name="crm_suggest_%d"' % dp.pk)
        self.client.post(url + "?view=dante&f=all", {"pin": dp.pk})
        self.assertTrue(dp.pinned_by.filter(pk=self.admin.pk).exists())
        self.assertContains(self.client.get(url), "★")   # «мої» за замовчуванням
        self.client.post(url, {"crm_accept": dp.pk, f"crm_suggest_{dp.pk}": acme.pk})
        dp.refresh_from_db()
        self.assertEqual(dp.crm_customer, acme)
        self.client.post(url, {"to_minerva": dp.pk})
        self.assertEqual(dp.maps.get().project.customer, acme)

    def test_calendar_project_entry_balance_and_minerva_calendar(self):
        dp = DanteProject.objects.create(dante_id="363", name="Antenne")
        dp.pinned_by.add(self.admin)
        self.du.user = self.admin
        self.du.save()
        self.fake.periods = {"09-2026": {"1108": {"targetTime": 10080.0, "remainingLeave": 4.5}}}
        r = self.client.get(self.url, {"m": "2026-09"})
        self.assertContains(r, "Місяць в askDANTE")
        self.assertContains(r, "Час на проєкт")
        r = self.client.post(self.url, {"action": "project", "u": self.du.pk, "m": "2026-09", "date": "2026-09-08",
                                        "project": dp.pk, "duration": "1:15", "comment": "Test",
                                        "next": "/calendar/?year=2026&month=9"})
        self.assertRedirects(r, "/calendar/?year=2026&month=9", fetch_redirect_response=False)
        self.assertEqual(self.fake.calls[-1]["duration"], 75)
        r = self.client.get("/calendar/?year=2026&month=9")
        self.assertContains(r, "Робочий час ·")
        self.assertContains(r, "wt-days")
        r = self.client.post(self.url, {"action": "project", "u": self.du.pk, "date": "2026-09-08", "project": dp.pk,
                                        "duration": "1:00", "next": "https://evil.example/"})
        self.assertNotIn("evil", r["Location"])

    def test_project_and_customer_cards_show_hours(self):
        from crm.models import Customer
        from projects.models import Project
        customer = Customer.objects.create(name="Max", company="ACME")
        pr = Project.objects.create(name="Antenne", customer=customer)
        r = self.client.get(reverse("admin:projects_project_change", args=[pr.pk]))
        self.assertContains(r, "Час askDANTE")
        self.assertContains(r, "ще не зв")
        r = self.client.get(reverse("admin:crm_customer_change", args=[customer.pk]))
        self.assertContains(r, "Час askDANTE")
