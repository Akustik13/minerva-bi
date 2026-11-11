"""🎙 Наради — логіка (meetings/services.py): python manage.py test meetings --settings=tabele.settings_test

Підготовка, квиток і обмін, запити RAG-ПК (статуси, розшифровка частинами, ключові моменти), синхронізація,
✓ / ✕, повтор, видалення, приватність (лише той, хто записав — і суперкористувач ні).
"""
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import Http404
from django.test import TestCase
from django.utils import timezone

from calendar_app.models import CalendarEvent
from crm.models import Customer
from meetings import consent, notify, services
from meetings.models import Meeting, MeetingItem, MeetingSettings, MeetingTombstone, MeetingTranscript
from meetings.services import MeetingError, PushError, TicketError
from projects.models import Project, ProjectHighlight, ProjectLink, ProjectParticipant, ProjectPhase, ProjectStep

UID_ICS = "abc-123@client.example"
ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "METHOD:REQUEST", "BEGIN:VEVENT", f"UID:{UID_ICS}", "SEQUENCE:0",
    "DTSTART:20261112T090000Z", "DTEND:20261112T100000Z", "SUMMARY:Abstimmung Antenne",
    "ORGANIZER;CN=Max Muster:mailto:max@client.example",
    "ATTENDEE;CN=Owner;PARTSTAT=ACCEPTED:mailto:owner@sevskiy.example",
    "ATTENDEE;CN=Eva Kunde;PARTSTAT=ACCEPTED:mailto:eva@client.example",
    "ATTENDEE;CN=Tom Absage;PARTSTAT=DECLINED:mailto:tom@client.example",
    "ATTENDEE;CUTYPE=ROOM;CN=Raum 1;PARTSTAT=ACCEPTED:mailto:room@client.example",
    "END:VEVENT", "END:VCALENDAR", ""])


def staff(username, role=None, **kw):
    user = User.objects.create_user(username, f"{username}@sevskiy.example", "p", is_staff=True, **kw)
    if role:
        user.profile.role = role
        user.profile.save()
    return User.objects.get(pk=user.pk)


SEGMENTS = [
    {"n": 1, "start": 0.5, "end": 4.0, "ch": "L", "text": "Bevor wir anfangen: sind alle einverstanden mit der Aufnahme?"},
    {"n": 2, "start": 4.5, "end": 6.0, "ch": "R", "text": "Ja, passt."},
    {"n": 3, "start": 120.0, "end": 130.0, "ch": "R", "text": "Wir liefern die Antennen bis Freitag nach Köln."},
    {"n": 4, "start": 131.0, "end": 140.0, "ch": "L", "text": "Ich schicke die Zeichnung bis Montag."},
    {"n": 5, "start": 141.0, "end": 150.0, "ch": "R", "text": "Die Lieferung könnte sich wegen des Zolls verzögern."},
]


class MeetingBase(TestCase):
    """Власник — суперкористувач (право є), інший суперкористувач — щоб перевірити, що він НЕ бачить."""

    def setUp(self):
        notify.BACKGROUND = False
        self.addCleanup(setattr, notify, "BACKGROUND", True)
        self.deliver = patch("agent.notify.deliver", return_value="🔔 Повідомлено: пошта")
        self.delivered = self.deliver.start()
        self.addCleanup(self.deliver.stop)
        s = MeetingSettings.load()
        s.enabled = True
        s.save()
        self.owner = User.objects.create_superuser("rec", "owner@sevskiy.example", "p", first_name="Slava",
                                                   last_name="P")
        self.other = User.objects.create_superuser("boss", "boss@sevskiy.example", "p")
        self.customer = Customer.objects.create(name="Max Muster", company="Client GmbH", email="max@client.example")
        self.project = Project.objects.create(name="Antenne 5G", customer=self.customer, owner=self.owner)
        ProjectParticipant.objects.create(project=self.project, email="eva@client.example", name="Eva Kunde")

    # ── помічники ──
    def meeting(self, **data):
        base = {"title": "Abstimmung", "project": self.project.pk, "participants": [
            {"name": "Max Muster", "email": "max@client.example", "source": "manual"}]}
        base.update(data)
        return services.create(self.owner, base)

    def bound(self, m=None, session="s1"):
        """Нарада, для якої вікно запису обміняло квиток (push_token) — ще без запису."""
        m = m or self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        token = services.redeem(ticket)["push_token"]
        return Meeting.objects.get(pk=m.pk), token

    def status(self, m, token, state, seq, session="s1", **data):
        payload = {"session": session, "uid": str(m.uid), "state": state, "seq": seq,
                   "at": timezone.now().isoformat(), **data}
        return services.push_status(m.pk, token, payload)

    def recording(self, m=None, session="s1"):
        m, token = self.bound(m, session)
        consent_ = {"announced": True, "all_agreed": True, "lang": "de", "at": timezone.now().isoformat()}
        self.status(m, token, "recording", 1, session, consent=consent_,
                    capture={"mode": "both", "processing": "standard"}, lang_l="uk", lang_r="de")
        return Meeting.objects.get(pk=m.pk), token

    def transcript(self, m, token, segments=SEGMENTS, upload="u1", parts=1, part=1, session="s1", **extra):
        return services.push_transcript(m.pk, token, {"session": session, "uid": str(m.uid), "upload_id": upload,
                                                      "parts": parts, "part": part, "segments": segments, **extra})

    def processed(self, marks=None):
        """Запис → recorded → розшифровка (без ключових моментів)."""
        m, token = self.recording()
        self.status(m, token, "recorded", 2, duration=150.0, marks=marks or [],
                    levels={"L": {"peak_dbfs": -3.2, "rms_dbfs": -20, "sound_share": 0.4, "sound_s": 60}},
                    silent=[])
        self.transcript(Meeting.objects.get(pk=m.pk), token)
        return Meeting.objects.get(pk=m.pk), token

    def moments(self, m, token, items, session="s1"):
        return services.push_moments(m.pk, token, {"session": session, "uid": str(m.uid), "model": "qwen3.5:9b",
                                                   "summary": "Kurz: Lieferung bis Freitag.", "items": items})


