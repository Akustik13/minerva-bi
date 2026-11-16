"""✏️ Виправити помилки розпізнавання — Claude пропонує правки «було → стало» (meetings/ai_fix.py):
python manage.py test meetings.tests_ai_fix --settings=tabele.settings_test

Правка — лише з дослівним «from» з того самого рядка; текст змінюється лише після ✓ (і цитати пунктів цього рядка),
↶ повертає; відхилена вдруге не пропонується; без галочки згоди, ключа чи розшифровки — відмова; протокол згоди;
база знань оновлюється після ✓; сторінка — блок, позначки в тексті, адреси лише POST і лише власнику.
"""
import json
from unittest.mock import patch

from django.urls import reverse

from inventory.models import Product
from meetings import admin_pages, ai_fix, services
from meetings.models import Meeting, MeetingItem, MeetingTranscript
from meetings.services import MeetingError
from meetings.tests_pages import ITEMS, PagesBase

ANSWER = {"fixes": [
    {"line": 3, "from": "Antennen", "to": "Antennen AN-5G", "why": "артикул проєкту"},
    {"line": 3, "from": "nicht im Text", "to": "egal", "why": "вигадано"},          # немає в рядку — ні
    {"line": 4, "from": "Zeichnung", "to": "Zeichnung", "why": "те саме"},         # без змін — ні
    {"line": 99, "from": "Montag", "to": "Dienstag", "why": "немає рядка"},        # немає рядка — ні
    {"line": 4, "from": "Ich", "to": "Ich und alle anderen Kollegen aus dem Büro", "why": "переписування"},
]}


class FixBase(PagesBase):
    def setUp(self):
        super().setUp()
        from core.shared_cache import cache
        cache.clear()
        ai_fix.BACKGROUND = False
        self.addCleanup(setattr, ai_fix, "BACKGROUND", True)
        avail = patch("meetings.ai_fix.available", return_value=True)
        avail.start()
        self.addCleanup(avail.stop)

    def claude(self, answer=ANSWER):
        return patch.multiple("email_assistant.ai_helper", _call_ai_direct=lambda *a, **k: json.dumps(answer),
                              last_usage=lambda: None, last_model=lambda: "claude-sonnet-5-5")

    def segments(self, m):
        return MeetingTranscript.objects.get(meeting=m).segments

    def proposed(self, m):
        with self.claude():
            st = ai_fix.start(m, self.owner, "claude-sonnet-5-5", consent=True)
        self.assertEqual(st["state"], "done", st)
        return [f for s in self.segments(m) for f in s.get("fix") or []]


class ValidateTests(FixBase):
    def test_only_verbatim_short_fixes(self):
        segs = [{"n": 3, "text": "Wir liefern die Antennen bis Freitag nach Köln."}]
        out = ai_fix.validate(segs, ANSWER["fixes"] + [{"line": 3, "from": "Antennen", "to": "Antenne"}])
        self.assertEqual([(f["n"], f["from"], f["to"]) for f in out], [(3, "Antennen", "Antennen AN-5G")])

    def test_glossary_from_the_meeting(self):
        product = Product.objects.create(sku="AN220213-01C", name="Antenna 5G")
        self.project.products.add(product)
        m = self.meeting()
        words = ai_fix.glossary(m)
        for word in ("Client GmbH", self.project.code, "Antenne 5G", "Max Muster", "AN220213-01C", "Antenna 5G"):
            self.assertIn(word, words)


