"""📨 Оригінал листа (email_assistant/originals.py): з сервера пошти як є, без сервера — з Minerva; лист як вкладення
(.eml, message/rfc822) проходить SMTP без base64 і відкривається в Minerva (imap_client._get_attachments_raw).

python manage.py test email_assistant.tests_originals --settings=tabele.settings_test
"""
import email
import os
import shutil
import tempfile
from email.message import EmailMessage as Mime
from email.policy import SMTP, default
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from email_assistant import originals
from email_assistant.imap_client import _get_attachments_raw
from email_assistant.models import EmailAccount, EmailMessage
from email_assistant.smtp_client import SMTPClient


def raw_letter(message_id="<m1@hrpcba.com>", subject="Quotation HR-031", pdf=b"%PDF-1.4 quote") -> bytes:
    m = Mime(policy=SMTP)
    m["From"] = "Anduin Chen <anduin@hrpcba.com>"
    m["To"] = "slava@sevskiy.de"
    m["Subject"] = subject
    m["Message-ID"] = message_id
    m.set_content("Dear Slava,\nplease find our quotation attached.")
    m.add_alternative("<p>Dear Slava,<br><b>please</b> find our quotation attached.</p>", subtype="html")
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="HR-031 Quotation.pdf")
    return m.as_bytes()


class FakeIMAP:
    """IMAPClient: store {(папка, uid): байти}; calls — що питали."""
    store, calls = {}, []

    def __init__(self, account):
        self.account, self.folder, self.conn = account, None, self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def select_folder(self, folder):
        self.folder = folder
        return True

    def uid(self, cmd, uid, what):
        FakeIMAP.calls.append((self.folder, int(uid), what))
        data = FakeIMAP.store.get((self.folder, int(uid)))
        if data is None:
            return "OK", [None]
        return "OK", [(b"1 (UID %s BODY[] {%d}" % (uid, len(data)), data), b")"]


class OriginalsBase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        self.user = User.objects.create_user("slava", "slava@sevskiy.de", "p", is_staff=True)
        self.account = EmailAccount.objects.create(user=self.user, email_address="slava@sevskiy.de",
                                                   display_name="Slava", smtp_host="smtp.x", smtp_port=587,
                                                   smtp_username="slava@sevskiy.de", smtp_password="p",
                                                   imap_username="slava@sevskiy.de", imap_password="p")
        FakeIMAP.store, FakeIMAP.calls = {}, []

    def msg(self, uid=7, folder="INBOX", message_id="<m1@hrpcba.com>", attachments=None, **kw):
        data = dict(account=self.account, folder="inbox", imap_folder_name=folder, imap_uid=uid,
                    subject="Quotation HR-031", from_email="anduin@hrpcba.com", from_name="Anduin Chen",
                    to_emails=["slava@sevskiy.de"], body_text="Dear Slava,\nquote attached.",
                    body_html="<p>Dear Slava,<br>quote attached.</p>", message_id=message_id,
                    sent_at=timezone.now(), attachments=attachments or [])
        data.update(kw)
        return EmailMessage.objects.create(**data)


class FetchTests(OriginalsBase):
    def test_letter_on_server_comes_as_is_without_marking_read(self):
        m = self.msg()
        FakeIMAP.store[("INBOX", 7)] = raw = raw_letter()
        with mock.patch("email_assistant.imap_client.IMAPClient", FakeIMAP):
            got = originals.fetch([m])
        self.assertEqual(got[m.pk].data, raw)
        self.assertFalse(got[m.pk].rebuilt)
        self.assertEqual(FakeIMAP.calls, [("INBOX", 7, "(BODY.PEEK[])")])      # PEEK — не «прочитано»

    def test_other_letter_under_the_uid_or_no_server_copy_is_rebuilt(self):
        moved = self.msg(uid=8, message_id="<other@x>")                     # UID тепер у іншого листа
        FakeIMAP.store[("INBOX", 8)] = raw_letter()
        local = self.msg(uid=0, folder="minerva:<s1@sevskiy.de>", message_id="<s1@sevskiy.de>")
        with mock.patch("email_assistant.imap_client.IMAPClient", FakeIMAP):
            got = originals.fetch([moved, local])
        self.assertTrue(got[moved.pk].rebuilt)
        self.assertTrue(got[local.pk].rebuilt)
        self.assertEqual([c[1] for c in FakeIMAP.calls], [8])                # копії з Minerva на сервері не шукаємо

    def test_server_down_every_letter_from_minerva(self):
        m = self.msg()
        with mock.patch("email_assistant.imap_client.IMAPClient", side_effect=OSError("no imap")):
            got = originals.fetch([m])
        self.assertTrue(got[m.pk].rebuilt)
        parsed = email.message_from_bytes(got[m.pk].data, policy=default)
        self.assertEqual(parsed["Subject"], "Quotation HR-031")