ITEMS = [
    {"kind": "promise", "text": "Клієнт доставить антени до п'ятниці", "who": "Max", "due": "bis Freitag",
     "quote": "liefern die Antennen bis Freitag", "line": 3},
    {"kind": "task", "text": "Надіслати креслення", "due": "14.11.2026", "quote": "schicke die Zeichnung bis Montag",
     "line": 4},
    {"kind": "risk", "text": "Затримка на митниці", "quote": "könnte sich wegen des Zolls verzögern", "line": 5},
    {"kind": "decision", "text": "Вигадане", "quote": "Wir stornieren den ganzen Auftrag sofort", "line": 3},
]


class AccessTests(MeetingBase):
    def test_can_record_needs_enabled_setting_and_right(self):
        self.assertTrue(services.can_record(self.owner))
        MeetingSettings.objects.filter(pk=1).update(enabled=False)
        self.assertFalse(services.can_record(self.owner))
        self.assertEqual(services.record_refusal(self.owner), services.DISABLED_TEXT)
        with self.assertRaisesMessage(MeetingError, "ще не ввімкнено"):
            self.meeting()
        MeetingSettings.objects.filter(pk=1).update(enabled=True)
        self.assertFalse(services.can_record(staff("ro", role="readonly")))
        self.assertFalse(services.can_record(staff("off", is_active=False)))
        self.assertFalse(services.can_record(None))

    def test_only_owner_sees_meeting_even_superuser(self):
        m = self.meeting()
        self.assertEqual(list(services.own(self.owner)), [m])
        self.assertEqual(list(services.own(self.other)), [])
        with self.assertRaises(Http404):
            services.get_own(self.other, m.pk)
        with self.assertRaises(Http404):
            services.get_own(self.owner, "abc")
        self.assertEqual(services.get_own(self.owner, str(m.pk)), m)
        self.assertEqual(str(m), f"🎙 Нарада #{m.pk}")
        self.assertNotIn("Abstimmung", str(m))
        with self.assertRaises(Http404):
            services.edit(self.other, m, {"title": "x"})
        with self.assertRaises(Http404):
            services.issue_ticket(m, self.other)


