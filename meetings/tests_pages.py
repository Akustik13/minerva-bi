"""🎙 Наради — сторінки (meetings/admin.py, admin_pages.py): python manage.py test meetings --settings=tabele.settings_test

Лише власник (і суперкористувач — ні), ролі з module_operations {'meetings': ['view', 'add', 'change']} доходять до
сторінки й видалення (Layer 4), запис → 302 на вікно запису з #minerva=, опитування, ✓ / ✕ / ↺, база знань, «✏️
Змінити» перевіряє доступ ще раз, видалення, «🧾 Протоколи згоди» (адміністратор, лише метадані), ключ для RAG-ПК,
відмова, поки записування вимкнено, translate="no" довкола розшифровки й тексту згоди.
"""
import json
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api.models import APIKey
from calendar_app.models import CalendarEvent
from core.models import AuditLog
from crm.models import Customer
from meetings import admin_pages, notify, services
from meetings.models import Meeting, MeetingItem, MeetingSettings
from projects.models import Project, ProjectHighlight

SEGMENTS = [
    {"n": 1, "start": 0.5, "end": 4.0, "ch": "L", "text": "Bevor wir anfangen: sind alle einverstanden mit der Aufnahme?"},
    {"n": 2, "start": 4.5, "end": 6.0, "ch": "R", "text": "Ja, passt."},
    {"n": 3, "start": 120.0, "end": 130.0, "ch": "R", "text": "Wir liefern die Antennen bis Freitag nach Köln."},
    {"n": 4, "start": 131.0, "end": 140.0, "ch": "L", "text": "Ich schicke die Zeichnung bis Montag."},
]
ITEMS = [
    {"kind": "decision", "text": "Поставка до п’ятниці", "quote": "liefern die Antennen bis Freitag", "line": 3},
    {"kind": "task", "text": "Надіслати креслення", "due": "14.11.2026", "quote": "schicke die Zeichnung bis Montag",
     "line": 4},
]
TITLE = "Geheime Abstimmung Antenne"


