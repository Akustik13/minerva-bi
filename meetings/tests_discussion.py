"""💬 «Лише обговорення» — пункт наради, що стосується розмови, але не є моментом, ризиком, кроком чи дедлайном
(meetings/services.accept / restore / accepted_items / drop_unused_items; _item.html, page.html, meetings.js):
python manage.py test meetings.tests_discussion --settings=tabele.settings_test

Нічого не створюється ні в проєкті, ні в календарі; можна завжди (і без проєкту, і без права змінювати проєкт);
назва й проєкт пункту запам'ятовуються в пункті; ↺ Повернути — знову чекає; у вікні «🗑 Видалити нараду» таких
пунктів немає; разом з розшифровкою (строк зберігання, запис заново) вони зникають.
"""
import json

from django.utils import timezone

from calendar_app.models import CalendarEvent
from meetings import admin_pages, parts, services
from meetings.models import Meeting, MeetingItem
from meetings.tests_pages import PagesBase, staff
from projects.models import Project, ProjectHighlight, ProjectRisk, ProjectStep


class DiscussionTests(PagesBase):
    def item(self, m, kind):
        return m.items.get(kind=kind)

    def post(self, m, item, body):
        return self.client.post(self.url("item", m, item.pk), json.dumps(body), content_type="application/json")

    def counts(self):
        return (ProjectHighlight.objects.count(), ProjectRisk.objects.count(), ProjectStep.objects.count(),
                CalendarEvent.objects.filter(event_type=CalendarEvent.TYPE_DEADLINE).count())

    def test_one_click_note_creates_nothing(self):
        m = self.processed()
        self.login(self.owner)
        before = self.counts()
        item = self.item(m, "decision")
        data = self.post(m, item, {"action": "accept", "target": "note"}).json()
        self.assertTrue(data["ok"], data)
        self.assertIn("💬 Лише обговорення — лишається в цій нараді", data["html"])
        self.assertIn('data-mt-act="restore"', data["html"])
        self.assertEqual(data["pending"], 1)
        item.refresh_from_db()
        self.assertEqual((item.status, item.target), (MeetingItem.ACCEPTED, MeetingItem.NOTE))
        self.assertIsNone(item.highlight_id)
        self.assertIsNone(item.risk_id)
        self.assertEqual(self.counts(), before)

    def test_title_and_project_are_kept_in_the_item(self):
        m = self.processed()
        other = Project.objects.create(name="Filter", customer=self.customer, owner=self.owner)
        parts.add_project(self.owner, m, other.pk)                              # нарада про два проєкти
        self.login(self.owner)
        item = self.item(m, "task")
        data = self.post(m, item, {"action": "accept", "target": "note", "title": "  Тема:   креслення ",
                                   "project": other.pk}).json()
        self.assertTrue(data["ok"], data)
        item.refresh_from_db()
        self.assertEqual(item.title, "Тема: креслення")
        self.assertEqual(item.project_id, other.pk)                             # 📁 до якого проєкту теза
        self.assertIn(other.code, data["html"])

    def test_restore_brings_it_back(self):
        m = self.processed()
        self.login(self.owner)
        item = self.item(m, "decision")
        self.post(m, item, {"action": "accept", "target": "note"})
        data = self.post(m, item, {"action": "restore"}).json()
        self.assertTrue(data["ok"], data)
        item.refresh_from_db()
        self.assertEqual((item.status, item.target, item.decided_at), (MeetingItem.PENDING, "", None))
        self.assertIn("💬 Лише обговорення</button>", data["html"])
        # an item really added to the project is not «restored» this way
        self.post(m, item, {"action": "accept", "target": "moment"})
        data = self.post(m, item, {"action": "restore"}).json()
        self.assertFalse(data["ok"])
        self.assertEqual(self.item(m, "decision").target, "moment")

    def test_allowed_without_project_and_without_project_right(self):
        m = self.processed()
        Meeting.objects.filter(pk=m.pk).update(project=None)
        m = Meeting.objects.get(pk=m.pk)
        item = self.item(m, "decision")
        self.assertEqual(services.targets(self.owner, m, item, project=None)["targets"]["note"], "")
        viewer = staff("pm", module_operations={"meetings": ["view", "add", "change"]})
        self.assertEqual(services.targets(viewer, m, item, project=self.project)["targets"]["note"], "")
        services.accept(self.owner, item, "note")
        self.assertEqual(self.item(m, "decision").target, MeetingItem.NOTE)

    def test_page_buttons_group_and_delete_dialog(self):
        m = self.processed()
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, 'data-mt-act="note"', count=2)                 # кожен пункт, що чекає
        self.assertContains(r, "💬 Лише обговорення</b>")                       # і в «Куди додати»
        decision, task = self.item(m, "decision"), self.item(m, "task")
        services.accept(self.owner, decision, "note")
        services.accept(self.owner, task, "moment")
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "💬 Лише обговорення (1)</summary>")
        dialog = r.content.decode().split('id="mt-del-h"', 1)[1]
        self.assertIn("Уже додано (1)", dialog)
        self.assertEqual([i.pk for i in services.accepted_items(m)], [task.pk])
        self.assertEqual([row["item"].pk for row in admin_pages.accepted_rows(m)], [task.pk])

    def test_notes_go_with_the_transcript(self):
        m = self.processed()
        decision, task = self.item(m, "decision"), self.item(m, "task")
        services.accept(self.owner, decision, "note")
        services.accept(self.owner, task, "moment")
        services.drop_transcript(Meeting.objects.get(pk=m.pk), now=timezone.now())
        left = list(m.items.values_list("pk", "title"))
        self.assertEqual(left, [(task.pk, "")])                                 # момент у проєкті — без назви

    def test_record_again_drops_notes(self):
        m = self.processed()
        services.accept(self.owner, self.item(m, "decision"), "note")
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.FAILED)
        services.issue_ticket(Meeting.objects.get(pk=m.pk), self.owner)
        self.assertFalse(m.items.filter(target=MeetingItem.NOTE).exists())