class RebuildTests(OriginalsBase):
    def test_headers_body_and_files_from_disk_missing_named(self):
        os.makedirs(os.path.join(self.media, "email_attachments", "1"), exist_ok=True)
        with open(os.path.join(self.media, "email_attachments", "1", "0_q.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 saved")
        m = self.msg(uid=0, folder="eml:x", subject="Re:\r\n Quotation", attachments=[
            {"name": "q.pdf", "content_type": "application/pdf", "size": 14, "file_path": "email_attachments/1/0_q.pdf"},
            {"name": "drawing.step", "content_type": "application/octet-stream", "size": 9},
            {"name": "evil.pdf", "content_type": "application/pdf", "size": 1, "file_path": "../../etc/passwd"}])
        data, missing = originals.rebuild(m)
        self.assertEqual(missing, ["drawing.step", "evil.pdf"])
        parsed = email.message_from_bytes(data, policy=default)
        self.assertEqual(parsed["Subject"], "Re: Quotation")                   # перенос рядка в темі — не ламає
        self.assertEqual(parsed["Message-ID"], "<m1@hrpcba.com>")
        self.assertIn("Anduin Chen", parsed["From"])
        self.assertIn("quote attached", parsed.get_body(("html",)).get_content())
        atts = list(parsed.iter_attachments())
        self.assertEqual([a.get_filename() for a in atts], ["q.pdf"])
        self.assertEqual(atts[0].get_content(), b"%PDF-1.4 saved")
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))                   # усі рядки — CRLF

    def test_estimate_and_filename(self):
        m = self.msg(attachments=[{"name": "a.pdf", "size": 1000000}])
        self.assertGreater(originals.estimate(m), 1370000)
        name = originals.filename(m, "HongRong (shenzhen)")
        when = timezone.localtime(m.sent_at).strftime("%Y-%m-%d %H-%M")
        self.assertEqual(name, f"HongRong (shenzhen) - {when} Anduin Chen - Quotation HR-031.eml")
        m.subject = 'RE: a/b\\c:"d"?*<e>|\r\nf'
        bad = originals.filename(m)
        self.assertTrue(bad.endswith("RE a b c d e f.eml"), bad)               # символи, яких не приймає Windows


class AsAttachmentTests(OriginalsBase):
    def test_eml_goes_through_smtp_unchanged_and_opens_in_minerva(self):
        inner = raw_letter()
        sent = []

        class Conn:
            def sendmail(self, frm, rcpt, data):
                sent.append(data)

            def quit(self):
                pass
        with mock.patch("email_assistant.smtp_client.SMTPClient._open", return_value=Conn()), \
                mock.patch("core.smtp_utf8.smtp_login"), \
                mock.patch("email_assistant.imap_client.IMAPClient", side_effect=OSError("no imap")):
            out = SMTPClient(self.account).send(
                to_emails=["olga@sevskiy.de"], subject="RFQ — листи", body_text="Листи у вкладенні",
                attachments=[originals.attachment("2026-11-19 HongRong - Quotation.eml", inner),
                             {"name": "extra.pdf", "content": b"%PDF extra", "content_type": "application/pdf"}],
                signature=False)
        self.assertTrue(out["ok"])
        outer = email.message_from_bytes(sent[0], policy=default)
        part = [p for p in outer.iter_attachments() if p.get_content_type() == "message/rfc822"][0]
        self.assertEqual(part.get_filename(), "2026-11-19 HongRong - Quotation.eml")
        self.assertIn(part["Content-Transfer-Encoding"], ("7bit", "8bit"))     # не base64
        letter = part.get_payload(0)
        self.assertEqual(letter["Subject"], "Quotation HR-031")
        pdf = [p for p in letter.iter_attachments()][0]
        self.assertEqual(pdf.get_content(), b"%PDF-1.4 quote")
        # Minerva колеги: .eml — з вмістом (раніше 0 байт), вкладення всередині — як і раніше, далі
        atts = _get_attachments_raw(email.message_from_bytes(sent[0]))
        self.assertEqual([a["name"] for a in atts],
                         ["2026-11-19 HongRong - Quotation.eml", "HR-031 Quotation.pdf", "extra.pdf"])
        self.assertIn(b"Subject: Quotation HR-031", atts[0]["_data"])
        self.assertEqual(email.message_from_bytes(atts[0]["_data"])["Message-ID"], "<m1@hrpcba.com>")