class ServiceTests(FixBase):
    def test_refusals(self):
        m = self.processed()
        with self.assertRaisesMessage(MeetingError, "Поставте галочку"):
            ai_fix.start(m, self.owner, "claude-sonnet-5-5", consent=False)
        with self.assertRaises(MeetingError):
            ai_fix.start(m, self.owner, "gpt-5", consent=True)
        with patch("meetings.ai_fix.available", return_value=False), \
                self.assertRaisesMessage(MeetingError, "немає ключа Anthropic"):
            ai_fix.start(m, self.owner, "claude-sonnet-5-5", consent=True)
        with self.assertRaisesMessage(MeetingError, "Розшифровки ще немає"):
            ai_fix.start(self.meeting(), self.owner, "claude-sonnet-5-5", consent=True)
        from django.http import Http404
        with self.assertRaises(Http404):
            ai_fix.start(m, self.other, "claude-sonnet-5-5", consent=True)

    def test_propose_does_not_change_text_and_logs_consent(self):
        m = self.processed()
        before = [s["text"] for s in self.segments(m)]
        fixes = self.proposed(m)
        self.assertEqual([(f["from"], f["to"], f["st"]) for f in fixes], [("Antennen", "Antennen AN-5G", "p")])
        self.assertEqual([s["text"] for s in self.segments(m)], before)
        m.refresh_from_db()
        cloud = [e for e in m.consent_log if e.get("kind") == "cloud"]
        self.assertEqual((cloud[-1]["task"], cloud[-1]["where"], cloud[-1]["model"]),
                         ("fix", "minerva", "claude-sonnet-5-5"))
        self.assertTrue(any("перевірено на помилки розпізнавання" in x["text"] for x in admin_pages.log_lines(m)))

    def test_accept_reset_reject_and_no_second_offer(self):
        m = self.processed()
        fid = self.proposed(m)[0]["id"]
        res = ai_fix.decide(self.owner, m, "accept", [fid])
        self.assertEqual((res["changed"], res["counts"]["accepted"]), (1, 1))
        seg = next(s for s in self.segments(m) if s["n"] == 3)
        self.assertEqual(seg["text"], "Wir liefern die Antennen AN-5G bis Freitag nach Köln.")
        self.assertEqual(seg["orig"], "Wir liefern die Antennen bis Freitag nach Köln.")
        item = m.items.get(segment=3)
        self.assertEqual(item.quote, "liefern die Antennen AN-5G bis Freitag")
        key = item.key
        ai_fix.decide(self.owner, m, "reset", [fid])                               # ↶ — як розпізнав RAG-ПК
        seg = next(s for s in self.segments(m) if s["n"] == 3)
        self.assertEqual(seg["text"], "Wir liefern die Antennen bis Freitag nach Köln.")
        item.refresh_from_db()
        self.assertEqual((item.quote, item.key), ("liefern die Antennen bis Freitag", key))
        res = ai_fix.decide(self.owner, m, "reject", "all")
        self.assertEqual(res["counts"], {"pending": 0, "accepted": 0, "rejected": 1})
        self.proposed(m)                                                           # той самий пошук ще раз
        self.assertEqual(ai_fix.counts(self.segments(m))["pending"], 0)            # відхилене не повертається

    def test_accept_all_skips_a_place_another_fix_changed(self):
        m = self.processed()
        answer = {"fixes": [{"line": 4, "from": "Zeichnung", "to": "Zeichnungen", "why": "a"},
                            {"line": 4, "from": "Zeichnung bis", "to": "Skizze bis", "why": "b"}]}
        with self.claude(answer):
            ai_fix.start(m, self.owner, "claude-haiku-5-5", consent=True)
        res = ai_fix.decide(self.owner, m, "accept", "all")
        self.assertEqual((res["changed"], res["skipped"]), (1, 1))
        self.assertEqual(next(s for s in self.segments(m) if s["n"] == 4)["text"], "Ich schicke die Zeichnungen bis Montag.")

    def test_knowledge_base_refreshed_after_accept(self):
        m = self.processed()
        Meeting.objects.filter(pk=m.pk).update(approved_at=m.transcript_at)
        m.refresh_from_db()
        fid = self.proposed(m)[0]["id"]
        with patch("meetings.knowledge.publish") as publish, self.captureOnCommitCallbacks(execute=True):
            ai_fix.decide(self.owner, m, "accept", [fid])
        publish.assert_called_once()

    def test_pieces_mark_pending_and_accepted(self):
        seg = {"n": 1, "text": "a Köln b Bonn c", "fix": [
            {"id": "x", "from": "Köln", "to": "Kölln", "why": "w", "st": "p"},
            {"id": "y", "from": "Bon", "to": "Bonn", "st": "a"},
            {"id": "z", "from": "c", "to": "C", "st": "r"}]}
        out = ai_fix.pieces(seg)
        self.assertEqual([(p["text"], p["kind"]) for p in out],
                         [("a ", ""), ("Köln", "p"), (" b ", ""), ("Bonn", "a"), (" c", "")])
        self.assertIsNone(ai_fix.pieces({"text": "x"}))


class PageTests(FixBase):
    def test_box_marks_and_endpoints(self):
        m = self.processed()
        self.login(self.owner)
        page = self.client.get(self.url("page", m))
        self.assertContains(page, "✏️ Виправити помилки розпізнавання")
        self.assertContains(page, ai_fix.CONSENT_TEXT)
        url = self.url("fix", m)
        self.assertEqual(self.client.get(url).status_code, 405)
        r = self.client.post(url, json.dumps({"model": "claude-sonnet-5-5"}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        with self.claude():
            r = self.client.post(url, json.dumps({"model": "claude-sonnet-5-5", "consent": True}),
                                 content_type="application/json")
        self.assertEqual(r.json()["state"]["state"], "done")
        self.assertEqual(self.client.get(self.url("status", m)).json()["ai_fix"]["state"], "done")
        page = self.client.get(self.url("page", m))
        self.assertContains(page, 'class="mt-fx"')
        self.assertContains(page, "чекають ✓ / ✕")
        fid = [f for s in self.segments(m) for f in s.get("fix") or []][0]["id"]
        decide = self.url("fix_decide", m)
        self.assertEqual(self.client.get(decide).status_code, 405)
        r = self.client.post(decide, json.dumps({"action": "accept", "ids": [fid]}), content_type="application/json")
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIn("mt-fx-ok", data["rows"]["3"])
        self.assertIn("Виправлено (1)", data["panel"])
        self.login(self.other)
        self.assertEqual(self.client.post(decide, json.dumps({"action": "reset", "ids": [fid]}),
                                          content_type="application/json").status_code, 404)

    def test_url_names(self):
        self.assertEqual(reverse("admin:meetings_meeting_fix", args=[3]), "/admin/meetings/meeting/3/fix/")
        self.assertEqual(reverse("admin:meetings_meeting_fix_decide", args=[3]), "/admin/meetings/meeting/3/fix/decide/")

    def test_escaped_text(self):
        m = self.processed()
        tr = MeetingTranscript.objects.get(meeting=m)
        segs = tr.segments
        segs[2]["text"] = "Wir <b>liefern</b> die Antennen"
        segs[2]["fix"] = [{"id": "q", "from": "Antennen", "to": "<i>x</i>", "why": "<s>", "st": "p"}]
        tr.segments = segs
        tr.save()
        html = admin_pages.fix_text_html(segs[2])
        self.assertIn("&lt;b&gt;liefern&lt;/b&gt;", html)
        self.assertNotIn("<i>", html)
        self.login(self.owner)
        self.assertNotContains(self.client.get(self.url("page", m)), "<i>x</i>")
