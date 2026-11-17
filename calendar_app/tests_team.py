"""Календар команди («x_Alle Mitarbeiter»): спільні зустрічі «V, S: Telco Wirelessworks» + учасники.
python manage.py test calendar_app.tests_team --settings=tabele.settings_test"""
import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from calendar_app import invites, sync, team
from calendar_app.models import CalendarAccount, CalendarSettings, MeetingInvite, SyncRule
from calendar_app.tests_invites import ME, ics
from calendar_app.tests_sync import FakeRemote
from email_assistant.models import EmailAccount, EmailSettings
from email_assistant.sync import save_message

SERGEY = "sergey@sevskiy.example"
PIETER = "p.oosthoek@wirelessworks.it"


def invite_ics(uid, attendees, day):
    """Запрошення Outlook на завтра: організатор — Pieter (сторонній), учасники — як задано."""
    lines = "".join(f'ATTENDEE;ROLE=REQ-PARTICIPANT;CN="{cn}";RSVP=TRUE:mailto:{email}\r\n' for cn, email in attendees)
    text = ics(uid=uid, summary="Indoor positioning - next steps", start=f"{day:%Y%m%d}T140000",
               end=f"{day:%Y%m%d}T150000", extra=lines)
    return text.replace('ORGANIZER;CN="Max Muster":mailto:max@acme.example',
                        f'ORGANIZER;CN="Pieter Oosthoek":mailto:{PIETER}')