class PrepareTests(MeetingBase):
    def test_prefill_from_project(self):
        data = services.prefill(self.owner, project=self.project.pk)
        self.assertEqual(data["project"], self.project)
        self.assertEqual(data["customer"], self.customer)
        self.assertTrue(data["title"].startswith(f"Нарада {self.project.code} · "))
        people = {p["email"]: p for p in data["participants"]}
        self.assertIn("eva@client.example", people)
        self.assertFalse(people["eva@client.example"]["checked"])
        self.assertEqual(people["max@client.example"]["source"], "customer")
        self.assertEqual(data["retention"], "text_only")

    def test_prefill_customer_with_single_open_project(self):
        data = services.prefill(self.owner, customer=self.customer.pk)
        self.assertEqual(data["project"], self.project)
        Project.objects.create(name="Second", customer=self.customer)
        self.assertIsNone(services.prefill(self.owner, customer=self.customer.pk)["project"])

    def _invite_event(self, user=None):
        from calendar_app import invites
        from calendar_app.models import MeetingInvite
        from email_assistant.models import EmailAccount, EmailMessage
        user = user or self.owner
        acc = EmailAccount.objects.create(user=user, email_address=user.email, imap_username=user.email,
                                          imap_password="x", smtp_username=user.email, smtp_password="x")
        msg = EmailMessage.objects.create(account=acc, subject="Einladung: Abstimmung Antenne",
                                          from_email="max@client.example", from_name="Max Muster",
                                          to_emails=[user.email], cc_emails=["cc@client.example"], body_text=ICS,
                                          sent_at=datetime(2026, 11, 10, 8, 0, tzinfo=dt_tz.utc))
        inv = MeetingInvite.objects.filter(user=user, uid=UID_ICS).first()
        if inv is None:
            ev = invites.parse_ics(ICS)[0]
            inv = MeetingInvite.objects.create(user=user, uid=UID_ICS, email_message=msg, title=ev["title"],
                                               start_at=ev["start_at"], end_at=ev["end_at"])
        return invites.accept(inv, user).event, msg

    def test_prefill_from_own_invite_attendees(self):
        ev, msg = self._invite_event()
        ProjectLink.objects.update_or_create(project=self.project, message=msg,
                                             defaults={"status": ProjectLink.CONFIRMED})
        data = services.prefill(self.owner, event=ev.pk)
        self.assertEqual(data["calendar_event"], ev)
        self.assertEqual(data["title"], "Abstimmung Antenne")
        self.assertEqual(data["project"], self.project)
        people = {p["email"]: p for p in data["participants"]}
        self.assertTrue(people["max@client.example"]["checked"])          # організатор
        self.assertTrue(people["eva@client.example"]["checked"])          # прийняла
        self.assertFalse(people["tom@client.example"]["checked"])         # відмовився — лише підказка
        self.assertNotIn("room@client.example", people)                   # кімната
        self.assertNotIn("owner@sevskiy.example", people)                 # я сам

    def test_prefill_foreign_event_ignored_and_never_raises(self):
        ev = CalendarEvent.objects.create(user=self.other, title="Private", start_at=timezone.now())
        data = services.prefill(self.owner, event=ev.pk)
        self.assertIsNone(data["calendar_event"])
        self.assertTrue(data["title"])
        with patch("meetings.services._event_projects", side_effect=RuntimeError("boom")):
            own_ev = CalendarEvent.objects.create(user=self.owner, title="Call", start_at=timezone.now())
            self.assertTrue(services.prefill(self.owner, event=own_ev.pk)["title"])

    def test_prefill_mail_fallback_without_ics(self):
        from email_assistant.models import EmailAccount, EmailMessage
        acc = EmailAccount.objects.create(user=self.owner, email_address="owner@sevskiy.example", imap_username="x",
                                          imap_password="x", smtp_username="x", smtp_password="x")
        msg = EmailMessage.objects.create(account=acc, subject="Call", from_email="lead@client.example",
                                          from_name="Lea", to_emails=["owner@sevskiy.example", "eva@client.example"])
        ev = CalendarEvent.objects.create(user=self.owner, title="Call", start_at=timezone.now(), email_message=msg,
                                          event_type=CalendarEvent.TYPE_MEETING)
        people = {p["email"]: p for p in services.prefill(self.owner, event=ev.pk)["participants"]}
        self.assertTrue(people["lead@client.example"]["checked"])
        self.assertFalse(people["eva@client.example"]["checked"])
        self.assertNotIn("owner@sevskiy.example", people)

    def test_create_rechecks_access_and_snapshots_retention(self):
        s = MeetingSettings.load()
        s.transcript_retention_days, s.audio_retention_days = 400, 20
        s.save()
        m = self.meeting(participants=[{"name": "A", "email": "A@x.example", "checked": True},
                                       {"name": "B", "email": "b@x.example", "checked": False},
                                       {"name": "", "email": ""}, {"name": "C", "email": "bad address"}],
                         lang_l="uk", lang_r="xx", retention="audio_text", consent_lang="en")
        self.assertEqual((m.status, m.retention_days, m.audio_days, m.retention), ("ready", 400, 20, "audio_text"))
        self.assertEqual(m.participants, [{"name": "A", "email": "a@x.example", "source": "manual"},
                                          {"name": "C", "email": "", "source": "manual"}])
        self.assertEqual((m.lang_l, m.lang_r, m.consent_lang, m.customer), ("uk", "", "en", self.customer))
        self.assertFalse(m.consent_all_agreed)
        foreign = CalendarEvent.objects.create(user=self.other, title="x", start_at=timezone.now())
        with self.assertRaisesMessage(MeetingError, "Подію календаря не знайдено"):
            self.meeting(calendar_event=foreign.pk)
        with self.assertRaisesMessage(MeetingError, "Проєкт не знайдено"):
            self.meeting(project=999999)
        theirs = services.create(self.other, {"title": "x"})
        with self.assertRaisesMessage(MeetingError, "продовжуєте"):
            self.meeting(continues=theirs.pk)
        cont = self.meeting(continues=m.pk)
        self.assertEqual(cont.continues, m)

    def test_edit_logs_protocol_changes(self):
        m = self.meeting()
        services.edit(self.owner, m, {"participants": [{"name": "Eva", "email": "eva@client.example"}],
                                      "consent_note": "Tom kam später", "title": "Neu"})
        m.refresh_from_db()
        self.assertEqual(m.title, "Neu")
        self.assertEqual([e["field"] for e in m.consent_log], ["participants", "consent_note"])
        self.assertEqual(m.consent_log[0]["user"], self.owner.pk)
        with self.assertRaisesMessage(MeetingError, "Напишіть назву"):
            services.edit(self.owner, m, {"title": "  "})

    def test_continue_prefill(self):
        m = self.meeting()
        data = services.continue_prefill(self.owner, m)
        self.assertEqual(data["title"], "Abstimmung (продовження)")
        self.assertEqual(data["continues"], m)
        self.assertEqual(data["project"], self.project)
        self.assertTrue(any(p["email"] == "max@client.example" and p["checked"] for p in data["participants"]))
        with self.assertRaises(Http404):
            services.continue_prefill(self.other, m)


class TicketTests(MeetingBase):
    def test_issue_and_redeem_once(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        self.assertTrue(services.record_url(ticket).endswith(f"/#minerva={ticket}"))
        m.refresh_from_db()
        self.assertEqual(m.ticket_hash, services._sha(ticket))
        self.assertNotIn(ticket, m.ticket_hash)
        out = services.redeem(ticket)
        info = out["meeting"]
        self.assertEqual((info["id"], info["uid"], info["title"]), (m.pk, str(m.uid), "Abstimmung"))
        self.assertEqual(info["project_code"], self.project.code)
        self.assertEqual(set(info["consent_texts"]), {"de", "en", "uk"})
        self.assertIn("Danke, die Aufnahme läuft jetzt.", info["consent_texts"]["de"]["after"])
        self.assertTrue(info["return_url"].endswith(f"/{m.pk}/page/"))
        self.assertIn("Client GmbH", info["hotwords"])
        m.refresh_from_db()
        self.assertEqual(m.push_hash, services._sha(out["push_token"]))
        self.assertEqual(m.ticket_hash, "")
        with self.assertRaises(TicketError) as e:
            services.redeem(ticket)
        self.assertEqual((e.exception.code, e.exception.status), ("ticket_invalid", 404))
        with self.assertRaises(TicketError):
            services.redeem("x")

    def test_expired_ticket(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner, now=timezone.now() - timedelta(minutes=10))
        with self.assertRaises(TicketError) as e:
            services.redeem(ticket)
        self.assertEqual((e.exception.code, e.exception.status), ("ticket_expired", 410))

    def test_new_ticket_kills_old_push_token(self):
        m, token = self.bound()
        services.issue_ticket(m, self.owner)
        with self.assertRaises(PushError) as e:
            self.status(m, token, "recording", 1, consent={"announced": True, "all_agreed": True})
        self.assertEqual((e.exception.code, e.exception.status), ("push_forbidden", 403))

    def test_no_ticket_while_recording(self):
        m, _ = self.recording()
        with self.assertRaisesMessage(MeetingError, "продовження"):
            services.issue_ticket(m, self.owner)

    def test_rerecord_from_failed_resets_and_retires_old_session(self):
        m, token = self.recording()
        self.status(m, token, "error", 2, stage="transcribe", message="CUDA out of memory")
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_detail), ("failed", "CUDA out of memory"))
        services.issue_ticket(m, self.owner)
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_session, m.push_hash, m.old_sessions), ("ready", "", "", ["s1"]))
        self.assertIsNone(m.consent_at)
        self.assertEqual(m.consent_log[0]["kind"], "recording")           # протокол лишився
        self.assertEqual(services.sync([{"meeting": m.pk, "uid": str(m.uid), "session": "s1"}])[0]["action"],
                         "delete")

    def test_hotwords_whole_items_within_limit(self):
        from inventory.models import Product
        self.project.name = "N" * 150
        self.project.save()
        for i in range(30):
            self.project.products.add(Product.objects.create(sku=f"AN2202{i:02d}-01A", name=f"Ant {i}"))
        words = services.hotwords(Meeting.objects.get(pk=self.meeting().pk))
        self.assertLessEqual(len(words), services.HOTWORDS_MAX)
        parts = words.split(", ")
        self.assertIn(self.project.code, parts)
        self.assertTrue(all(p == "N" * 150 or p in ("Client GmbH", self.project.code) or p.startswith("AN2202")
                            for p in parts))