def staff(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@sevskiy.example", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class PagesBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        notify.BACKGROUND = False
        self.addCleanup(setattr, notify, "BACKGROUND", True)
        deliver = patch("agent.notify.deliver", return_value="🔔")
        deliver.start()
        self.addCleanup(deliver.stop)
        s = MeetingSettings.load()
        s.enabled = True
        s.save()
        self.owner = User.objects.create_superuser("rec", "owner@sevskiy.example", "p", first_name="Slava")
        self.other = User.objects.create_superuser("boss", "boss@sevskiy.example", "p")
        self.customer = Customer.objects.create(name="Max Muster", company="Client GmbH", email="max@client.example")
        self.project = Project.objects.create(name="Antenne 5G", customer=self.customer, owner=self.owner)

    # ── помічники ──
    def meeting(self, user=None, **data):
        base = {"title": TITLE, "project": self.project.pk,
                "participants": [{"name": "Max Muster", "email": "max@client.example", "source": "manual"}]}
        base.update(data)
        return services.create(user or self.owner, base)

    def processed(self, items=ITEMS, user=None):
        """Запис зі згодою → розшифровка → ключові моменти (як із RAG-ПК)."""
        user = user or self.owner
        m = self.meeting(user)
        token = services.redeem(services.issue_ticket(m, user))["push_token"]
        m = Meeting.objects.get(pk=m.pk)
        common = {"session": "s1", "uid": str(m.uid)}
        consent = {"announced": True, "all_agreed": True, "lang": "de", "at": timezone.now().isoformat()}
        services.push_status(m.pk, token, {**common, "state": "recording", "seq": 1, "consent": consent,
                                           "at": timezone.now().isoformat(), "capture": {"mode": "both"}})
        services.push_status(m.pk, token, {**common, "state": "recorded", "seq": 2, "duration": 150.0,
                                           "at": timezone.now().isoformat(), "marks": [{"t": 2.0, "note": "consent_ok"}]})
        services.push_transcript(m.pk, token, {**common, "upload_id": "u1", "parts": 1, "part": 1,
                                               "segments": SEGMENTS})
        if items is not None:
            services.push_moments(m.pk, token, {**common, "model": "qwen", "summary": "Kurz: Lieferung bis Freitag.",
                                                "items": items})
        return Meeting.objects.get(pk=m.pk)

    def url(self, name, m, *extra):
        return reverse(f"admin:meetings_meeting_{name}", args=[m.pk, *extra])

    def login(self, user):
        self.client.force_login(user)


class UrlTests(TestCase):
    def test_names_and_no_layer4_suffix(self):
        names = {"new": [], "protocols": [], "page": [1], "record": [1], "status": [1], "item": [1, 2], "kb": [1],
                 "save": [1], "retry": [1], "continue": [1], "remove": [1]}
        for name, args in names.items():
            path = reverse(f"admin:meetings_meeting_{name}", args=args)
            self.assertTrue(path.startswith("/admin/meetings/meeting/"), path)
            self.assertNotIn(path.rstrip("/").rsplit("/", 1)[-1], ("add", "change", "delete"), path)
        self.assertEqual(reverse("admin:meetings_meeting_page", args=[7]), "/admin/meetings/meeting/7/page/")
        self.assertEqual(reverse("admin:meetings_meeting_new"), "/admin/meetings/meeting/new/")
        self.assertEqual(reverse("admin:meetings_meetingsettings_key"), "/admin/meetings/meetingsettings/key/")


class OwnerOnlyTests(PagesBase):
    def test_owner_sees_page_list_and_status(self):
        m = self.meeting()
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'<h1 translate="no">{TITLE}</h1>', html=False)
        r = self.client.get(reverse("admin:meetings_meeting_changelist"))
        self.assertContains(r, TITLE)
        self.assertContains(r, "🎙 Записати нараду")
        self.assertNotContains(r, "Додати нарада")
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_change", args=[m.pk]))["Location"],
                         self.url("page", m))
        self.assertTrue(self.client.get(reverse("admin:meetings_meeting_add") + "?project=1")["Location"].startswith(
            reverse("admin:meetings_meeting_new")))

    def test_other_superuser_gets_404_everywhere(self):
        m = self.processed()
        item = m.items.first()
        self.login(self.other)
        for name, method, extra in (("page", "get", ()), ("status", "get", ()), ("continue", "get", ()),
                                    ("record", "post", ()), ("item", "post", (item.pk,)), ("kb", "post", ()),
                                    ("save", "post", ()), ("retry", "post", ()), ("remove", "post", ())):
            r = getattr(self.client, method)(self.url(name, m, *extra))
            self.assertEqual(r.status_code, 404, name)
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_change", args=[m.pk])).status_code, 404)
        r = self.client.get(reverse("admin:meetings_meeting_changelist"))
        self.assertNotContains(r, TITLE)
        self.assertTrue(Meeting.objects.filter(pk=m.pk).exists())
        self.assertEqual(MeetingItem.objects.get(pk=item.pk).status, MeetingItem.PENDING)

    def test_changes_only_by_post(self):
        m = self.processed()
        self.login(self.owner)
        for name, extra in (("record", ()), ("item", (m.items.first().pk,)), ("kb", ()), ("save", ()),
                            ("retry", ()), ("remove", ())):
            r = self.client.get(self.url(name, m, *extra))
            self.assertEqual(r.status_code, 405, name)
        self.assertTrue(Meeting.objects.filter(pk=m.pk).exists())

    def test_no_delete_permission_or_actions(self):
        m = self.meeting()
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_delete", args=[m.pk])).status_code, 403)
        r = self.client.get(reverse("admin:meetings_meeting_changelist"))
        self.assertNotContains(r, 'name="action"')


