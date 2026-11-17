"""Запрошення на зустрічі з пошти: python manage.py test calendar_app --settings=tabele.settings_test"""
import datetime
import email
import os
import shutil
import tempfile
from datetime import timezone as dt_tz
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from calendar_app import invites
from calendar_app.models import CalendarEvent, CalendarSettings, MeetingInvite
from email_assistant.models import EmailAccount, EmailSettings
from email_assistant.sync import save_message

ME = "me@sevskiy.example"
BERLIN = ZoneInfo("Europe/Berlin")


def ics(uid="m-1@outlook", seq=0, method="REQUEST", start="20261103T100000", end="20261103T110000",
        tzid="W. Europe Standard Time", summary="Projekt Antenne – Abstimmung", extra=""):
    tz = f';TZID="{tzid}"' if tzid else ""
    return ("BEGIN:VCALENDAR\r\nMETHOD:" + method + "\r\nPRODID:Microsoft Exchange Server 2010\r\n"
            "BEGIN:VTIMEZONE\r\nTZID:W. Europe Standard Time\r\nBEGIN:STANDARD\r\nDTSTART:16010101T030000\r\n"
            "END:STANDARD\r\nEND:VTIMEZONE\r\n"
            f"BEGIN:VEVENT\r\nUID:{uid}\r\nSEQUENCE:{seq}\r\nSUMMARY;LANGUAGE=de-DE:{summary}\r\n"
            f"DTSTART{tz}:{start}\r\nDTEND{tz}:{end}\r\n"
            'ORGANIZER;CN="Max Muster":mailto:max@acme.example\r\n'
            "LOCATION:Microsoft Teams-Besprechung\r\n"
            "DESCRIPTION:Hallo\\, wie besprochen.\\n\\nJetzt teilnehmen: https://teams.microsoft.com/l/meetup-join/\r\n"
            " 19%3ameeting_abc%40thread.v2/0?context=x\r\n"
            f"{extra}BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:REMINDER\r\nEND:VALARM\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n")