class PushTests(MeetingBase):
    def test_check_push_identity(self):
        m, token = self.bound()
        with self.assertRaises(PushError) as e:
            services.check_push(999999, token, "s1")
        self.assertEqual((e.exception.code, e.exception.status), ("meeting_unknown", 404))
        with self.assertRaises(PushError) as e:
            services.check_push(m.pk, token, "s1", uid="00000000-0000-0000-0000-000000000000")
        self.assertEqual(e.exception.code, "meeting_unknown")
        with self.assertRaises(PushError) as e:
            services.check_push(m.pk, "wrong", "s1")
        self.assertEqual(e.exception.code, "push_forbidden")
        with self.assertRaises(PushError) as e:
            services.check_push(m.pk, token, "bad session!")
        self.assertEqual((e.exception.code, e.exception.field), ("invalid_request", "session"))
        services.check_push(m.pk, token, "s1", uid=str(m.uid))           # перший запит прив'язує
        self.assertEqual(Meeting.objects.get(pk=m.pk).bench_session, "s1")
        with self.assertRaises(PushError) as e:
            services.check_push(m.pk, token, "s2")
        self.assertEqual((e.exception.code, e.exception.status), ("session_mismatch", 409))
        uid, pk = str(m.uid), m.pk
        Meeting.objects.get(pk=pk).delete()
        with self.assertRaises(PushError) as e:
            services.check_push(pk, token, "s1", uid=uid)
        self.assertEqual((e.exception.code, e.exception.status), ("meeting_gone", 410))
        self.assertTrue(MeetingTombstone.objects.filter(meeting_id=pk, bench_session="s1").exists())

    def test_gone_when_transcript_deleted_or_owner_gone(self):
        m, token = self.bound()
        Meeting.objects.filter(pk=m.pk).update(transcript_deleted_at=timezone.now())
        with self.assertRaises(PushError) as e:
            services.check_push(m.pk, token, "s1")
        self.assertEqual(e.exception.code, "meeting_gone")
        m2, token2 = self.bound()
        Meeting.objects.filter(pk=m2.pk).update(owner=None)
        with self.assertRaises(PushError) as e:
            services.check_push(m2.pk, token2, "s1")
        self.assertEqual(e.exception.code, "meeting_gone")

    def test_recording_requires_consent(self):
        m, token = self.bound()
        with self.assertRaises(PushError) as e:
            self.status(m, token, "recording", 1, consent={"announced": True, "all_agreed": False})
        self.assertEqual((e.exception.code, e.exception.status), ("consent_missing", 409))
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_session, m.bench_seq), ("ready", "", 0))

    def test_recording_status_fills_protocol(self):
        m, _ = self.recording()
        self.assertEqual(m.status, "recording")
        self.assertTrue(m.consent_announced and m.consent_all_agreed)
        self.assertEqual(m.consent_lang, "de")
        self.assertEqual(m.consent_text, consent.text_for("de"))
        self.assertEqual(m.consent_log[0]["kind"], "recording")
        self.assertEqual(m.consent_log[0]["attempt"], 1)
        self.assertEqual((m.capture_mode, m.no_headset, m.lang_l, m.lang_r), ("both", False, "uk", "de"))
        self.assertIsNotNone(m.started_at)
        self.assertEqual(m.bench_state, "recording")

    def test_seq_old_and_unknown_state(self):
        m, token = self.recording()
        out = self.status(m, token, "recorded", 1)
        self.assertFalse(out["applied"])
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "recording")
        out = self.status(m, token, "teleported", 5)
        self.assertFalse(out["applied"])
        self.assertEqual(Meeting.objects.get(pk=m.pk).bench_seq, 5)
        with self.assertRaises(PushError) as e:
            self.status(m, token, "recorded", "x")
        self.assertEqual(e.exception.field, "seq")

    def test_clock_clamped_to_receive_time(self):
        m, token = self.bound()
        far = (timezone.now() + timedelta(days=3)).isoformat()
        services.push_status(m.pk, token, {"session": "s1", "state": "recording", "seq": 1, "at": far,
                                           "started_at": far,
                                           "consent": {"announced": True, "all_agreed": True, "at": far}})
        m.refresh_from_db()
        self.assertLess(m.started_at, timezone.now() + timedelta(minutes=1))
        self.assertLess(m.consent_at, timezone.now() + timedelta(minutes=1))

    def test_recorded_keeps_only_allowed_levels_and_consent_marks(self):
        m, token = self.recording()
        self.status(m, token, "recorded", 2, duration=150.0, silent=["R", "X"],
                    levels={"L": {"peak_dbfs": -3.25, "sound_share": 0.4, "sound_s": 60}, "R": {"peak_dbfs": -80}},
                    marks=[{"t": 3.0, "note": "consent_ok"}, {"t": 50, "note": "Preis!"},
                           {"t": 60, "note": "мікрофон від'єднано", "auto": True}])
        m.refresh_from_db()
        self.assertEqual(m.status, "processing")
        self.assertEqual(m.levels, {"L": {"peak_dbfs": -3.2}, "R": {"peak_dbfs": -80.0}})
        self.assertNotIn("sound_share", str(m.levels))
        self.assertEqual(m.silent, ["R"])
        self.assertEqual([mk["kind"] for mk in m.marks], ["consent_ok", "user", "auto"])
        self.assertEqual(m.marks[0]["label"], "✅ Згоду підтверджено вголос")
        self.assertIn("confirmed", [e["kind"] for e in m.consent_log])
        self.assertEqual(m.duration_s, 150.0)

    def test_statuses_without_consent_are_ignored(self):
        m, token = self.bound()
        out = self.status(m, token, "recorded", 1, duration=10)
        self.assertFalse(out["applied"])
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "ready")
        with self.assertRaises(PushError) as e:
            self.transcript(Meeting.objects.get(pk=m.pk), token)
        self.assertEqual(e.exception.code, "consent_missing")

    def test_done_without_speech_fails_and_notifies(self):
        m, token = self.recording()
        with self.captureOnCommitCallbacks(execute=True):
            self.status(m, token, "done", 2, segments=0, items=0)
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_detail), ("failed", services.EMPTY_TEXT))
        self.assertEqual(self.delivered.call_count, 1)
        self.assertEqual(self.delivered.call_args.args[1]["subject"], "⚠️ Запис наради не вдалося обробити")

    def test_discarded_resets_meeting(self):
        m, token = self.recording()
        self.status(m, token, "discarded", 2)
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_session, m.push_hash, m.old_sessions), ("ready", "", "", ["s1"]))
        self.assertFalse(m.consent_all_agreed)
        self.assertEqual([e["kind"] for e in m.consent_log], ["recording", "discarded"])
        self.assertIn("учасник не погодився", m.bench_detail)
        with self.assertRaises(PushError) as e:
            self.status(m, token, "recorded", 3)
        self.assertEqual(e.exception.code, "push_forbidden")
        m2, token2 = self.recording(m, session="s2")            # нова спроба — номер 2
        self.assertEqual(m2.consent_log[-1]["attempt"], 2)

    def test_audio_deleted_does_not_hide_detail(self):
        m, token = self.processed()
        self.status(m, token, "error", 3, stage="moments", message="Ollama down")
        self.status(m, token, "audio_deleted", 4)
        m.refresh_from_db()
        self.assertIsNotNone(m.audio_deleted_at)
        self.assertEqual(m.bench_detail, services.MOMENTS_FAILED_TEXT)
        self.assertEqual(m.status, "review")


