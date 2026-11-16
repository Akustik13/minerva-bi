"""📋 Агенда наради (meetings/agenda.py, admin agenda_view, _agenda.html, пульт проєкту «🎙 Мої наради»):
python manage.py test meetings.tests_agenda --settings=tabele.settings_test

Пункти й підпункти (1.1) — лише власнику наради; підказки з проєкту без AI (ризики, віхи, кроки клієнта, «Що зараз», AI-огляд), уже
додане не пропонується; не обговорене на минулій нараді проєкту — підказка до наступної, узяли — там «перенесено»,
прибрали — знову «обговорити»; ✉️ лист з агендою — чернетка у власній скриньці, переклад AI (підмінено); у пульті — «📋 агенда N / M» і «📋 Підготувати агенду» до зустрічі з календаря.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from calendar_app.models import CalendarEvent
from meetings import agenda
from meetings.models import Meeting, MeetingAgendaItem
from meetings.tests_pages import PagesBase
from projects.models import ProjectOverview, ProjectRisk, ProjectStep


class AgendaBase(PagesBase):
    def send(self, m, **body):
        return self.client.post(self.url("agenda", m), json.dumps(body), content_type="application/json")

    def texts(self, m):
        return list(MeetingAgendaItem.objects.filter(meeting=m).order_by("order", "pk").values_list("text", flat=True))


class AgendaEditTests(AgendaBase):
    def test_add_edit_tick_move_remove(self):
        m = self.meeting()
        self.login(self.owner)
        for text in ("Строк зразків", "  Ціна   серії ", "Креслення корпусу"):
            data = self.send(m, action="add", text=text).json()
            self.assertTrue(data["ok"], data)
        self.assertEqual(self.texts(m), ["Строк зразків", "Ціна серії", "Креслення корпусу"])
        self.assertIn('id="agenda"', data["html"])
        a, b, c = MeetingAgendaItem.objects.filter(meeting=m).order_by("order")
        self.send(m, action="move", item=c.pk, delta=-1)
        self.assertEqual(self.texts(m), ["Строк зразків", "Креслення корпусу", "Ціна серії"])
        self.send(m, action="edit", item=a.pk, text="Строк зразків до 25.11", note="Погодили 25.11")
        self.send(m, action="status", item=a.pk, status="done")
        a.refresh_from_db()
        self.assertEqual((a.text, a.note, a.status), ("Строк зразків до 25.11", "Погодили 25.11", "done"))
        data = self.send(m, action="remove", item=b.pk).json()
        self.assertEqual(self.texts(m), ["Строк зразків до 25.11", "Креслення корпусу"])
        self.assertIn("обговорили 1 з 2", data["html"])
        self.assertIn("1. Строк зразків до 25.11 — Погодили 25.11", agenda.copy_text(m))
        self.assertFalse(self.send(m, action="add", text="   ").json()["ok"])
        self.assertFalse(self.send(m, action="status", item=a.pk, status="bogus").json()["ok"])

    def test_page_shows_agenda_and_only_owner_can_change(self):
        m = self.meeting()
        agenda.add(self.owner, m, "Строк зразків")
        self.login(self.owner)
        html = self.client.get(self.url("page", m)).content.decode()
        self.assertIn('id="agenda"', html)
        self.assertIn("Строк зразків", html)
        self.assertIn("🧭 Підказати з проєкту", html)
        self.login(self.other)                                                  # чужа нарада — 404, навіть адміну
        self.assertEqual(self.send(m, action="add", text="x").status_code, 404)
        self.assertEqual(self.client.get(self.url("agenda", m)).status_code, 405)


class AgendaSuggestTests(AgendaBase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        ProjectRisk.objects.create(project=self.project, title="Затримка плат", probability=3, impact=3)
        ProjectRisk.objects.create(project=self.project, title="Дрібниця", probability=1, impact=1)   # 🟢 — ні
        ProjectStep.objects.create(project=self.project, title="Зразки погоджено", kind=ProjectStep.MILESTONE,
                                   end=today + timedelta(days=5), side=ProjectStep.CUSTOMER)
        ProjectStep.objects.create(project=self.project, title="Клієнт дає креслення", side=ProjectStep.CUSTOMER,
                                   start=today + timedelta(days=2), end=today + timedelta(days=6))
        ProjectStep.objects.create(project=self.project, title="Далека віха", kind=ProjectStep.MILESTONE,
                                   end=today + timedelta(days=60))
        ProjectOverview.objects.create(project=self.project, data={
            "open_questions": [{"text": "Який роз'єм на серію?", "asked_by": "customer"}],
            "promises": [{"text": "Надіслати звіт вимірювань", "by": "we", "status": "open"},
                         {"text": "Уже зроблено", "by": "we", "status": "done"}]})
        self.project.due_date = today - timedelta(days=3)                          # «Строк проєкту минув»
        self.project.save()

    def test_suggestions_from_project_without_ai(self):
        m = self.meeting()
        rows = agenda.suggest(self.owner, m)
        texts = " | ".join(r["text"] for r in rows)
        for part in ("Затримка плат", "◆ Зразки погоджено", "Клієнт дає креслення", "❓ Який роз'єм на серію?",
                     "🤝 Надіслати звіт вимірювань", "Строк проєкту минув"):
            self.assertIn(part, texts)
        for part in ("Дрібниця", "Далека віха", "Уже зроблено"):
            self.assertNotIn(part, texts)
        self.assertEqual([r["group"] for r in rows], sorted((r["group"] for r in rows),
                                                            key=[g for g, _ in agenda.GROUPS].index))

    def test_take_one_and_all_without_duplicates(self):
        m = self.meeting()
        self.login(self.owner)
        data = self.send(m, action="suggest").json()
        self.assertIn("🧭 Підказки з проєкту", data["html"])
        ref = next(r["ref"] for r in agenda.suggest(self.owner, m) if r["ref"].startswith("risk:"))
        data = self.send(m, action="take", ref=ref).json()
        self.assertTrue(data["ok"], data)
        self.assertNotIn(ref, [r["ref"] for r in agenda.suggest(self.owner, m)])        # уже в агенді
        left = len(agenda.suggest(self.owner, m))
        self.send(m, action="take_all")
        self.assertEqual(MeetingAgendaItem.objects.filter(meeting=m).count(), left + 1)
        self.assertEqual(agenda.suggest(self.owner, m), [])
        self.assertFalse(self.send(m, action="take", ref=ref).json()["ok"])             # двічі не додається

    def test_not_discussed_moves_to_the_next_meeting(self):
        first = self.meeting()
        old = agenda.add(self.owner, first, "Ціна серії")
        agenda.add(self.owner, first, "Обговорили", )
        agenda.set_status(self.owner, first, MeetingAgendaItem.objects.get(text="Обговорили").pk, "done")
        second = self.meeting()
        carry = [r for r in agenda.suggest(self.owner, second) if r["group"] == "carry"]
        self.assertEqual([r["text"] for r in carry], ["Ціна серії"])
        new = agenda.add(self.owner, second, carry[0]["text"], ref=carry[0]["ref"])
        old.refresh_from_db()
        self.assertEqual((old.status, old.moved_to_id), (MeetingAgendaItem.MOVED, new.pk))
        self.assertEqual(agenda.progress(first)["total"], 1)                             # перенесене не рахується
        agenda.remove(self.owner, second, new.pk)                                        # передумали
        old.refresh_from_db()
        self.assertEqual((old.status, old.moved_to_id), (MeetingAgendaItem.OPEN, None))
        other_user_meeting = self.meeting(self.other)                                    # чужі наради — ні
        self.assertFalse([r for r in agenda.suggest(self.other, other_user_meeting) if r["group"] == "carry"])


class CockpitAgendaTests(AgendaBase):
    def test_counts_and_upcoming_meeting_from_calendar(self):
        from projects import cockpit, terms
        m = self.meeting()
        a = agenda.add(self.owner, m, "Строк зразків")
        agenda.add(self.owner, m, "Ціна серії")
        agenda.set_status(self.owner, m, a.pk, "done")
        self.assertEqual(agenda.counts([m.pk]), {m.pk: (1, 2)})
        ev = CalendarEvent.objects.create(user=self.owner, title="Progress-call", event_type=CalendarEvent.TYPE_MEETING,
                                          start_at=timezone.now() + timedelta(days=2))
        facts = {"terms": [terms.Term(event=ev, meeting=True, state=terms.SOON)]}
        up = agenda.upcoming(self.project, self.owner, facts)
        self.assertEqual(len(up), 1)
        self.assertFalse(up[0]["prepared"])
        self.assertIn(f"event={ev.pk}", up[0]["url"])
        self.assertEqual(agenda.upcoming(self.project, self.other, facts), [])           # чужий календар — ні
        Meeting.objects.filter(pk=m.pk).update(calendar_event=ev)
        self.assertTrue(agenda.upcoming(self.project, self.owner, facts)[0]["prepared"])
        Meeting.objects.filter(pk=m.pk).update(calendar_event=None)
        from meetings.models import Meeting as M
        ctx = cockpit._meetings_context(self.project, self.owner, list(M.objects.filter(pk=m.pk)), facts)
        self.assertEqual((ctx["rows"][0]["agenda_done"], ctx["rows"][0]["agenda_total"]), (1, 2))
        self.assertEqual(len(ctx["upcoming"]), 1)
        self.login(self.owner)
        html = self.client.get(f"/admin/projects/project/{self.project.pk}/change/").content.decode()
        self.assertIn("📋 агенда 1 / 2", html)


class SubItemsTests(AgendaBase):
    def test_sub_items_numbering_status_order_and_copy(self):
        m = self.meeting()
        self.login(self.owner)
        top = agenda.add(self.owner, m, "Демонстрація біконів")
        agenda.add(self.owner, m, "Ціна серії")
        data = self.send(m, action="add", text="Точність у приміщенні", parent=top.pk).json()
        self.assertTrue(data["ok"], data)
        self.assertIn('data-ag-subadd="%d"' % top.pk, data["html"])                 # поле «＋ Підпункт» є
        self.assertIn("＋ Підпункт", data["html"])
        self.send(m, action="add", text="Час роботи батареї", parent=top.pk)
        a, b = MeetingAgendaItem.objects.filter(parent=top).order_by("order")
        self.assertEqual([(r.num, r.text) for r in agenda.items(m)],
                         [("1", "Демонстрація біконів"), ("1.1", "Точність у приміщенні"),
                          ("1.2", "Час роботи батареї"), ("2", "Ціна серії")])
        self.assertFalse(self.send(m, action="add", text="x", parent=a.pk).json()["ok"])  # лише один рівень
        self.send(m, action="move", item=b.pk, delta=-1)                              # серед підпунктів пункту
        self.assertEqual([r.text for r in agenda.items(m)][1:3], ["Час роботи батареї", "Точність у приміщенні"])
        agenda.set_status(self.owner, m, a.pk, "done")
        top.refresh_from_db()
        self.assertEqual(top.status, "open")
        agenda.set_status(self.owner, m, b.pk, "skipped")
        top.refresh_from_db()
        self.assertEqual(top.status, "done")                                          # усі ☑ / ⏭ — пункт ☑
        agenda.set_status(self.owner, m, a.pk, "open")
        top.refresh_from_db()
        self.assertEqual(top.status, "open")
        agenda.set_status(self.owner, m, top.pk, "done")                              # пункт ☑ — відкриті підпункти теж
        a.refresh_from_db()
        self.assertEqual(a.status, "done")
        self.assertEqual(agenda.progress(m), {"total": 2, "done": 1, "open": 1})      # лише пункти
        text = agenda.copy_text(m)
        self.assertIn("1. Демонстрація біконів\n   1.1. Час роботи батареї\n   1.2. Точність у приміщенні\n2. Ціна серії",
                      text)
        self.send(m, action="remove", item=top.pk)
        self.assertEqual(self.texts(m), ["Ціна серії"])                               # разом з підпунктами

    def test_carry_takes_open_sub_items_along(self):
        first = self.meeting()
        top = agenda.add(self.owner, first, "Демонстрація біконів")
        agenda.add(self.owner, first, "Точність", parent=top.pk)
        done = agenda.add(self.owner, first, "Батарея", parent=top.pk)
        agenda.set_status(self.owner, first, done.pk, "done")
        second = self.meeting()
        carry = next(r for r in agenda.suggest(self.owner, second) if r["group"] == "carry")
        self.assertIn("підпунктів: 1", carry["why"])
        new = agenda.add(self.owner, second, carry["text"], ref=carry["ref"])
        self.assertEqual(list(new.kids.values_list("text", flat=True)), ["Точність"])


class SendAgendaTests(AgendaBase):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        EmailAccount.objects.create(user=self.owner, email_address="slava@sevskiy.example")

    def test_draft_in_own_mailbox_without_ai(self):
        from email_assistant.models import EmailDraft
        m = self.meeting(participants=[{"name": "Max Muster", "email": "max@client.example", "source": "manual"},
                                       {"name": "Slava", "email": "slava@sevskiy.example", "source": "manual"}])
        top = agenda.add(self.owner, m, "Demo der Beacons")
        agenda.add(self.owner, m, "Genauigkeit", parent=top.pk)
        self.assertEqual([p["email"] for p in agenda.people(m, self.owner)], ["max@client.example"])  # без своїх
        self.login(self.owner)
        with patch("email_assistant.ai_helper._call_ai_direct") as ai:
            data = self.send(m, action="send", to=["max@client.example"], extra="kollege@sevskiy.example; ",
                             lang="de", ai=0).json()
        ai.assert_not_called()
        self.assertTrue(data["ok"], data)
        draft = EmailDraft.objects.get()
        self.assertEqual(data["compose"], f"/email/compose/?draft={draft.pk}")
        self.assertEqual(draft.to_emails, ["max@client.example", "kollege@sevskiy.example"])
        self.assertEqual(draft.subject, f"Tagesordnung: {m.title}")
        self.assertIn("Guten Tag,", draft.body)
        self.assertIn("1. Demo der Beacons\n    1.1. Genauigkeit", draft.body)   # відступ — і в редакторі
        self.assertFalse(self.send(m, action="send", to=["not-an-address"], lang="de").json()["ok"])

    def test_ai_translation_and_bad_answer(self):
        from email_assistant.models import EmailDraft
        m = self.meeting()
        top = agenda.add(self.owner, m, "Демонстрація біконів")
        agenda.add(self.owner, m, "Точність", parent=top.pk)
        agenda.edit(self.owner, m, top.pk, note="Зразки до 25.11")
        answer = json.dumps([f"{m.title} EN", "Beacon demo", "Accuracy", "Samples by 25.11"])
        with patch("email_assistant.ai_helper._call_ai_direct", return_value=f"Here: {answer}") as ai:
            url = agenda.send(self.owner, m, [], "en", ai=True)
        self.assertIn('"Демонстрація біконів"', ai.call_args.args[0])                   # тексти — даними, JSON
        draft = EmailDraft.objects.get(pk=int(url.rsplit("=", 1)[1]))
        self.assertEqual(draft.subject, f"Agenda: {m.title} EN")
        self.assertIn("1. Beacon demo — Samples by 25.11\n    1.1. Accuracy", draft.body)
        with patch("email_assistant.ai_helper._call_ai_direct", return_value="sorry"):
            with self.assertRaises(Exception):
                agenda.send(self.owner, m, [], "en", ai=True)

    def test_without_mailbox(self):
        m = self.meeting(self.other)
        agenda.add(self.other, m, "Пункт")
        with self.assertRaises(Exception) as e:
            agenda.send(self.other, m, [], "uk")
        self.assertIn("немає вашої поштової скриньки", str(e.exception))