class ModuleOperationsTests(PagesBase):
    """«✏️ Змінити доступ» пише module_operations: view / add / change — Layer 4 не блокує сторінку й видалення."""

    def test_manager_with_explicit_operations(self):
        user = staff("mgr", module_operations={"meetings": ["view", "add", "change"]})
        self.assertTrue(services.can_record(user))
        self.login(user)
        r = self.client.post(reverse("admin:meetings_meeting_new"), {"title": "Нарада менеджера", "retention":
                                                                     "text_only", "consent_lang": "de"})
        m = Meeting.objects.get(owner=user)
        self.assertRedirects(r, self.url("page", m), fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.url("page", m)).status_code, 200)
        self.assertEqual(self.client.get(self.url("status", m)).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_changelist")).status_code, 200)
        r = self.client.post(self.url("remove", m))
        self.assertRedirects(r, reverse("admin:meetings_meeting_changelist"), fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.filter(pk=m.pk).exists())


class PrepareTests(PagesBase):
    def test_get_prefills_from_project(self):
        self.login(self.owner)
        r = self.client.get(reverse("admin:meetings_meeting_new") + f"?project={self.project.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f"Нарада {self.project.code}")
        self.assertContains(r, "Згоду на запис підтвердите у вікні запису перед ▶")
        self.assertContains(r, 'class="mt-say" translate="no"')
        self.assertNotContains(r, "consent_announced")

    def test_post_creates_with_ticked_people_only(self):
        self.login(self.owner)
        r = self.client.post(reverse("admin:meetings_meeting_new"), {
            "title": "  Abstimmung   Woche 46 ", "project": self.project.pk, "lang_l": "uk", "lang_r": "de",
            "retention": "audio_text", "consent_lang": "en",
            "p_name": ["Max Muster", "Eva Kunde"], "p_email": ["max@client.example", "eva@client.example"],
            "p_source": ["invite", "project"], "p_on": ["0"], "x_name": ["Gast"], "x_email": [""],
            "consent_note": "Gast hört zu"})
        m = Meeting.objects.get(owner=self.owner)
        self.assertRedirects(r, self.url("page", m), fetch_redirect_response=False)
        self.assertEqual((m.title, m.project, m.customer, m.lang_l, m.lang_r, m.retention, m.consent_lang),
                         ("Abstimmung Woche 46", self.project, self.customer, "uk", "de", "audio_text", "en"))
        self.assertEqual([p["name"] for p in m.participants], ["Max Muster", "Gast"])
        self.assertEqual(m.status, Meeting.READY)
        self.assertIsNone(m.consent_at)

    def test_other_users_event_is_refused(self):
        start = timezone.now()
        ev = CalendarEvent.objects.create(user=self.other, title="Fremd", start_at=start,
                                          event_type=CalendarEvent.TYPE_DEADLINE)
        self.login(self.owner)
        r = self.client.post(reverse("admin:meetings_meeting_new"), {"title": "X", "event": ev.pk})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "Подію календаря не знайдено", status_code=400)
        self.assertFalse(Meeting.objects.exists())

    def test_refusal_when_not_enabled(self):
        MeetingSettings.objects.filter(pk=1).update(enabled=False)
        self.login(self.owner)
        r = self.client.get(reverse("admin:meetings_meeting_new"))
        self.assertContains(r, services.DISABLED_TEXT)
        self.assertNotContains(r, 'name="title"')
        r = self.client.post(reverse("admin:meetings_meeting_new"), {"title": "X"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Meeting.objects.exists())
        r = self.client.get(reverse("admin:meetings_meeting_changelist"))
        self.assertNotContains(r, reverse("admin:meetings_meeting_new"))

    def test_refusal_without_right(self):
        viewer = staff("viewer", module_operations={"meetings": ["view"]})      # бачить розділ, записувати — ні
        self.login(viewer)
        r = self.client.get(reverse("admin:meetings_meeting_new"))
        self.assertContains(r, services.NO_RIGHT_TEXT)
        self.assertNotContains(r, 'name="title"')
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_new"),
                                         HTTP_ACCEPT="text/html").status_code, 200)
        reader = staff("ro", role="readonly")                                     # розділу немає взагалі
        self.login(reader)
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_new")).status_code, 403)

    def test_continue_prefills_and_links(self):
        m = self.processed(items=None)
        self.login(self.owner)
        r = self.client.get(self.url("continue", m))
        self.assertContains(r, f"{TITLE} (продовження)")
        self.assertContains(r, f'name="continues" value="{m.pk}"')
        r = self.client.post(reverse("admin:meetings_meeting_new"), {"title": f"{TITLE} (продовження)",
                                                                     "continues": m.pk, "project": self.project.pk})
        new = Meeting.objects.exclude(pk=m.pk).get()
        self.assertEqual(new.continues, m)