class TranscriptTests(MeetingBase):
    def test_parts_assembled_in_order_and_idempotent(self):
        m, token = self.recording()
        out = self.transcript(m, token, SEGMENTS[2:], parts=2, part=2, model="large-v3")
        self.assertEqual(out, {"complete": False, "missing": 1})
        self.assertIsNone(Meeting.objects.get(pk=m.pk).transcript_at)
        out = self.transcript(m, token, SEGMENTS[:2], parts=2, part=1, languages={"L": "uk", "R": "de"})
        self.assertEqual(out, {"complete": True, "segments": 5})
        m.refresh_from_db()
        tr = MeetingTranscript.objects.get(meeting=m)
        self.assertEqual([s["n"] for s in tr.segments], [1, 2, 3, 4, 5])
        self.assertEqual((tr.model, tr.languages, tr.incoming), ("large-v3", {"L": "uk", "R": "de"}, {}))
        self.assertEqual(m.status, "processing")                 # ключові моменти ще шукаються
        self.assertTrue(m.has_transcript)
        self.assertEqual(m.transcript_chars, sum(len(s["text"]) for s in SEGMENTS))
        self.assertEqual(self.transcript(m, token, SEGMENTS[2:], parts=2, part=2), {"complete": True, "segments": 5})
        with self.assertRaises(PushError) as e:
            self.transcript(m, token, upload="u2")
        self.assertEqual((e.exception.code, e.exception.status), ("transcript_exists", 409))

    def test_new_split_replaces_staging(self):
        m, token = self.recording()
        self.transcript(m, token, SEGMENTS[:2], parts=3, part=1)
        out = self.transcript(m, token, SEGMENTS, upload="u2", parts=1, part=1)
        self.assertTrue(out["complete"])

    def test_invalid_segments(self):
        m, token = self.recording()
        bad = [dict(SEGMENTS[0], end=0.1)]
        with self.assertRaises(PushError) as e:
            self.transcript(m, token, bad)
        self.assertEqual((e.exception.code, e.exception.field), ("invalid_request", "segments[0].end"))
        with self.assertRaises(PushError) as e:
            self.transcript(m, token, [dict(SEGMENTS[0], ch="X")])
        self.assertEqual(e.exception.field, "segments[0].ch")
        with self.assertRaises(PushError) as e:
            self.transcript(m, token, [SEGMENTS[0], SEGMENTS[2]])
        self.assertEqual(e.exception.field, "segments.n")
        out = self.transcript(m, token, [dict(SEGMENTS[0], text="x" * 5000)])
        self.assertTrue(out["complete"])
        self.assertEqual(len(MeetingTranscript.objects.get(meeting=m).segments[0]["text"]),
                         services.SEGMENT_TEXT_MAX)

    def test_consent_excerpt_fallback_and_marks(self):
        m, _ = self.processed()
        self.assertEqual([x["n"] for x in m.consent_excerpt], [1, 2])     # «einverstanden… Aufnahme» + «Ja, passt»
        m2, token2 = self.recording(session="s9")
        self.status(m2, token2, "recorded", 2, marks=[{"t": 135, "note": "consent_join"}], session="s9")
        self.transcript(Meeting.objects.get(pk=m2.pk), token2, session="s9")
        m2.refresh_from_db()
        self.assertEqual([x["n"] for x in m2.consent_excerpt], [3, 4, 5])  # [135 − 45, 135 + 30]
        self.assertTrue(all(len(x["text"]) <= services.EXCERPT_ITEM for x in m2.consent_excerpt))

    def test_user_marks_become_items(self):
        m, _ = self.processed(marks=[{"t": 125, "note": "Preis klären"}, {"t": 10, "note": "пауза", "auto": True},
                                     {"t": 2, "note": "consent_ok"}, {"t": 140.5, "note": ""}])
        items = list(m.items.order_by("start_s"))
        self.assertEqual([(i.kind, i.title, i.segment) for i in items],
                         [("mark", "Preis klären", 3), ("mark", services.MARK_DEFAULT, 5)])
        self.assertEqual(items[0].quote, SEGMENTS[2]["text"])


