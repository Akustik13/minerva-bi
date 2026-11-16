"""🎙 Нарада про кілька проєктів (meetings/parts.py): python manage.py test meetings.tests_projects --settings=tabele.settings_test

Нараду без проєкту підключають до проєкту й після запису («📁 Проєкти наради»), проєктів може бути кілька
(⭐ головний), 🏷 частини запису «звідси — про проєкт …», проєкт кожного ключового моменту (✓ іде в нього), частини
бази знань — у проєкт своєї частини, нарада видна в кожному своєму проєкті (лише власнику) з посиланням на її частину,
💡 підказки без AI, права й чужі наради.
"""
import json
from datetime import datetime, timezone as dt_tz

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import Customer
from meetings import admin_pages, knowledge, notify, parts, services
from meetings.models import Meeting, MeetingItem, MeetingPart, MeetingProject, MeetingSettings, MeetingTranscript
from meetings.tests_pages import PagesBase, staff
from projects import timeline
from projects.models import Project, ProjectHighlight
from rag_assistant.models import KnowledgeItem
from rag_assistant.tests_knowledge import KnowledgeBase

SEGMENTS = [
    {"n": 1, "start": 1.0, "end": 5.0, "ch": "L", "text": "Fangen wir mit den Antennen an."},
    {"n": 2, "start": 20.0, "end": 30.0, "ch": "R", "text": "Die Antennen kommen bis Freitag."},
    {"n": 3, "start": 300.0, "end": 310.0, "ch": "L", "text": "Jetzt zum Bahnfilter, das zweite Thema."},
    {"n": 4, "start": 320.0, "end": 330.0, "ch": "R", "text": "Der Bahnfilter braucht noch eine Zeichnung."},
]


class ProjectsBase(PagesBase):
    def setUp(self):
        super().setUp()
        self.rail = Customer.objects.create(name="Bahn", company="Rail AG", email="rail@bahn.example")
        self.second = Project.objects.create(name="Bahnfilter", customer=self.rail, owner=self.owner)

    def recorded(self, project=True, items=None):
        """Нарада з розшифровкою (як після RAG-ПК) і пунктами в 25-й і 325-й секундах."""
        data = {"title": "Zwei Themen", "participants": []}
        if project:
            data["project"] = self.project.pk
        m = services.create(self.owner, data)
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.REVIEW, transcript_at=timezone.now(),
                                               duration_s=400.0, capture_mode="both",
                                               started_at=datetime(2026, 11, 3, 9, 0, tzinfo=dt_tz.utc))
        MeetingTranscript.objects.create(meeting=m, segments=SEGMENTS, at=timezone.now())
        for n, (second, kind) in enumerate(items or [(25.0, "decision"), (325.0, "task")], 1):
            MeetingItem.objects.create(meeting=m, key=f"k{n}", kind=kind, title=f"Пункт {n}", quote=f"Zitat {n}",
                                       start_s=second, ch="R")
        return Meeting.objects.get(pk=m.pk)

    def post_json(self, name, m, body):
        return self.client.post(self.url(name, m), json.dumps(body), content_type="application/json")