class RecordTests(PagesBase):
    def test_record_redirects_to_bench_with_ticket(self):
        m = self.meeting()
        self.login(self.owner)
        r = self.client.post(self.url("record", m))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("https://192.168.2.68:8791/#minerva="), r["Location"])
        ticket = r["Location"].split("#minerva=", 1)[1]
        self.assertEqual(services.redeem(ticket)["meeting"]["id"], m.pk)

    def test_record_refused_when_disabled(self):
        m = self.meeting()
        MeetingSettings.objects.filter(pk=1).update(enabled=False)
        self.login(self.owner)
        r = self.client.post(self.url("record", m))
        self.assertRedirects(r, self.url("page", m), fetch_redirect_response=False)
        self.assertEqual(Meeting.objects.get(pk=m.pk).ticket_hash, "")

    def test_ready_card_and_never_seen_bench(self):
        m = self.meeting()
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "🎙 Відкрити вікно запису")
        self.assertContains(r, 'target="_blank"')
        self.assertContains(r, "RAG-ПК ще не підключено до Minerva")
        self.assertContains(r, "https://192.168.2.68:8791/#check")
        MeetingSettings.objects.filter(pk=1).update(bench_seen_at=timezone.now(), bench_info={"live_busy": True})
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "RAG-ПК на зв&#x27;язку")
        self.assertContains(r, "зараз іде інший запис")


class StatusTests(PagesBase):
    def test_status_json_without_content(self):
        m = self.processed()
        self.login(self.owner)
        data = self.client.get(self.url("status", m)).json()
        self.assertEqual(data["status"], Meeting.REVIEW)
        self.assertTrue(data["transcript"])
        self.assertEqual(data["pending"], 2)
        self.assertIn("changed", data)
        body = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(TITLE, body)
        self.assertNotIn("Antennen", body)

    def test_processing_lines(self):
        m = self.processed(items=None)
        self.assertEqual(m.status, Meeting.PROCESSING)
        self.login(self.owner)
        data = self.client.get(self.url("status", m)).json()
        self.assertTrue(any("Шукаю ключові моменти" in line["text"] for line in data["lines"]))
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "Шукаю ключові моменти")
        self.assertContains(r, '"poll": true')