class MomentTests(MeetingBase):
    def test_moments_before_transcript(self):
        m, token = self.recording()
        with self.assertRaises(PushError) as e:
            self.moments(m, token, ITEMS)
        self.assertEqual((e.exception.code, e.exception.retryable), ("transcript_incomplete", True))

    def test_moments_validated_review_and_single_notification(self):
        m, token = self.processed()
        with self.captureOnCommitCallbacks(execute=True):
            out = self.moments(m, token, ITEMS)
        self.assertEqual(out, {"added": 3, "skipped": 1, "known": 0})
        m.refresh_from_db()
        self.assertEqual(m.status, "review")
        self.assertEqual(MeetingTranscript.objects.get(meeting=m).summary, "Kurz: Lieferung bis Freitag.")
        promise = m.items.get(kind="promise")
        self.assertEqual((promise.segment, promise.ch, promise.start_s, promise.due_text), (3, "R", 120.0,
                                                                                          "bis Freitag"))
        self.assertEqual(self.delivered.call_count, 1)
        head = self.delivered.call_args.args[1]["head"]
        self.assertIn("3 пункти чекають перевірки", head)
        self.assertNotIn("Abstimmung", str(self.delivered.call_args))
        with self.captureOnCommitCallbacks(execute=True):
            self.status(m, token, "done", 3, segments=5, items=3)
        self.assertEqual(self.delivered.call_count, 1)             # лише на першому кінцевому стані

    def test_repush_never_resurrects_decided_items(self):
        m, token = self.processed()
        self.moments(m, token, ITEMS)
        item = m.items.get(kind="risk")
        services.reject(self.owner, item)
        out = self.moments(Meeting.objects.get(pk=m.pk), token, ITEMS)
        self.assertEqual(out, {"added": 0, "skipped": 1, "known": 3})
        self.assertEqual(MeetingItem.objects.get(pk=item.pk).status, "rejected")

    def test_error_after_transcript_is_review_with_note(self):
        m, token = self.processed()
        with self.captureOnCommitCallbacks(execute=True):
            self.status(m, token, "error", 3, stage="moments", message="Модель не встановлена")
        m.refresh_from_db()
        self.assertEqual((m.status, m.moments_error), ("review", services.MOMENTS_FAILED_TEXT))
        self.assertIn("ключові моменти знайти не вдалося", self.delivered.call_args.args[1]["head"])
        self.status(m, token, "recording", 4, consent={"announced": True, "all_agreed": True})
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, "review")   # лише вперед