class AttachProjectTests(ProjectsBase):
    def test_main_project_is_always_linked(self):
        m = self.meeting()
        self.assertEqual(list(m.project_links.values_list("project_id", flat=True)), [self.project.pk])
        services.edit(self.owner, m, {"project": self.second.pk})
        self.assertEqual(set(Meeting.objects.get(pk=m.pk).project_links.values_list("project_id", flat=True)),
                         {self.project.pk, self.second.pk})

    def test_meeting_without_project_attached_after_recording(self):
        m = self.recorded(project=False)
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "📁 Підключити до проєкту")
        self.assertContains(r, "Нарада ще не в проєкті")
        self.assertEqual(timeline.own_meetings(self.project, self.owner), [])
        r = self.post_json("projects", m, {"action": "add", "project": self.project.pk})
        self.assertEqual(r.json(), {"ok": True})
        m.refresh_from_db()
        self.assertEqual((m.project, m.customer), (self.project, self.customer))   # клієнт — з проєкту
        self.assertEqual([x.pk for x in timeline.own_meetings(self.project, self.owner)], [m.pk])
        self.assertEqual(timeline.own_meetings(self.project, self.other), [])        # чужа нарада — нікому
        r = self.client.get(self.url("page", m))
        self.assertContains(r, f'{self.project.code} · {self.project.name}')
        self.assertNotContains(r, "Нарада ще не в проєкті")

    def test_several_projects_main_and_remove(self):
        m = self.recorded()
        self.login(self.owner)
        self.post_json("projects", m, {"action": "add", "project": self.second.pk})
        self.assertEqual([p.pk for p in parts.links(m)], [self.project.pk, self.second.pk])
        self.assertEqual([x.pk for x in timeline.own_meetings(self.second, self.owner)], [m.pk])
        r = self.client.get(reverse("admin:meetings_meeting_changelist"))
        self.assertContains(r, f"{self.project.code}, {self.second.code}")
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "Проєкти наради:")
        self.post_json("projects", m, {"action": "main", "project": self.second.pk})
        m.refresh_from_db()
        self.assertEqual(m.project, self.second)
        self.assertEqual([p.pk for p in parts.links(m)], [self.second.pk, self.project.pk])
        # ✕ головний — головним стає інший, його позначки й вибір пунктів прибираються
        parts.set_part(self.owner, m, 300, self.second.pk)
        item = m.items.get(key="k2")
        item.project = self.second
        item.save()
        self.post_json("projects", m, {"action": "remove", "project": self.second.pk})
        m.refresh_from_db()
        self.assertEqual(m.project, self.project)
        self.assertFalse(MeetingPart.objects.filter(meeting=m).exists())
        self.assertIsNone(MeetingItem.objects.get(pk=item.pk).project_id)
        self.post_json("projects", m, {"action": "remove", "project": self.project.pk})
        m.refresh_from_db()
        self.assertIsNone(m.project_id)
        self.assertFalse(MeetingProject.objects.filter(meeting=m).exists())
        r = self.post_json("projects", m, {"action": "remove", "project": self.project.pk})
        self.assertEqual(r.status_code, 400)

    def test_rights_and_foreign_meeting(self):
        m = self.recorded()
        self.login(self.other)
        self.assertEqual(self.post_json("projects", m, {"action": "add", "project": self.second.pk}).status_code, 404)
        self.assertEqual(self.post_json("part", m, {"action": "set", "second": 10}).status_code, 404)
        self.login(self.owner)
        self.assertEqual(self.client.get(self.url("projects", m)).status_code, 405)
        self.assertEqual(self.post_json("projects", m, {"action": "nope"}).status_code, 400)
        viewer = staff("viewer", module_operations={"meetings": ["view", "add"]})
        mine = services.create(viewer, {"title": "Без права змінювати"})
        self.login(viewer)
        r = self.post_json("projects", mine, {"action": "add", "project": self.project.pk})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(MeetingProject.objects.filter(meeting=mine).exists())