class ItemTests(PagesBase):
    def item(self, m, kind):
        return m.items.get(kind=kind)

    def post(self, m, item, body):
        return self.client.post(self.url("item", m, item.pk), json.dumps(body), content_type="application/json")

    def test_accept_moment_reject_restore(self):
        m = self.processed()
        self.login(self.owner)
        decision, task = self.item(m, "decision"), self.item(m, "task")
        r = self.post(m, decision, {"action": "accept", "target": "moment", "title": "Поставка до п’ятниці",
                                    "kind": "delivery"})
        data = r.json()
        self.assertTrue(data["ok"], data)
        self.assertIn("✓ Додано", data["html"])
        self.assertEqual(data["pending"], 1)
        decision.refresh_from_db()
        self.assertEqual((decision.status, decision.target), (MeetingItem.ACCEPTED, "moment"))
        self.assertEqual(decision.highlight.kind, ProjectHighlight.DELIVERY)
        self.assertEqual(decision.highlight.source, ProjectHighlight.MEETING)
        data = self.post(m, task, {"action": "reject"}).json()
        self.assertTrue(data["ok"])
        self.assertIn("↺ Повернути", data["html"])
        self.assertEqual(self.item(m, "task").status, MeetingItem.REJECTED)
        data = self.post(m, task, {"action": "restore"}).json()
        self.assertEqual(self.item(m, "task").status, MeetingItem.PENDING)
        self.assertIn("✓ Прийняти", data["html"])

    def test_deadline_needs_date_and_stays_private(self):
        m = self.processed()
        self.login(self.owner)
        task = self.item(m, "task")
        r = self.post(m, task, {"action": "accept", "target": "deadline", "title": "Креслення"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("дату", r.json()["error"])
        data = self.post(m, task, {"action": "accept", "target": "deadline", "title": "Креслення",
                                   "date": "2026-11-14"}).json()
        self.assertTrue(data["ok"], data)
        task.refresh_from_db()
        self.assertEqual((task.event.user, task.event.title, task.event.description), (self.owner, "Креслення", ""))

    def test_page_shows_panel_with_reasons_and_warning(self):
        m = self.processed()
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "✓ Прийняти ▾")
        self.assertContains(r, "У проєкті ще немає плану")           # 📅 крок без плану — причина
        self.assertContains(r, "Пункт і цитату побачать усі, хто бачить проєкт")
        self.assertContains(r, "Дедлайн бачите лише ви")
        self.assertContains(r, 'value="2026-11-14"')                  # «14.11.2026» зі сказаного → дата
        self.assertContains(r, '<span translate="no">liefern die Antennen bis Freitag</span>')


class KnowledgeTests(PagesBase):
    @override_settings(MINERVA_INSTANCE_ID="test-suite")
    def test_publish_from_page(self):
        from rag_assistant.models import KnowledgeItem
        m = self.processed()
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "📚 Додати в мою базу знань")
        r = self.client.post(self.url("kb", m))
        self.assertEqual(r["Location"], self.url("page", m) + "#kb")
        self.assertIsNotNone(Meeting.objects.get(pk=m.pk).approved_at)
        self.assertEqual(KnowledgeItem.objects.filter(source_key__startswith=f"meeting:{m.pk}:").count(), 1)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "Додано у вашу базу знань")


class SaveTests(PagesBase):
    def test_edit_title_and_participants_logged(self):
        m = self.meeting()
        self.login(self.owner)
        self.client.post(self.url("save", m), {"title": "Neuer Titel", "project": self.project.pk,
                                               "participants_form": "1", "p_name": ["Max Muster"],
                                               "p_email": ["max@client.example"], "p_source": ["manual"],
                                               "x_name": ["Eva"], "x_email": ["eva@client.example"],
                                               "p_on": ["0"], "consent_note": "", "lang_l": "uk"})
        m = Meeting.objects.get(pk=m.pk)
        self.assertEqual((m.title, m.lang_l), ("Neuer Titel", "uk"))
        self.assertEqual([p["email"] for p in m.participants], ["max@client.example", "eva@client.example"])
        self.assertTrue(any(e["kind"] == "edit" for e in m.consent_log))

    def test_save_rechecks_project_access(self):
        user = staff("mgr2", denied_models=["projects:project"])
        m = self.meeting(user, project=None)
        self.login(user)
        r = self.client.post(self.url("save", m), {"title": "X", "project": self.project.pk})
        self.assertRedirects(r, self.url("page", m), fetch_redirect_response=False)
        m.refresh_from_db()
        self.assertIsNone(m.project)
        self.assertEqual(m.title, TITLE)
        msgs = [str(x) for x in r.wsgi_request._messages]
        self.assertTrue(any("Немає права бачити проєкти" in x for x in msgs), msgs)