class SyncTests(MeetingBase):
    def entry(self, m, session="s1", **kw):
        return {"meeting": m.pk, "uid": str(m.uid), "session": session, **kw}

    def test_rules(self):
        m, token = self.recording()
        ready, _ = self.bound()                                  # обміняли квиток, запис ще не прив'язався
        gone = self.meeting()
        gone_pk, gone_uid = gone.pk, str(gone.uid)
        gone.delete()
        out = services.sync([
            self.entry(m), self.entry(m, session="s2"), self.entry(ready, session="new"),
            {"meeting": gone_pk, "uid": gone_uid, "session": "x"}, {"meeting": 999999, "uid": "22222222-2222-2222-2222-222222222222",
                                                                     "session": "y"},
            {"meeting": m.pk, "uid": "11111111-1111-1111-1111-111111111111", "session": "s1"},
            {"meeting": m.pk, "uid": str(m.uid), "session": ""}, "junk"])
        self.assertEqual([o["action"] for o in out], ["keep", "delete", "keep", "delete", "unknown", "unknown",
                                                      "unknown"])
        Meeting.objects.filter(pk=m.pk).update(transcript_deleted_at=timezone.now())
        self.assertEqual(services.sync([self.entry(m)])[0]["action"], "delete")

    def test_bench_info_only_with_block(self):
        services.sync([])
        self.assertIsNone(MeetingSettings.load().bench_seen_at)
        services.sync([], bench={"version": "0.3.0", "live_busy": True, "queue": 2, "secret": "x", "outbox": "3",
                                 "last_error": "e" * 500})
        s = MeetingSettings.load()
        self.assertIsNotNone(s.bench_seen_at)
        self.assertEqual(s.bench_info, {"version": "0.3.0", "last_error": "e" * 200, "queue": 2, "outbox": 3,
                                        "live_busy": True})

    def test_stage_refresh_and_failed_keeps_clock(self):
        m, token = self.recording()
        self.status(m, token, "recorded", 2)
        old = timezone.now() - timedelta(hours=30)
        Meeting.objects.filter(pk=m.pk).update(bench_updated_at=old)
        services.sync([self.entry(m, stage="queued", queue_pos=2)])
        m.refresh_from_db()
        self.assertEqual(m.bench_state, "queued")
        self.assertGreater(m.bench_updated_at, old)
        Meeting.objects.filter(pk=m.pk).update(bench_updated_at=old)
        services.sync([self.entry(m, stage="failed")])
        m.refresh_from_db()
        self.assertEqual((m.bench_state, m.bench_updated_at), ("failed", old))

    def test_retry_once_and_drop_audio(self):
        m, token = self.recording()
        self.status(m, token, "error", 2, stage="transcribe", message="VRAM")
        m.refresh_from_db()
        self.assertTrue(services.can_retry(m))
        services.retry(self.owner, m)
        m.refresh_from_db()
        self.assertEqual((m.status, m.bench_detail), ("processing", services.RETRY_TEXT))
        self.assertEqual(services.sync([self.entry(m)])[0]["action"], "retry")
        # відповідь могла загубитись — повтор ще не взято, тож знову retry (а не keep)
        self.assertEqual(services.sync([self.entry(m, stage="failed")])[0]["action"], "retry")
        self.assertEqual(services.sync([self.entry(m, stage="transcribing")])[0]["action"], "keep")   # узяв
        self.assertEqual(services.sync([self.entry(m)])[0]["action"], "keep")
        with self.assertRaises(Http404):
            services.retry(self.other, m)
        p, ptoken = self.processed()
        self.assertEqual(services.sync([self.entry(p, audio_on_bench=True)])[0]["action"], "drop_audio")
        a, atoken = self.recording(self.meeting(retention="audio_text"), session="a1")
        self.status(a, atoken, "recorded", 2, session="a1")
        self.transcript(Meeting.objects.get(pk=a.pk), atoken, session="a1")
        self.assertEqual(services.sync([self.entry(a, session="a1", audio_on_bench=True)])[0]["action"], "keep")
        Meeting.objects.filter(pk=a.pk).update(approved_at=timezone.now())
        self.assertEqual(services.sync([self.entry(a, session="a1", audio_on_bench=True)])[0]["action"], "drop_audio")


class AcceptTests(MeetingBase):
    def setUp(self):
        super().setUp()
        self.m, token = self.processed()
        self.moments(self.m, token, ITEMS)
        self.m.refresh_from_db()

    def item(self, kind):
        return self.m.items.get(kind=kind)

    def test_moment(self):
        item = services.accept(self.owner, self.item("promise"), "moment", {"title": "Liefertermin Freitag"})
        h = item.highlight
        self.assertEqual((item.status, item.target), ("accepted", "moment"))
        self.assertEqual((h.title, h.quote, h.source, h.meeting, h.meeting_second),
                         ("Liefertermin Freitag", "liefern die Antennen bis Freitag", "meeting", self.m, 120))
        self.assertEqual(h.date, self.m.day)
        self.assertEqual(h.kind, ProjectHighlight.OTHER)
        with self.assertRaisesMessage(MeetingError, "уже вирішено"):
            services.accept(self.owner, item, "moment")

    def test_risk_owner_defaults_to_project_owner(self):
        item = services.accept(self.owner, self.item("risk"), "risk", {"category": "supply", "probability": 3})
        r = item.risk
        self.assertEqual((r.source, r.meeting, r.meeting_second, r.owner, r.category, r.probability),
                         ("meeting", self.m, 141, self.project.owner, "supply", 3))
        self.assertEqual(r.evidence, "könnte sich wegen des Zolls verzögern")
        self.assertIsNotNone(r.review_on)

    def test_step_needs_plan_then_side_and_assignee(self):
        with self.assertRaisesMessage(MeetingError, "ще немає плану"):
            services.accept(self.owner, self.item("promise"), "step")
        ProjectPhase.objects.create(project=self.project, name="Prototyp")
        item = services.accept(self.owner, self.item("promise"), "step", {"date": "14.11.2026"})
        s = item.step
        self.assertEqual((s.side, s.assignee, s.source, s.meeting_second, s.end),
                         (ProjectStep.CUSTOMER, None, "meeting", 120, datetime(2026, 11, 14).date()))
        self.assertIn("«liefern die Antennen bis Freitag»", s.notes)
        mine = services.accept(self.owner, self.item("task"), "step").step
        self.assertEqual((mine.side, mine.assignee), (ProjectStep.WE, self.owner))

    def test_deadline_private_without_quote_and_not_linked(self):
        item = services.accept(self.owner, self.item("task"), "deadline", {"title": "Zeichnung schicken",
                                                                           "date": "2026-11-16"})
        ev = item.event
        self.assertEqual((ev.user, ev.title, ev.description, ev.event_type, ev.all_day, ev.crm_customer),
                         (self.owner, "Zeichnung schicken", "", "deadline", True, self.customer))
        self.assertEqual(timezone.localtime(ev.start_at).hour, 9)
        self.assertFalse(ProjectLink.objects.filter(event=ev).exists())
        with self.assertRaisesMessage(MeetingError, "дату"):
            services.accept(self.owner, self.item("risk"), "deadline", {"title": "x"})

    def test_targets_without_project_or_right(self):
        Meeting.objects.filter(pk=self.m.pk).update(project=None)
        m = Meeting.objects.get(pk=self.m.pk)
        info = services.targets(self.owner, m, m.items.get(kind="task"))
        self.assertIn("без проєкту", info["targets"]["moment"])
        self.assertEqual(info["default"], "deadline")
        with self.assertRaisesMessage(MeetingError, "без проєкту"):
            services.accept(self.owner, m.items.get(kind="task"), "moment")
        with self.assertRaises(Http404):
            services.accept(self.other, m.items.get(kind="task"), "deadline", {"date": "2026-11-16"})
        with self.assertRaisesMessage(MeetingError, "Виберіть"):
            services.accept(self.owner, m.items.get(kind="task"), "elsewhere")

    def test_reject_restore(self):
        item = services.reject(self.owner, self.item("task"))
        self.assertEqual(item.status, "rejected")
        with self.assertRaises(MeetingError):
            services.reject(self.owner, item)
        self.assertEqual(services.restore(self.owner, item).status, "pending")
        with self.assertRaises(Http404):
            services.reject(self.other, item)