class PartsTests(ProjectsBase):
    def test_part_marks_items_and_accept(self):
        m = self.recorded()
        self.login(self.owner)
        r = self.post_json("part", m, {"action": "set", "second": 300, "project": self.second.pk, "note": "Filter"})
        self.assertEqual(r.json(), {"ok": True})
        self.assertTrue(MeetingProject.objects.filter(meeting=m, project=self.second).exists())   # додано сам
        marks = parts.parts_of(m)
        spans = parts.ranges(m, marks)
        self.assertEqual([(s["start"], s["end"], s["project"].pk) for s in spans],
                         [(0.0, 300.0, self.project.pk), (300.0, None, self.second.pk)])
        first, second = m.items.order_by("start_s")
        self.assertEqual(parts.item_project(m, first), self.project)
        self.assertEqual(parts.item_project(m, second), self.second)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "🏷 Далі —")
        self.assertContains(r, "mt-partrow mt-pj-c1")
        self.assertContains(r, 'class="mt-row mt-seg mt-pj-c0"')
        self.assertContains(r, 'class="mt-row mt-seg mt-pj-c1"')
        # ✓ без вибору — у проєкт частини; з вибором — у вибраний
        url = self.url("item", m, second.pk)
        r = self.client.post(url, json.dumps({"action": "accept", "target": "moment"}), content_type="application/json")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(ProjectHighlight.objects.get(meeting=m).project, self.second)
        r = self.client.post(self.url("item", m, first.pk), json.dumps(
            {"action": "accept", "target": "moment", "project": self.second.pk}), content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(MeetingItem.objects.get(pk=first.pk).project, self.second)
        # «загальне» — головний проєкт
        parts.set_part(self.owner, m, 10, None)
        self.assertEqual(parts.project_at(parts.parts_of(m), 20, m.project)[0], None)
        self.assertEqual(parts.ranges(m)[1]["general"], True)
        # повтор на тій самій секунді — заміна, не дубль; ✕ прибирає
        parts.set_part(self.owner, m, 300, self.project.pk)
        self.assertEqual(MeetingPart.objects.filter(meeting=m).count(), 2)
        part = MeetingPart.objects.get(meeting=m, start_s=300)
        self.assertEqual(part.project, self.project)
        self.assertEqual(self.post_json("part", m, {"action": "remove", "part": part.pk}).json(), {"ok": True})
        self.assertEqual(self.post_json("part", m, {"action": "remove", "part": part.pk}).status_code, 400)
        self.assertEqual(self.post_json("part", m, {"action": "set", "second": 9999}).status_code, 400)  # поза записом

    def test_accept_into_project_not_of_meeting_refused(self):
        m = self.recorded()
        other = Project.objects.create(name="Чужий", owner=self.owner)
        self.login(self.owner)
        item = m.items.first()
        r = self.client.post(self.url("item", m, item.pk), json.dumps(
            {"action": "accept", "target": "moment", "project": other.pk}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("немає серед проєктів наради", r.json()["error"])
        self.assertFalse(ProjectHighlight.objects.filter(project=other).exists())

    def test_item_panel_offers_meeting_projects(self):
        m = self.recorded()
        parts.add_project(self.owner, m, self.second.pk)
        ctx = admin_pages.item_context(self.owner, m, m.items.order_by("start_s").first())
        self.assertEqual([c["pk"] for c in ctx["pj_choices"]], [self.project.pk, self.second.pk])
        self.assertTrue(all(c["noplan"] for c in ctx["pj_choices"]))          # плану ще немає — кроку не буде
        self.assertTrue(ctx["pj_choices"][0]["selected"])

    def test_project_sees_its_part(self):
        m = self.recorded()
        self.assertEqual(timeline.own_meetings(self.project, self.owner)[0].pending, 2)   # один проєкт — усі
        parts.set_part(self.owner, m, "300,0", self.second.pk)                            # кома — як у сторінці
        mine = timeline.own_meetings(self.second, self.owner)
        self.assertEqual(mine[0].part_second, 300)
        self.assertEqual((mine[0].pending, timeline.own_meetings(self.project, self.owner)[0].pending), (1, 1))
        self.assertIn("🏷 частина 00:05:00–кінець", mine[0].part_text)
        self.assertIn("🏷 частина 00:00:00–00:05:00", timeline.own_meetings(self.project, self.owner)[0].part_text)
        self.assertIn("#t=300", timeline.meeting_url(m.pk, mine[0].part_second))
        self.assertIn("🏷", timeline.meeting_line(mine[0]))

    def test_suggestions_without_ai(self):
        m = self.recorded()
        parts.add_project(self.owner, m, self.second.pk)
        found = parts.suggest(m, SEGMENTS)
        self.assertEqual([(s["second"], s["project"].pk, s["word"]) for s in found], [(300.0, self.second.pk, "bahnfilter")])
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "💡 Схоже, тут мова про інший проєкт")
        parts.set_part(self.owner, m, 300, self.second.pk)
        self.assertEqual(parts.suggest(m, SEGMENTS), [])                       # позначено — підказки немає
        self.assertEqual(parts.suggest(self.recorded(), SEGMENTS), [])         # один проєкт — нічого розрізняти

    def test_hotwords_from_every_project(self):
        m = self.recorded()
        parts.add_project(self.owner, m, self.second.pk)
        words = services.hotwords(m)
        self.assertIn(self.project.code, words)
        self.assertIn(self.second.code, words)


@override_settings(MINERVA_INSTANCE_ID="test-suite")
class KnowledgePartsTests(KnowledgeBase):
    def setUp(self):
        super().setUp()
        notify.BACKGROUND = False
        self.addCleanup(setattr, notify, "BACKGROUND", True)
        s = MeetingSettings.load()
        s.enabled = True
        s.save()
        self.project = Project.objects.create(name="Antenne", customer=self.client_co, owner=self.user)
        self.rail = Customer.objects.create(name="Bahn", company="Rail AG", email="rail@bahn.example")
        self.second = Project.objects.create(name="Bahnfilter", customer=self.rail, owner=self.user)

    def test_each_part_goes_to_its_project(self):
        m = services.create(self.user, {"title": "Zwei Themen", "project": self.project.pk})
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.REVIEW, transcript_at=timezone.now(),
                                               started_at=datetime(2026, 11, 3, 9, 0, tzinfo=dt_tz.utc))
        MeetingTranscript.objects.create(meeting=m, segments=SEGMENTS, at=timezone.now())
        m = Meeting.objects.get(pk=m.pk)
        parts.set_part(self.user, m, 300, self.second.pk)
        pks = knowledge.publish(m)
        items = list(KnowledgeItem.objects.filter(pk__in=pks).order_by("source_key"))
        self.assertEqual([(i.project, i.customer) for i in items],
                         [(self.project, self.client_co), (self.second, self.rail)])
        self.assertIn("Die Antennen kommen", items[0].text)
        self.assertNotIn("Bahnfilter", items[0].text)
        self.assertIn(f"Ця частина — про проєкт {self.second.code}", items[1].text)
        self.assertEqual(items[1].title, f"🎙 Нарада {self.second.code} · 03.11.2026 · 2/2")
        # позначку прибрали на сторінці — база знань оновилась сама: одна частина з головним проєктом
        self.client.force_login(self.user)
        part = MeetingPart.objects.get(meeting=m)
        r = self.client.post(reverse("admin:meetings_meeting_part", args=[m.pk]),
                             json.dumps({"action": "remove", "part": part.pk}), content_type="application/json")
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(list(KnowledgeItem.objects.filter(source_key__startswith=f"meeting:{m.pk}:")
                              .values_list("project_id", flat=True)), [self.project.pk])
