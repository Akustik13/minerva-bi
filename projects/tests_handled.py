"""✓ «Відповіли поза поштою» на пульті: лист, на який відповіли телефоном / на зустрічі, не висить у «Що зараз»
(новий лист у гілці — знову), позначає лише власник скриньки, ↶ знімає; зустрічі з відеонарад і візитів — не
«прострочено», відпустка — «минуло».
python manage.py test projects --settings=tabele.settings_test"""
import json

from projects import overview, terms
from projects.models import MailHandled
from projects.tests_terms import TermsBase


class MailHandledTests(TermsBase):
    def url(self):
        from django.urls import reverse
        return reverse("projects:mail_handled", args=[self.project.pk])

    def post(self, body, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(self.url(), json.dumps(body), content_type="application/json")

    def waiting(self):
        return [a for a in overview.collect(self.project)["attention"] if a["text"].startswith("Чекає нашої відповіді")]

    def test_phone_reply_hides_the_item_until_a_new_mail(self):
        items = self.waiting()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["act"]["m"], self.reply.pk)                    # ✓ — власнику листа
        r = self.post({"messages": [self.reply.pk], "how": "phone", "note": "Домовились про зразки"})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j["marked"], [self.reply.pk])
        self.assertEqual(j["notice"], "✓ 📞 Телефоном · 📌 записано в моменти")   # текст для сповіщення з «↶»
        self.assertIn("✓ Відповіли поза поштою · 1", j["html"])
        self.assertIn("Домовились про зразки", j["html"])
        self.assertFalse(self.waiting())
        facts = overview.collect(self.project)
        self.assertEqual([h["msg"].pk for h in facts["handled"]], [self.reply.pk])
        self.assertNotIn(self.reply, facts["waiting_us"])
        text, _refs = overview.prompt_text(facts)
        self.assertIn("ПОЗНАЧКА ЛЮДИНИ: на цей лист 📞 відповіли телефоном", text)       # AI знає про дзвінок
        self.mail("Re: Anfrage 5G Antenne", sent=self.ago(0), in_reply_to=self.reply.message_id,
                  references=(self.ask.message_id, self.reply.message_id), body="Und noch eine Frage")
        self.assertEqual(len(self.waiting()), 1)                                 # новий лист — знову чекає

    def test_undo_brings_the_item_back(self):
        self.post({"messages": [self.reply.pk], "how": "meeting"})
        self.assertTrue(MailHandled.objects.filter(message=self.reply).exists())
        r = self.post({"messages": [self.reply.pk], "undo": True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(MailHandled.objects.exists())
        self.assertEqual(len(self.waiting()), 1)

    def test_only_own_messages_and_known_choice(self):
        self.assertEqual(self.post({"messages": [self.reply.pk], "how": "telepathy"}).status_code, 400)
        olga, _acc = self.colleague()
        r = self.post({"messages": [self.reply.pk], "how": "phone"}, user=olga)
        self.assertEqual(r.status_code, 400)                                     # чужа скринька — ні
        self.assertFalse(MailHandled.objects.exists())
        seen = overview.visible(overview.collect(self.project)["attention"], olga)
        self.assertFalse([a for a in seen if a.get("act")])                     # ✓ і дані листа — лише власнику

    def test_cockpit_shows_the_button_to_the_owner(self):
        from django.urls import reverse
        html = self.client.get(reverse("projects:cockpit", args=[self.project.pk])).json()["html"]
        self.assertIn("data-pjc-act=", html)
        self.assertIn("data-mail-handled=", html)

    def test_overdue_event_gets_calendar_action(self):
        ev = self.event("Сплатити рахунок", -1, message=self.reply)
        dup = self.event("Сплатити рахунок", -1, message=self.ask)
        item = [a for a in overview.collect(self.project)["attention"] if a["text"].startswith("Прострочено")][0]
        self.assertEqual(item["act"]["ev"], [ev.pk, dup.pk])
        self.assertFalse(item["act"]["meeting"])


class MeetingWordsTests(TermsBase):
    def test_video_meetings_visits_and_holidays_are_not_overdue(self):
        for title in ("Videobesprechung LTE-Leiterplatten mit Herrn Dobler", "Відеонарада щодо виробництва LTE-плат",
                      "Візит Мартіна Доблера о 15:00", "Telefongespräch mit Kunde"):
            self.event(title, -3)
        self.event("Початок відпустки Viacheslav Priymak", -2)
        t = self.terms()
        self.assertEqual(t["Початок відпустки Viacheslav Priymak"].state, terms.PASSED)
        self.assertTrue(all(x.state == terms.HELD for k, x in t.items() if "відпустки" not in k))
        self.assertFalse([a for a in self.attention() if a.startswith("Прострочено")])

    def test_visit_only_in_the_title_and_person_choice_wins(self):
        from calendar_app.models import CalendarEvent
        ev = self.event("Datenblatt senden", -1)
        ev.description = "Please visit our website for details"
        ev.save()
        holiday = self.event("Urlaub-Vertretung organisieren", -1)
        CalendarEvent.objects.filter(pk=holiday.pk).update(type_by_user=True)
        t = self.terms()
        self.assertEqual(t["Datenblatt senden"].state, terms.OVERDUE)            # «visit» в описі — не зустріч
        self.assertEqual(t["Urlaub-Vertretung organisieren"].state, terms.OVERDUE)   # людина вибрала «термін»


class TalkNoteTests(TermsBase):
    """📝 «Про що йшла мова»: дзвінок / зустріч → 📌 момент (лист або подія календаря — для майбутньої транскрипції)."""
    def post(self, name, body, user=None):
        from django.urls import reverse
        if user is not None:
            self.client.force_login(user)
        return self.client.post(reverse(f"projects:{name}", args=[self.project.pk]), json.dumps(body),
                                content_type="application/json")

    def test_phone_reply_with_summary_becomes_a_call_moment(self):
        from projects.models import ProjectHighlight
        r = self.post("mail_handled", {"messages": [self.reply.pk], "how": "phone",
                                        "summary": "Погодили MHF4\nЗразки до 25.11, ціна після тесту"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("📌 записано в моменти", r.json()["notice"])
        h = ProjectHighlight.objects.get(project=self.project)
        self.assertEqual((h.kind, h.title, h.message_id), (ProjectHighlight.CALL, "Погодили MHF4", self.reply.pk))
        self.assertIn("Зразки до 25.11", h.quote)
        mark = MailHandled.objects.get(message=self.reply)
        self.assertEqual(mark.highlight_id, h.pk)
        self.post("mail_handled", {"messages": [self.reply.pk], "undo": True})
        self.assertFalse(ProjectHighlight.objects.exists())                     # ↶ прибирає й момент

    def test_meeting_held_with_summary_links_the_event(self):
        from projects.models import ProjectHighlight
        ev = self.event("Videobesprechung Muster", -2, kind="meeting")
        r = self.post("event_note", {"events": [ev.pk], "done": True, "summary": "Клієнт хоче 3 партії по 500"})
        self.assertEqual(r.status_code, 200)
        ev.refresh_from_db()
        self.assertTrue(ev.is_done)
        h = ProjectHighlight.objects.get(pk=r.json()["highlight"])
        self.assertEqual((h.kind, h.event_id, h.date), (ProjectHighlight.MEETING, ev.pk, ev.start_at.date()))
        self.post("event_note", {"events": [ev.pk], "undo": True, "highlight": h.pk})
        ev.refresh_from_db()
        self.assertFalse(ev.is_done)
        self.assertFalse(ProjectHighlight.objects.exists())

    def test_only_own_calendar(self):
        olga, _acc = self.colleague()
        ev = self.event("Telko", -1, kind="meeting")
        r = self.post("event_note", {"events": [ev.pk], "done": True, "summary": "x"}, user=olga)
        self.assertEqual(r.status_code, 400)
        ev.refresh_from_db()
        self.assertFalse(ev.is_done)
