"""📨 Запрошення на зустрічі в проєкті (projects/invites.py): варіанти «оберіть один», ✅ / ❔ / ✕ з пульта (лист
організатору — SMTP підмінено), решту варіантів відхилити / не заносити, змінено / скасовано організатором, відповідь
з Outlook (calendar_app.invites.track_reply), приватність, шкала.

python manage.py test projects.tests_invites --settings=tabele.settings_test
"""
import json
from datetime import timedelta
from datetime import timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from calendar_app.models import MeetingInvite
from email_assistant.models import EmailAccount
from projects import cockpit, invites, overview, services, timeline
from projects.tests import ME, ProjectsBase

ORG = "buyer@client.example"


def ics(uid, start, title="Telco Beacon", method="REQUEST", seq=0, organizer=ORG, attendee=ME, partstat="NEEDS-ACTION"):
    fmt = "%Y%m%dT%H%M%SZ"
    s = start.astimezone(dt_tz.utc).strftime(fmt)
    e = (start + timedelta(hours=1)).astimezone(dt_tz.utc).strftime(fmt)
    return ("BEGIN:VCALENDAR\r\n" f"METHOD:{method}\r\n" "BEGIN:VEVENT\r\n" f"UID:{uid}\r\n" f"SEQUENCE:{seq}\r\n"
            f"DTSTART:{s}\r\n" f"DTEND:{e}\r\n" f"SUMMARY:{title}\r\n" f"ORGANIZER;CN=Max Muster:mailto:{organizer}\r\n"
            f"ATTENDEE;CN=Slava;PARTSTAT={partstat};RSVP=TRUE:mailto:{attendee}\r\n"
            + ("STATUS:CANCELLED\r\n" if method == "CANCEL" else "") + "END:VEVENT\r\nEND:VCALENDAR\r\n")


def smtp(ok=True):
    return patch("email_assistant.smtp_client.SMTPClient.send", return_value={"ok": ok, "error": "" if ok else "x"})