class RetryTests(PagesBase):
    def test_retry_from_failed(self):
        m = self.processed(items=None)
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.FAILED, bench_detail="GPU")
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "🔄 Спробувати ще раз")
        self.assertContains(r, "data-confirm=")
        self.client.post(self.url("retry", m))
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, Meeting.PROCESSING)


class RemoveTests(PagesBase):
    def test_remove_with_quote_scrub_and_neutral_audit(self):
        m = self.processed()
        decision = m.items.get(kind="decision")
        services.accept(self.owner, decision, "moment", {})
        self.login(self.owner)
        r = self.client.get(self.url("page", m))
        self.assertContains(r, "Також прибрати цитати з цих пунктів (1)")
        r = self.client.post(self.url("remove", m), {"scrub": "1"})
        self.assertRedirects(r, reverse("admin:meetings_meeting_changelist"), fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.filter(pk=m.pk).exists())
        highlight = ProjectHighlight.objects.get(pk=decision.highlight_id)
        self.assertEqual(highlight.quote, "")
        log = AuditLog.objects.get(object_id=str(m.pk), extra__module="meetings")
        self.assertEqual(log.object_repr, f"🎙 Нарада #{m.pk}")
        self.assertNotIn(TITLE, json.dumps(log.extra, ensure_ascii=False))