class TeamCalendarTests(TestCase):
    def setUp(self):
        from core.shared_cache import cache
        cache.clear()
        FakeRemote.store = {}
        User = get_user_model()
        self.user = User.objects.create_user("slava", ME, "x", first_name="Viacheslav", is_staff=True,
                                             is_superuser=True)
        self.sergey = User.objects.create_user("sergey", SERGEY, "x", first_name="Sergey", is_staff=True)
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x", smtp_username=ME,
            smtp_password="x", imap_folder_inbox="INBOX")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        p = mock.patch("calendar_app.sync.client", side_effect=FakeRemote)
        p.start()
        self.addCleanup(p.stop)
        self.g = CalendarAccount.objects.create(user=self.user, provider="google", email="g@x.y")
        sync.refresh_calendars(self.g)
        self.team_cal = self.g.calendars.get()
        self.day = timezone.localdate() + datetime.timedelta(days=1)
        self.uid = 0

    def meeting(self, attendees, accept=True):
        """Лист Pieter'а із запрошенням → MeetingInvite → (✓) подія Minerva."""
        self.uid += 1
        save_message(self.account, {
            "uid": self.uid, "message_id": f"<t{self.uid}@wirelessworks.it>", "in_reply_to": "", "references": [],
            "subject": "Indoor positioning - next steps", "from_email": PIETER, "from_name": "Pieter Oosthoek",
            "to_emails": [ME], "cc_emails": [], "body_text": invite_ics(f"m-{self.uid}", attendees, self.day),
            "body_html": "", "attachments": [], "is_read": True,
            "sent_at": timezone.now() - datetime.timedelta(hours=1)}, "INBOX")
        inv = MeetingInvite.objects.get(uid=f"m-{self.uid}")
        if accept:
            invites.accept(inv, self.user)
        return inv

    def rule(self, **kw):
        return SyncRule.objects.create(user=self.user, source=None, target=self.team_cal, what=SyncRule.TEAM,
                                       mode=SyncRule.TEAM, **kw)

    def copies(self):
        return list(FakeRemote.store.get(self.team_cal.remote_id, {}).values())

    def test_shared_meeting_gets_letters_company_and_our_people(self):
        self.meeting([("Viacheslav Pryimak", ME), ("Sergey", SERGEY), ("Pieter Oosthoek", PIETER)])
        rule = self.rule()
        plan = sync.run(rule)
        self.assertEqual(len(plan["create"]), 1)
        [copy] = self.copies()
        self.assertEqual(copy["title"], "V, S: Telco Wirelessworks")
        self.assertEqual(copy["description"].split("\n")[0], "👥 Учасники: Viacheslav (V), Sergey (S)")
        again = sync.run(rule)                                    # ще прохід — без дублів і без змін
        self.assertEqual((again["create"], again["update"]), ([], []))

    def test_participants_all_or_none(self):
        self.meeting([("Viacheslav Pryimak", ME), ("Sergey", SERGEY), ("Pieter Oosthoek", PIETER)])
        sync.run(self.rule(participants=SyncRule.PEOPLE_ALL))
        first_line = self.copies()[0]["description"].split("\n")[0]
        self.assertEqual(first_line, "👥 Учасники: Viacheslav (V), Sergey (S), Pieter Oosthoek")
        FakeRemote.store = {}
        sync.run(self.rule(participants=SyncRule.PEOPLE_NONE))
        self.assertFalse(self.copies()[0]["description"].startswith("👥"))

    def test_only_meetings_with_a_colleague(self):
        self.meeting([("Viacheslav Pryimak", ME), ("Pieter Oosthoek", PIETER)])   # лише я з наших
        self.meeting([("Sergey", SERGEY)], accept=False)                          # не прийнято — не моя зустріч
        self.assertEqual(sync.run(self.rule())["create"], [])
        # «Лише зустрічі» + назва для команди — і зустріч лише зі мною, з однією буквою
        rule = SyncRule.objects.create(user=self.user, source=None, target=self.team_cal, what=SyncRule.MEETINGS,
                                       mode=SyncRule.TEAM)
        sync.run(rule)
        self.assertEqual([c["title"] for c in self.copies()], ["V: Telco Wirelessworks"])

    def test_letters_from_settings_and_crm_company(self):
        CalendarSettings.objects.update_or_create(user=self.sergey, defaults={"team_letter": "SG"})
        CalendarSettings.objects.update_or_create(user=self.user, defaults={"team_letter": "VP"})
        inv = self.meeting([("Sergey", SERGEY), ("Pieter Oosthoek", PIETER), ("Yuriy", "yuriy@sevskiy.example")])
        from crm.models import Customer
        MeetingInvite.objects.filter(pk=inv.pk).update(
            crm_customer=Customer.objects.create(name="Pieter Oosthoek", company="Wireless Works B.V.",
                                                 email=PIETER))
        sync.run(self.rule(title_template="{who}: {company} – {title}"))
        [copy] = self.copies()
        self.assertEqual(copy["title"], "VP, SG, Y: Wireless Works – Indoor positioning - next steps")
        self.assertIn("Yuriy (Y)", copy["description"])   # наша адреса без людини Minerva — за іменем

    def test_page_letter_and_rule_form(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(self.user)
        self.client.post("/calendar/connections/sync/", {"action": "team_letter", "team_letter": " v."})
        self.assertEqual(CalendarSettings.for_user(self.user).team_letter, "V")
        self.client.post("/calendar/connections/sync/", {
            "action": "rule_add", "source": "", "target": str(self.team_cal.pk), "what": "team", "mode": "team",
            "title_template": "{who}: Telco {company}", "participants": "all", "days_back": "7",
            "days_ahead": "90", "propagate_delete": "1"})
        rule = SyncRule.objects.get()
        self.assertEqual((rule.what, rule.mode, rule.participants, rule.enabled), ("team", "team", "all", False))
        page = self.client.get("/calendar/connections/")
        self.assertContains(page, "Моя буква в календарі команди")
        self.assertContains(page, "{who}: Telco {company}")

    def test_helpers(self):
        self.assertEqual(team.company_of_domain("pieter@mail.wirelessworks.it"), "Wirelessworks")
        self.assertEqual(team.company_of_domain("a@shop.example.co.uk"), "Example")
        self.assertEqual(team.short_company("Muster GmbH & Co. KG"), "Muster")
        self.assertEqual(team.short_company("Wireless Works B.V."), "Wireless Works")
        self.assertEqual(team.clean_letter(" v."), "V")