class ParseTests(TestCase):
    def test_outlook_windows_timezone_teams_link(self):
        [e] = invites.parse_ics(ics())
        self.assertEqual(e["start_at"], datetime.datetime(2026, 11, 3, 10, 0, tzinfo=BERLIN))
        self.assertEqual(e["end_at"] - e["start_at"], datetime.timedelta(hours=1))
        self.assertEqual((e["title"], e["organizer"], e["method"]), ("Projekt Antenne – Abstimmung", "Max Muster",
                                                                     "REQUEST"))
        self.assertTrue(e["join_url"].startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"))
        self.assertIn("Hallo, wie besprochen.", e["description"])
        self.assertNotIn("REMINDER", e["description"])   # опис VALARM не перебиває опис зустрічі

    def test_utc_all_day_and_duration(self):
        [e] = invites.parse_ics(ics(start="20261103T090000Z", end="", tzid="", extra="DURATION:PT30M\r\n")
                                .replace("DTEND:\r\n", ""))
        self.assertEqual(e["start_at"], datetime.datetime(2026, 11, 3, 9, 0, tzinfo=dt_tz.utc))
        self.assertEqual(e["end_at"] - e["start_at"], datetime.timedelta(minutes=30))
        [d] = invites.parse_ics(ics(start="20261105", end="20261106", tzid="").replace("DTSTART:", "DTSTART;VALUE=DATE:"))
        self.assertTrue(d["all_day"])

    def test_broken_or_empty(self):
        self.assertEqual(invites.parse_ics("hello"), [])
        self.assertEqual(invites.parse_ics(ics().replace("UID:m-1@outlook\r\n", "")), [])


class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("anna", ME, "x", is_staff=True, is_superuser=True)
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        self.uid = 0

    def mail(self, body, subject="Einladung: Projekt Antenne", attachments=None):
        self.uid += 1
        return save_message(self.account, {
            "uid": self.uid, "message_id": f"<i{self.uid}@acme.example>", "in_reply_to": "", "references": [],
            "subject": subject, "from_email": "max@acme.example", "from_name": "Max Muster", "to_emails": [ME],
            "cc_emails": [], "body_text": body, "body_html": "", "attachments": attachments or [], "is_read": False,
            "sent_at": datetime.datetime(2026, 10, 20, 9, 0, tzinfo=dt_tz.utc)}, "INBOX")


class FlowTests(Base):
    def test_new_mail_suggests_then_accept_update_cancel(self):
        self.mail(ics())   # сигнал нового листа
        inv = MeetingInvite.objects.get()
        self.assertEqual(inv.status, MeetingInvite.NEW)
        self.assertFalse(CalendarEvent.objects.exists())   # без ✓ — нічого в календарі
        invites.accept(inv)
        ev = CalendarEvent.objects.get()
        self.assertEqual((ev.event_type, ev.title, ev.start_at), (CalendarEvent.TYPE_MEETING, inv.title, inv.start_at))
        self.assertIn("🔗 https://teams.microsoft.com", ev.description)
        # той самий лист ще раз — нічого
        self.assertEqual(invites.detect(inv.email_message), [])
        # перенесено
        self.mail(ics(seq=1, start="20261104T140000", end="20261104T150000"), subject="Aktualisiert: Projekt")
        inv.refresh_from_db()
        self.assertEqual(inv.status, MeetingInvite.UPDATE)
        invites.accept(inv)
        ev.refresh_from_db()
        self.assertEqual(ev.start_at, datetime.datetime(2026, 11, 4, 14, 0, tzinfo=BERLIN))
        # старіша версія не перебиває
        self.mail(ics(seq=0))
        inv.refresh_from_db()
        self.assertEqual(inv.status, MeetingInvite.ADDED)
        # скасовано
        self.mail(ics(seq=2, method="CANCEL"), subject="Abgesagt: Projekt")
        inv.refresh_from_db()
        self.assertEqual(inv.status, MeetingInvite.CANCEL)
        invites.accept(inv)
        ev.refresh_from_db()
        self.assertTrue(ev.is_done)
        self.assertTrue(ev.title.startswith("✖ Скасовано"))

    def test_dismissed_stays_dismissed_and_reply_ignored(self):
        self.mail(ics())
        inv = MeetingInvite.objects.get()
        invites.dismiss(inv)
        self.mail(ics(seq=3, start="20261110T100000", end="20261110T110000"))
        inv.refresh_from_db()
        self.assertEqual(inv.status, MeetingInvite.DISMISSED)
        self.mail(ics(uid="other@x", method="REPLY"), subject="Angenommen: Projekt")
        self.assertEqual(MeetingInvite.objects.count(), 1)
        self.assertEqual(invites.pending(self.user), [])

    def test_auto_mode_and_cancel_of_unknown(self):
        cfg = CalendarSettings.for_user(self.user)
        cfg.invites_auto = True
        cfg.save()
        self.mail(ics(uid="nope@x", method="CANCEL"))
        self.assertFalse(MeetingInvite.objects.exists())
        self.mail(ics())
        self.assertEqual(MeetingInvite.objects.get().status, MeetingInvite.ADDED)
        self.assertEqual(CalendarEvent.objects.count(), 1)

    def test_scan_history_with_ics_attachment_file(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            os.makedirs(os.path.join(media, "email_attachments"), exist_ok=True)
            with open(os.path.join(media, "email_attachments", "invite.ics"), "w", encoding="utf-8") as fh:
                fh.write(ics(uid="google-1"))
            att = [{"name": "invite.ics", "content_type": "application/ics", "size": 10,
                    "file_path": "email_attachments/invite.ics"}]
            m = self.mail("Einladung von Google Kalender", attachments=att)
            # сигнал уже знайшов запрошення з файлу
            self.assertEqual(MeetingInvite.objects.get().email_message, m)
            MeetingInvite.objects.all().delete()
            seen, found = invites.scan(self.user, datetime.date(2026, 10, 1), imap=False)
        self.assertEqual((seen, found), (1, 1))
        self.assertTrue(invites.looks_like_invite(m))

    def test_imap_parser_keeps_inline_calendar_part(self):
        from email_assistant.imap_client import _get_attachments_raw
        raw = ("MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n\r\nHallo\r\n--b\r\n"
               "Content-Type: text/calendar; charset=utf-8; method=REQUEST\r\n\r\n" + ics() + "\r\n--b--\r\n")
        atts = _get_attachments_raw(email.message_from_string(raw))
        self.assertEqual([(a["name"], a["content_type"]) for a in atts], [("invite.ics", "text/calendar")])
        self.assertIn(b"BEGIN:VEVENT", atts[0]["_data"])


class ViewTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def test_calendar_shows_pending_and_decide(self):
        self.mail(ics())
        inv = MeetingInvite.objects.get()
        r = self.client.get("/calendar/?year=2026&month=11")
        self.assertContains(r, "Запрошення на зустрічі з пошти")
        self.assertContains(r, "Projekt Antenne")
        other = get_user_model().objects.create_user("bob", "b@x.y", "p", is_staff=True, is_superuser=True)
        self.client.force_login(other)
        self.assertEqual(self.client.post(f"/calendar/invites/{inv.pk}/accept/").status_code, 404)
        self.client.force_login(self.user)
        d = self.client.post(f"/calendar/invites/{inv.pk}/accept/").json()
        self.assertTrue(d["ok"])
        self.assertTrue(CalendarEvent.objects.filter(pk=d["event_id"], event_type="meeting").exists())
        r = self.client.get("/calendar/?year=2026&month=11")
        self.assertNotContains(r, "Запрошення на зустрічі з пошти")

    def test_settings_scan_and_auto(self):
        r = self.client.post("/calendar/settings/", {"invites_auto": "1", "invites_since": "2026-10-01",
                                                     "default_remind_minutes": "30"})
        self.assertEqual(r.status_code, 200)
        cfg = CalendarSettings.for_user(self.user)
        self.assertTrue(cfg.invites_auto)
        self.assertEqual(cfg.invites_since, datetime.date(2026, 10, 1))


# ── листи з .ics, які синхронізація не зберігала (17.11.2026) ───────────────────
TEAMS_NEW = "https://teams.microsoft.com/meet/35067592779530?p=ERTecKgPwgKibAuB9"


def teams_ics(uid="040000008200E00074C5B7101A82E008-pieter"):
    """Запрошення Outlook / Teams з новим посиланням teams.microsoft.com/meet/… (як у листі Pieter)."""
    return ics(uid=uid, summary="Indoor positioning - next steps").replace(
        "https://teams.microsoft.com/l/meetup-join/\r\n 19%3ameeting_abc%40thread.v2/0?context=x",
        TEAMS_NEW).replace("LOCATION:Microsoft Teams-Besprechung",
                           "LOCATION:Microsoft Teams-vergadering\r\nX-MICROSOFT-SKYPETEAMSMEETINGURL:" + TEAMS_NEW)


def outlook_raw(uid=7, calendar=None) -> bytes:
    """Лист Outlook із запрошенням: текст, HTML і блок text/calendar без імені файлу."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    m = MIMEMultipart("alternative")
    m["Subject"] = "Indoor positioning - next steps"
    m["From"] = '"Pieter Oosthoek" <pieter@wireless.example>'
    m["To"] = ME
    m["Message-ID"] = f"<teams{uid}@wireless.example>"
    m["Date"] = "Mon, 16 Nov 2026 17:43:00 +0100"
    m.attach(MIMEText(f"Regards, Pieter\n\nMicrosoft Teams-vergadering\nDeelnemen: {TEAMS_NEW}", "plain", "utf-8"))
    m.attach(MIMEText(f"<p>Microsoft Teams-vergadering <a href='{TEAMS_NEW}'>Deelnemen</a></p>", "html", "utf-8"))
    cal = MIMEText(calendar or teams_ics(), "calendar", "utf-8")
    cal.set_param("method", "REQUEST")
    m.attach(cal)
    return m.as_bytes()


class NotSavedIcsTests(Base):
    """Щохвилинна синхронізація (sync_email) клала на диск лише PDF / DOCX / TXT / CSV — invite.ics листа Outlook
    не зберігався, і календар запрошення не бачив; нове посилання Teams (…/meet/…) не впізнавалось."""

    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)

    def fake_imap(self, mails):
        from unittest.mock import patch
        from email_assistant.tests_attachment_links import FakeConn, FakeIMAPClient
        conn = FakeConn(mails)
        return conn, patch("email_assistant.imap_client.IMAPClient", FakeIMAPClient(conn))

    def old_mail(self, uid=7):
        """Лист, збережений без файлу invite.ics (як до виправлення)."""
        from email_assistant.models import EmailMessage
        return EmailMessage.objects.create(
            account=self.account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name="INBOX", imap_uid=uid,
            subject="Indoor positioning - next steps", from_email="pieter@wireless.example", to_emails=[ME],
            body_text=f"Regards, Pieter\n\nMicrosoft Teams-vergadering\nDeelnemen: {TEAMS_NEW}",
            attachments=[{"name": "invite.ics", "content_type": "text/calendar", "size": 3000}],
            sent_at=datetime.datetime(2026, 11, 16, 16, 43, tzinfo=dt_tz.utc))

    def test_new_teams_link(self):
        [e] = invites.parse_ics(teams_ics())
        self.assertEqual(e["join_url"], TEAMS_NEW)
        self.assertTrue(invites.INVITE_HINT.search(f"Deelnemen: {TEAMS_NEW}"))

    def test_cron_sync_saves_the_ics_and_finds_the_invite(self):
        from io import StringIO
        from django.core.management import call_command
        from email_assistant.models import EmailMessage
        conn, imap = self.fake_imap({7: outlook_raw()})
        with imap:
            call_command("sync_email", stdout=StringIO())
        msg = EmailMessage.objects.get(imap_uid=7, imap_folder_name="INBOX")
        [att] = msg.attachments
        self.assertEqual(att["name"], "invite.ics")
        self.assertTrue(att["file_path"] and os.path.isfile(os.path.join(self.media, att["file_path"])))
        inv = MeetingInvite.objects.get()
        self.assertEqual((inv.email_message, inv.title, inv.join_url),
                         (msg, "Indoor positioning - next steps", TEAMS_NEW))
        self.assertEqual(inv.status, MeetingInvite.NEW)

    def test_preview_of_an_old_letter_takes_the_ics_from_imap_once(self):
        from email_assistant.views import _meeting_invites
        msg = self.old_mail()
        self.assertFalse(MeetingInvite.objects.exists())   # сигнал не мав файлу
        conn, imap = self.fake_imap({7: outlook_raw()})
        with imap:
            [inv] = _meeting_invites(self.user, [msg])
            self.assertEqual(conn.fetched, 1)
            msg.refresh_from_db()
            self.assertTrue(msg.attachments[0]["file_path"])
            self.assertEqual(_meeting_invites(self.user, [msg]), [inv])
            other = get_user_model().objects.create_user("bob", "b@x.y", "p", is_staff=True)
            self.assertEqual(_meeting_invites(other, [msg]), [])   # чужий лист — не розпізнаємо за власника
        self.assertEqual(conn.fetched, 1)                         # файл уже на диску — IMAP не питаємо
        self.assertEqual(inv.user, self.user)

    def test_imap_unavailable_is_tried_a_few_times_only(self):
        from unittest.mock import patch
        from email_assistant.views import _meeting_invites
        msg = self.old_mail()
        with patch("email_assistant.attachments.fetch_documents", side_effect=OSError("down")) as fetch:
            for _ in range(invites.FETCH_TRIES + 2):
                self.assertEqual(_meeting_invites(self.user, [msg]), [])
        self.assertEqual(fetch.call_count, invites.FETCH_TRIES)
        msg.refresh_from_db()
        self.assertEqual(msg.attachments[0]["cal_fail"], invites.FETCH_TRIES)

    def test_history_scan_finds_letters_with_unsaved_ics(self):
        self.old_mail()
        conn, imap = self.fake_imap({7: outlook_raw()})
        with imap:
            seen, found = invites.scan(self.user, datetime.date(2026, 11, 1))
        self.assertEqual((seen, found), (1, 1))
        self.assertEqual(MeetingInvite.objects.get().join_url, TEAMS_NEW)