class ProtocolsTests(PagesBase):
    def test_admin_sees_metadata_only(self):
        m = self.processed()
        self.login(self.other)                                  # адміністратор, але не власник
        r = self.client.get(reverse("admin:meetings_meeting_protocols"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "max@client.example")
        self.assertContains(r, f"#{m.pk}")
        for secret in (TITLE, "Antennen", "Kurz: Lieferung", "Поставка до"):
            self.assertNotContains(r, secret)
        r = self.client.get(reverse("admin:meetings_meeting_protocols") + "?q=nobody@x.y")
        self.assertNotContains(r, "max@client.example")

    def test_admin_delete_and_non_admin_404(self):
        m = self.processed()
        manager = staff("mgr3")
        self.login(manager)
        self.assertEqual(self.client.get(reverse("admin:meetings_meeting_protocols")).status_code, 404)
        self.assertEqual(self.client.post(reverse("admin:meetings_meeting_protocols"),
                                          {"action": "delete", "pk": m.pk}).status_code, 404)
        self.assertTrue(Meeting.objects.filter(pk=m.pk).exists())
        self.login(self.other)
        self.client.post(reverse("admin:meetings_meeting_protocols"), {"action": "delete", "pk": m.pk})
        self.assertFalse(Meeting.objects.filter(pk=m.pk).exists())
        self.assertTrue(AuditLog.objects.filter(object_id=str(m.pk), extra__module="meetings").exists())


class TranslateNoTests(PagesBase):
    def test_transcript_consent_and_summary_marked(self):
        m = self.processed()
        self.login(self.owner)
        html = self.client.get(self.url("page", m)).content.decode()
        self.assertIn('<span class="mt-text" translate="no">Wir liefern die Antennen bis Freitag nach Köln.</span>',
                      html)
        self.assertRegex(html, r'<div class="mt-say" translate="no">[^<]*einverstanden')
        self.assertIn('<div class="mt-summary" translate="no">Kurz: Lieferung bis Freitag.</div>', html)
        self.assertIn("✅ Згоду підтверджено вголос", html)                # позначка згоди → протокол / розшифровка
        for text in re.findall(r"<title>(.*?)</title>", html, re.S):
            self.assertNotIn(TITLE, text)                                   # вкладка браузера — без назви наради


class SettingsTests(PagesBase):
    def test_key_button_creates_meetings_key(self):
        self.login(self.owner)
        url = reverse("admin:meetings_meetingsettings_change", args=[MeetingSettings.load().pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "🔑 Створити ключ для RAG-ПК (наради)")
        self.assertContains(r, "Що ще треба до першого запису")
        self.assertEqual(self.client.get(reverse("admin:meetings_meetingsettings_key")).status_code, 302)
        self.assertFalse(APIKey.objects.exists())
        r = self.client.post(reverse("admin:meetings_meetingsettings_key"))
        key = APIKey.objects.get()
        self.assertEqual((key.scopes, key.log_mode), (["meetings:write"], APIKey.LOG_ERRORS))
        self.assertRedirects(r, reverse("admin:api_apikey_change", args=[key.pk]), fetch_redirect_response=False)
        self.assertIn(key, services.bench_keys())

    def test_key_needs_right_and_settings_read_only_for_manager(self):
        manager = staff("mgr4")
        self.login(manager)
        r = self.client.post(reverse("admin:meetings_meetingsettings_key"))
        self.assertFalse(APIKey.objects.exists())
        url = reverse("admin:meetings_meetingsettings_change", args=[MeetingSettings.load().pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "🔑 Створити ключ для RAG-ПК (наради)</button>")
        self.assertNotContains(r, 'name="_save"')
        self.client.post(url, {"enabled": "", "bench_url": "https://evil.example"})
        self.assertEqual(MeetingSettings.load().bench_url, "https://192.168.2.68:8791")

    def test_texts_reviewed_tick(self):
        self.login(self.owner)
        url = reverse("admin:meetings_meetingsettings_change", args=[MeetingSettings.load().pk])
        r = self.client.post(url, {
            "enabled": "on", "texts_reviewed": "on", "privacy_url": "https://sevskiy.de/datenschutz",
            "works_council_informed": "on", "bench_url": "https://192.168.2.68:8791", "ticket_ttl_seconds": 120,
            "default_retention": "text_only", "transcript_retention_days": 365, "audio_retention_days": 30,
            "protocol_retention_days": 1095, "inactive_owner_days": 30, "notify_email": "on",
            "consent_text_de": "", "consent_text_en": "", "consent_text_uk": "", "_save": "1"})
        self.assertEqual(r.status_code, 302, getattr(r, "context_data", {}).get("errors") if r.status_code != 302
                         else "")
        s = MeetingSettings.load()
        self.assertIsNotNone(s.texts_reviewed_at)
        self.assertEqual(s.texts_reviewed_by, self.owner)
        self.assertFalse(s.notify_telegram)
        self.assertTrue(all(row["ok"] for row in services.rollout_checklist(s) if row["key"] in ("texts", "privacy",
                                                                                                  "council")))


class ViewOnlyOwnerTests(PagesBase):
    """Власнику лишили лише перегляд (module_operations {'meetings': ['view']}): ✓ / ✕, «✏️ Змінити», база знань,
    повтор, запис і продовження — ні (і кнопок немає); «🗑 Видалити нараду» — так (приватність)."""

    def setUp(self):
        super().setUp()
        self.user = staff("cut", module_operations={"meetings": ["view", "add", "change"]})
        self.m = self.processed(user=self.user)
        self.user.profile.module_operations = {"meetings": ["view"]}
        self.user.profile.save()
        self.user = User.objects.get(pk=self.user.pk)
        self.assertFalse(self.user.has_perm("meetings.change_meeting"))
        self.login(self.user)

    def test_changes_refused(self):
        m, item = self.m, self.m.items.first()
        r = self.client.post(self.url("item", m, item.pk), json.dumps({"action": "reject"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], admin_pages.NO_CHANGE_TEXT)
        self.assertEqual(MeetingItem.objects.get(pk=item.pk).status, MeetingItem.PENDING)
        self.client.post(self.url("save", m), {"title": "Інша назва"})
        self.client.post(self.url("kb", m))
        m.refresh_from_db()
        self.assertEqual((m.title, m.approved_at), (TITLE, None))
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.FAILED)
        with patch("meetings.services.can_retry", return_value=True):
            self.client.post(self.url("retry", m))
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, Meeting.FAILED)
        self.client.post(self.url("record", m))
        self.assertEqual(Meeting.objects.get(pk=m.pk).ticket_hash, "")
        r = self.client.get(self.url("continue", m))
        self.assertContains(r, services.NO_RIGHT_TEXT)
        self.assertNotContains(r, 'name="title"')

    def test_page_hides_buttons_but_owner_may_delete(self):
        r = self.client.get(self.url("page", self.m))
        self.assertEqual(r.status_code, 200)
        for hidden in ("✓ Прийняти ▾", "✕ Не те</button>", "✏️ Змінити", "📚 Додати в мою базу знань",
                       "🎙 Записати продовження", self.url("save", self.m)):
            self.assertNotContains(r, hidden)
        self.assertContains(r, admin_pages.NO_CHANGE_TEXT)
        self.assertContains(r, "🗑 Видалити нараду")
        r = self.client.post(self.url("remove", self.m))
        self.assertRedirects(r, reverse("admin:meetings_meeting_changelist"), fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.filter(pk=self.m.pk).exists())


class ReviewRetryTests(PagesBase):
    """«🔄 Спробувати ще раз» — за services.can_retry(m) у будь-якому стані (і «Розшифровка готова», коли ключові
    моменти знайти не вдалося)."""

    def test_review_card_follows_can_retry(self):
        m = self.processed(items=None)
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.REVIEW, moments_error=services.MOMENTS_FAILED_TEXT)
        self.login(self.owner)
        with patch("meetings.services.can_retry", return_value=True):
            r = self.client.get(self.url("page", m))
        self.assertContains(r, services.MOMENTS_FAILED_TEXT)
        self.assertContains(r, "🔄 Спробувати ще раз")
        self.assertContains(r, self.url("retry", m))
        with patch("meetings.services.can_retry", return_value=False):
            r = self.client.get(self.url("page", m))
        self.assertNotContains(r, "🔄 Спробувати ще раз")


class ProgressLinesTests(PagesBase):
    def lines(self, **fields):
        base = {"status": Meeting.PROCESSING, "bench_updated_at": timezone.now(), "created_at": timezone.now()}
        return [x["text"] for x in admin_pages.progress_lines(Meeting(**{**base, **fields}))]

    def test_bench_detail_shown_on_every_stage(self):
        detail = "Спроба 1 з 3 не вдалася — RAG-ПК пробує ще раз"
        for state in ("transcribing", "finding_moments", "recorded"):
            self.assertIn(detail, self.lines(bench_state=state, bench_progress=40, bench_detail=detail), state)
        self.assertIn(detail, self.lines(status=Meeting.RECORDING, started_at=timezone.now(), bench_detail=detail))
        self.assertIn("⏳ Розшифровка… 40 %", self.lines(bench_state="transcribing", bench_progress=40))

    def test_close_note_only_with_notifications(self):
        self.assertIn(admin_pages.CLOSE_NOTE, self.lines(bench_state="transcribing"))
        MeetingSettings.objects.filter(pk=1).update(notify_email=False, notify_telegram=True)
        self.assertIn(admin_pages.CLOSE_NOTE, self.lines(bench_state="transcribing"))
        MeetingSettings.objects.filter(pk=1).update(notify_email=False, notify_telegram=False)
        self.assertNotIn(admin_pages.CLOSE_NOTE, self.lines(bench_state="transcribing"))
        m = self.processed(items=None)
        self.login(self.owner)
        self.assertNotIn(admin_pages.CLOSE_NOTE, [x["text"] for x in self.client.get(self.url("status", m)).json()
                                                  ["lines"]])
        self.assertNotContains(self.client.get(self.url("page", m)), "Сторінку можна закрити")