class InviteBase(ProjectsBase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now().replace(microsecond=0)
        self.ask = self.mail("Anfrage 5G Antenne", body="Wir brauchen 500 Stück", sent=self.now - timedelta(days=3))
        self.project = services.create_project(user=self.user, threads=[self.ask.thread])
        self._n = 0

    def invite(self, title="Telco Beacon", days=3, uid=None, method="REQUEST", seq=0, hour=10, thread=True,
               subject=None):
        self._n += 1
        start = (self.now + timedelta(days=days)).replace(hour=hour, minute=0, second=0)
        body = ics(uid or f"uid-{self._n}@client", start, title, method=method, seq=seq)
        msg = self.mail(subject or f"Einladung: {title}", body=body, sent=self.now - timedelta(hours=self._n),
                        in_reply_to=self.ask.message_id if thread else "",
                        references=[self.ask.message_id] if thread else ())
        return msg, MeetingInvite.objects.get(user=self.user, uid=uid or f"uid-{self._n}@client")

    def facts(self):
        return overview.collect(self.project)

    def texts(self, user=None):
        return [a["text"] for a in overview.visible(self.facts()["attention"], user or self.user)]

    def post(self, body, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(reverse("projects:invite", args=[self.project.pk]), json.dumps(body),
                                content_type="application/json")


class InviteAttentionTests(InviteBase):
    def test_invite_in_project_thread_needs_answer(self):
        msg, inv = self.invite()
        facts = self.facts()
        self.assertEqual([i.pk for i in facts["invites"]], [inv.pk])
        texts = self.texts()
        self.assertTrue(any(t.startswith("Запрошення на зустріч чекає відповіді: Telco Beacon") for t in texts), texts)
        self.assertFalse(any(t.startswith("Чекає нашої відповіді") for t in texts))     # не двічі той самий лист
        block = cockpit.context(self.project, self.user)["invites"]
        self.assertEqual(block["open"], 1)
        row = block["groups"][0]["rows"][0]
        self.assertEqual([a["key"] for a in row["answers"]], ["accepted", "tentative", "declined"])
        self.assertEqual(row["mail_url"], f"/email/message/{msg.pk}/")

    def test_variants_choose_one_and_decline_the_rest(self):
        a = self.invite("Telco Beacon – Option 1", days=3)[1]
        b = self.invite("Telco Beacon – Option 2", days=4)[1]
        c = self.invite("Telco Beacon – Option 3", days=5)[1]
        texts = self.texts()
        self.assertTrue(any(t.startswith("Пропонують 3 варіанти зустрічі") for t in texts), texts)
        g = cockpit.context(self.project, self.user)["invites"]["groups"][0]
        self.assertTrue(g["choose"])
        self.assertEqual([r["variant"] for r in g["rows"]], ["варіант 1 з 3", "варіант 2 з 3", "варіант 3 з 3"])
        with smtp() as send:
            data = self.post({"invite": b.pk, "answer": "accepted", "others": "decline"}).json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["declined"], 2)
        self.assertEqual(send.call_count, 3)                     # «Буду» + два «✕» організатору
        self.assertIn("решту варіантів відхилено: 2", data["html"])
        for inv, answer in ((a, "declined"), (b, "accepted"), (c, "declined")):
            inv.refresh_from_db()
            self.assertEqual(inv.response, answer)
        self.assertIn("обрано:", data["html"])
        self.assertFalse(any("варіант" in t for t in self.texts()))

    def test_others_dismiss_without_mail(self):
        a = self.invite("Call – Variante A", days=2)[1]
        b = self.invite("Call – Variante B", days=6)[1]
        with smtp() as send:
            data = self.post({"invite": a.pk, "answer": "accepted", "others": "dismiss"}).json()
        self.assertEqual(send.call_count, 1)
        b.refresh_from_db()
        self.assertEqual((b.status, b.response), (MeetingInvite.DISMISSED, ""))
        self.assertIn("не заносимо: 1", data["html"])

    def test_moved_and_cancelled_by_organizer(self):
        _, inv = self.invite(uid="move@client")
        with smtp():
            self.post({"invite": inv.pk, "answer": "accepted"})
        self.invite(uid="move@client", seq=1, days=8)                    # організатор переніс
        inv.refresh_from_db()
        self.assertEqual(invites.state(inv), invites.MOVED)
        self.assertTrue(any(t.startswith("Зустріч змінено: Telco Beacon") for t in self.texts()))
        self.invite(uid="move@client", seq=2, days=8, method="CANCEL")
        inv.refresh_from_db()
        self.assertEqual(invites.state(inv), invites.CANCEL)
        self.assertTrue(any(t.startswith("Організатор скасував зустріч") for t in self.texts()))
        data = self.post({"invite": inv.pk, "answer": "remove"}).json()
        self.assertTrue(data["ok"], data)
        inv.refresh_from_db()
        self.assertEqual(inv.status, MeetingInvite.CANCELLED)
        self.assertTrue(inv.event.title.startswith("✖ "))

    def test_send_failure_changes_nothing(self):
        _, inv = self.invite()
        with smtp(ok=False):
            r = self.post({"invite": inv.pk, "answer": "accepted"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Не вдалося відповісти", r.json()["error"])
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.response), (MeetingInvite.NEW, ""))

    def test_customer_invite_only_for_their_only_open_project(self):
        self.project.customer = self.client_co
        self.project.save()
        _, inv = self.invite("Besuch bei Client", thread=False, subject="Besuch")       # не в гілці проєкту
        self.assertEqual(inv.email_message.thread.crm_customer_id, self.client_co.pk)
        self.assertIn(inv.pk, [i.pk for i in self.facts()["invites"]])
        services.create_project(user=self.user, name="Другий проєкт", customer=self.client_co)
        self.assertNotIn(inv.pk, [i.pk for i in self.facts()["invites"]])


class InviteTrackingTests(InviteBase):
    def test_reply_from_outlook_is_seen(self):
        msg, inv = self.invite()
        start = timezone.localtime(inv.start_at)
        reply = ics(inv.uid, start, method="REPLY", partstat="ACCEPTED")
        self.mail("Accepted: Telco Beacon", from_email=ME, to=(ORG,), body=reply, sent=self.now, folder="Sent")
        inv.refresh_from_db()
        self.assertEqual((inv.status, inv.response), (MeetingInvite.ADDED, "accepted"))
        self.assertIsNotNone(inv.event_id)
        self.assertFalse(any("чекає відповіді" in t for t in self.texts()))
        row = cockpit.context(self.project, self.user)["invites"]["groups"][0]["rows"][0]
        self.assertEqual(row["state"], invites.ACCEPTED)

    def test_reply_of_somebody_else_or_older_is_ignored(self):
        _, inv = self.invite()
        start = timezone.localtime(inv.start_at)
        other = ics(inv.uid, start, method="REPLY", partstat="DECLINED", attendee="pieter@client.example")
        self.mail("Declined: Telco Beacon", from_email="pieter@client.example", body=other, sent=self.now)
        inv.refresh_from_db()
        self.assertEqual(inv.response, "")
        with smtp():
            self.post({"invite": inv.pk, "answer": "accepted"})
        old = ics(inv.uid, start, method="REPLY", partstat="DECLINED")
        self.mail("Declined: Telco Beacon", from_email=ME, to=(ORG,), body=old, sent=self.now - timedelta(days=1),
                  folder="Sent")
        inv.refresh_from_db()
        self.assertEqual(inv.response, "accepted")                        # старіша відповідь не перебиває


class InvitePrivacyTests(InviteBase):
    def test_colleague_invite_is_private(self):
        bob = User.objects.create_superuser("bob", "b@x.y", "p")
        acc = EmailAccount.objects.create(user=bob, email_address="bob@sevskiy.de", imap_username="bob",
                                          imap_password="x", smtp_username="bob", smtp_password="x")
        mine, self.account = self.account, acc
        secret = self.mail("Geheim: Preisrunde", body=ics("secret@client", self.now + timedelta(days=2),
                                                          "Geheim: Preisrunde", attendee="bob@sevskiy.de"),
                           sent=self.now)
        self.account = mine
        services.link_thread(self.project, secret.thread, user=bob)
        inv = MeetingInvite.objects.get(user=bob)
        self.assertTrue(any("Geheim" in t for t in self.texts(bob)))
        mine_texts = self.texts(self.user)
        self.assertFalse(any("Geheim" in t for t in mine_texts))
        self.assertTrue(any("чужа скринька" in t for t in mine_texts))
        self.assertEqual(cockpit.context(self.project, self.user)["invites"]["groups"], [])
        self.assertNotIn("Geheim", timeline.render(self.project, self.facts(), self.user))
        r = self.post({"invite": inv.pk, "answer": "accepted"})
        self.assertEqual(r.status_code, 400)
        inv.refresh_from_db()
        self.assertEqual(inv.response, "")


class InviteTimelineTests(InviteBase):
    def test_open_invite_on_timeline_until_answered(self):
        _, inv = self.invite()
        evs = timeline.events(self.project, self.facts(), self.user)
        self.assertTrue(any(e["label"].startswith("📨 Telco Beacon") for e in evs))
        with smtp():
            self.post({"invite": inv.pk, "answer": "accepted"})
        evs = timeline.events(self.project, self.facts(), self.user)
        self.assertFalse(any(e["label"].startswith("📨") for e in evs))
        self.assertTrue(any("Telco Beacon" in e["label"] for e in evs if e["lane"] == "calendar"))   # уже подія

    def test_page_shows_block(self):
        self.invite()
        r = self.client.get(f"/admin/projects/project/{self.project.pk}/change/")
        self.assertContains(r, 'id="pj-invites"')
        self.assertContains(r, "📨 Запрошення на зустрічі")
        self.assertContains(r, 'data-pjc-inv-act="accepted"')