class DeleteTests(MeetingBase):
    def test_delete_keeps_project_items_and_scrubs_quotes(self):
        from core.models import AuditLog
        m, token = self.processed()
        self.moments(m, token, ITEMS)
        ProjectPhase.objects.create(project=self.project, name="P")
        h = services.accept(self.owner, m.items.get(kind="promise"), "moment").highlight
        r = services.accept(self.owner, m.items.get(kind="risk"), "risk").risk
        s = services.accept(self.owner, m.items.get(kind="task"), "step").step
        with self.assertRaises(Http404):
            services.delete_meeting(self.other, m)
        pk, uid = m.pk, m.uid
        out = services.delete_meeting(self.owner, m, scrub=True)
        self.assertEqual(out, {"accepted": 3, "scrubbed": 3})
        self.assertFalse(Meeting.objects.filter(pk=pk).exists())
        h.refresh_from_db(), r.refresh_from_db(), s.refresh_from_db()
        self.assertEqual((h.quote, h.meeting), ("", None))
        self.assertTrue(r.evidence.startswith("🎙 Нарада "))
        self.assertNotIn("«", s.notes)
        self.assertTrue(s.notes.startswith("🎙 з наради від "))
        log = AuditLog.objects.get(action="delete", object_repr=f"🎙 Нарада #{pk}")
        self.assertEqual(log.extra["accepted_items"], 3)
        self.assertNotIn("Abstimmung", str(log.extra))
        tomb = MeetingTombstone.objects.get(meeting_id=pk)
        self.assertEqual((tomb.uid, tomb.bench_session, tomb.reason), (uid, "s1", "deleted"))
        self.assertEqual(services.sync([{"meeting": pk, "uid": str(uid), "session": "s1"}])[0]["action"],
                         "delete")


class HelpersTests(MeetingBase):
    def test_parse_day_and_labels(self):
        self.assertEqual(services.parse_day("14.11.2026"), datetime(2026, 11, 14).date())
        self.assertEqual(services.parse_day("bis 3.2.27 bitte"), datetime(2027, 2, 3).date())
        self.assertEqual(services.parse_day("2026-11-14T09:00"), datetime(2026, 11, 14).date())
        self.assertIsNone(services.parse_day("bis Freitag"))
        self.assertIsNone(services.parse_day("31.02.2026"))
        m = self.meeting()
        self.assertEqual(services.speaker_labels(m)["L"], "Я")
        m.capture_mode = "mic"
        self.assertEqual(services.speaker_labels(m)["L"], "У кімнаті (мікрофон)")
        m.capture_mode = "meeting"
        self.assertEqual(services.speaker_labels(m)["R"], "Нарада")

    def test_poll_state_bench_health_checklist(self):
        m, _ = self.processed()
        state = services.poll_state(m)
        self.assertEqual((state["status"], state["transcript"], state["pending"]), ("processing", True, 0))
        self.assertTrue(services.bench_health()["never"])
        services.sync([], bench={"version": "0.3.0", "live_busy": True})
        health = services.bench_health()
        self.assertEqual((health["never"], health["ok"], health["busy"]), (False, True, True))
        keys = {c["key"]: c["ok"] for c in services.rollout_checklist()}
        self.assertEqual(keys, {"texts": False, "privacy": False, "council": False, "key": False, "bench": True})
        from api.models import APIKey
        APIKey.objects.create(name="RAG-ПК — наради", scopes=["meetings:write"])
        self.assertTrue({c["key"]: c["ok"] for c in services.rollout_checklist()}["key"])
        s = MeetingSettings.load()
        services.mark_texts_reviewed(s, self.owner, True)
        self.assertEqual(s.texts_reviewed_by, self.owner)
        self.assertEqual(services.pending_counts([m]), {})


class ProtocolsTests(MeetingBase):
    def test_metadata_only_for_minerva_admin(self):
        m, _ = self.recording()
        rows = services.protocols(self.other)                       # адміністратор Minerva — метадані
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["pk"], row["project_code"], row["all_agreed"], row["transcript"]),
                         (m.pk, self.project.code, True, False))
        self.assertEqual(row["emails"], ["max@client.example"])
        self.assertNotIn("title", row)
        self.assertNotIn("Abstimmung", str(rows))
        self.assertEqual(services.protocols(self.other, q="CLIENT.example")[0]["pk"], m.pk)
        self.assertEqual(services.protocols(self.other, q="nobody@"), [])
        with self.assertRaises(Http404):
            services.protocols(staff("mgr"))

    def test_admin_delete_with_audit(self):
        from core.models import AuditLog
        m, _ = self.recording()
        pk = m.pk
        with self.assertRaises(Http404):
            services.admin_delete(staff("mgr2"), m)
        services.admin_delete(self.other, Meeting.objects.get(pk=pk))
        self.assertFalse(Meeting.objects.filter(pk=pk).exists())
        self.assertEqual(MeetingTombstone.objects.get(meeting_id=pk).reason, "admin")
        log = AuditLog.objects.get(object_repr=f"🎙 Нарада #{pk}")
        self.assertTrue(log.extra["by_admin"])
