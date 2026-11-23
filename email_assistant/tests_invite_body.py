"""📅 Запрошення Outlook / Teams лише блоком text/calendar: у листі — текст і HTML події (як в Outlook, з клікабельним
посиланням Teams), а не сирий «BEGIN:VCALENDAR…»; iCalendar лишається вкладенням invite.ics для календаря;
«🎥 Приєднатися» у смузі «📅 Запрошення».

python manage.py test email_assistant.tests_invite_body --settings=tabele.settings_test
"""
import email
import os
import shutil
import tempfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from email_assistant.imap_client import _get_attachments_raw, _get_body, calendar_body, looks_like_ics

JOIN = "https://teams.microsoft.com/meet/3339841575490221?p=8fZ1XnZBIibUpHvOoy"
ICS = ("BEGIN:VCALENDAR\r\nPRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN\r\nVERSION:2.0\r\n"
       "METHOD:REQUEST\r\nBEGIN:VTIMEZONE\r\nTZID:W. Europe Standard Time\r\nBEGIN:STANDARD\r\n"
       "DTSTART:16011028T030000\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
       "BEGIN:VEVENT\r\nATTENDEE;CN=\"Viacheslav Pryimak\";RSVP=TRUE:mailto:viacheslav.pryimak@sevski\r\n y.de\r\n"
       "DESCRIPTION;LANGUAGE=de-DE:______________\\n\\nMicrosoft Teams-Besprechung\\n\\nTe\r\n"
       f" ilnehmen: {JOIN}\\n\\nBesprechungs-ID: 333 984 157 549 022 1\\nPasscode: fV6UL9DQ\\n\r\n"
       "UID:040000008200E00074C5B7101A82E00800000000\r\nSUMMARY;LANGUAGE=de-DE:Interne Besprechung\r\n"
       "DTSTART;TZID=W. Europe Standard Time:20261123T153000\r\nDTEND;TZID=W. Europe Standard Time:20261123T160000\r\n"
       "ORGANIZER;CN=\"Dr. Sergey Sevskiy\":mailto:sergey@sevskiy.de\r\nLOCATION:Microsoft Teams-Besprechung\r\n"
       "X-ALT-DESC;FMTTYPE=text/html:<html><body><p>Microsoft Teams-Besprechung</p><p><b>Teilnehmen: </b>\r\n"
       f" <a href=\"{JOIN}\">{JOIN}</a></p></body></html>\r\n"
       "BEGIN:VALARM\r\nDESCRIPTION:REMINDER\r\nTRIGGER;RELATED=START:-PT15M\r\nACTION:DISPLAY\r\nEND:VALARM\r\n"
       "END:VEVENT\r\nEND:VCALENDAR\r\n")


def calendar_only():
    msg = MIMEText(ICS, "calendar", "utf-8")
    msg.replace_header("Content-Type", 'text/calendar; charset="utf-8"; method=REQUEST')
    msg["Subject"] = "Interne Besprechung"
    return email.message_from_bytes(msg.as_bytes())


class CalendarBodyTests(SimpleTestCase):
    def test_event_text_and_html(self):
        text, html = calendar_body(ICS)
        self.assertIn(f"Teilnehmen: {JOIN}", text)           # згорнутий рядок розгорнуто
        self.assertNotIn("REMINDER", text)                    # опис нагадування — ні
        self.assertIn(f'<a href="{JOIN}">', html)
        self.assertTrue(looks_like_ics(ICS) and not looks_like_ics("Hallo\nBEGIN:VCALENDAR"))
        self.assertEqual(calendar_body("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Call\nLOCATION:Room 1\nEND:VEVENT\n"
                                       "END:VCALENDAR"), ("Call\nRoom 1", ""))

    def test_calendar_only_message(self):
        msg = calendar_only()
        text, html = _get_body(msg)
        self.assertNotIn("BEGIN:VCALENDAR", text)
        self.assertIn(JOIN, text)
        self.assertIn(f'<a href="{JOIN}">', html)
        atts = _get_attachments_raw(msg)                      # iCalendar — вкладенням для календаря
        self.assertEqual([a["name"] for a in atts], ["invite.ics"])
        self.assertIn(b"BEGIN:VCALENDAR", atts[0]["_data"])

    def test_multipart_html_is_kept(self):
        mixed = MIMEMultipart("alternative")
        mixed.attach(MIMEText("Hallo", "plain", "utf-8"))
        mixed.attach(MIMEText("<p>Hallo HTML</p>", "html", "utf-8"))
        mixed.attach(MIMEText(ICS, "calendar", "utf-8"))
        text, html = _get_body(email.message_from_bytes(mixed.as_bytes()))
        self.assertEqual((text, html), ("Hallo", "<p>Hallo HTML</p>"))
        only_cal = MIMEMultipart("mixed")
        only_cal.attach(MIMEText(ICS, "calendar", "utf-8"))
        text, html = _get_body(email.message_from_bytes(only_cal.as_bytes()))
        self.assertIn(JOIN, text)


class OldInviteLetterTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from email_assistant.models import EmailAccount, EmailMessage, EmailSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        self.user = User.objects.create_superuser("slava", "s@x.y", "p")
        self.account = EmailAccount.objects.create(user=self.user, email_address="viacheslav.pryimak@sevskiy.de",
                                                   is_primary=True)
        EmailSettings.objects.filter(pk=EmailSettings.get_for_user(self.user).pk).update(mark_read_on_server=False)
        self.msg = EmailMessage.objects.create(
            account=self.account, folder="inbox", imap_folder_name="INBOX", imap_uid=7, subject="Interne Besprechung",
            body_text=ICS, from_email="sergey@sevskiy.de", sent_at=timezone.now(),
            attachments=[{"name": "invite.ics", "content_type": "text/calendar", "size": len(ICS)}])

    def test_fix_keeps_calendar_file_and_shows_event_text(self):
        from calendar_app.invites import calendars_of
        self.assertTrue(self.msg.fix_html_body())
        self.msg.refresh_from_db()
        self.assertIn(JOIN, self.msg.body_text)
        self.assertIn(f'<a href="{JOIN}">', self.msg.body_html)
        path = self.msg.attachments[0]["file_path"]
        self.assertTrue(os.path.isfile(os.path.join(self.media, path)))
        self.assertEqual(len(calendars_of(self.msg)), 1)      # календар і далі бачить зустріч
        self.assertIn("UID:040000008200E00074C5B7101A82E00800000000", calendars_of(self.msg)[0])
        self.assertFalse(self.msg.fix_html_body())             # удруге — нічого

    def test_preview_shows_event_text_and_join_button(self):
        from calendar_app.models import MeetingInvite
        self.client.force_login(self.user)
        page = self.client.get(f"/email/message/{self.msg.pk}/preview/").content.decode()
        self.assertNotIn("BEGIN:VCALENDAR", page)
        self.assertTrue(MeetingInvite.objects.filter(user=self.user, email_message=self.msg).exists())
        self.assertIn(f'href="{JOIN}"', page)
        self.assertIn("🎥", page)
        self.msg.refresh_from_db()
        self.assertIn(JOIN, self.msg.body_html)
