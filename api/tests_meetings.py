"""🎙 Наради — адреси для вікна запису на RAG-ПК (/api/v1/meetings/…, api/meetings_views.py).

Ключ, права, ключ магазину й сесія; обмін квитка; стан, розшифровка, ключові моменти; кожен код помилки; 413;
звірка; ping; ліміт запитів; «🔍 Що бачить ключ» і журнал API (без квитка й ключа надсилання).

    python manage.py test api.tests_meetings --settings=tabele.settings_test
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from api import key_insight, log_analysis
from api import meetings_views as mv
from api.models import APIKey, APIRequestLog
from meetings import notify, services
from meetings.models import Meeting, MeetingItem, MeetingSettings, MeetingTranscript
from projects.models import Project

SEGMENTS = [
    {"n": 1, "start": 0.5, "end": 4.0, "ch": "L", "text": "Sind alle einverstanden mit der Aufnahme?"},
    {"n": 2, "start": 4.5, "end": 6.0, "ch": "R", "text": "Ja, passt."},
    {"n": 3, "start": 120.0, "end": 130.0, "ch": "R", "text": "Wir liefern die Antennen bis Freitag nach Köln."},
]
ITEMS = [
    {"kind": "promise", "text": "Клієнт доставить антени до п'ятниці", "who": "Max", "due": "bis Freitag",
     "quote": "liefern die Antennen bis Freitag", "line": 3},
    {"kind": "decision", "text": "Вигадане", "quote": "Wir stornieren den ganzen Auftrag sofort", "line": 3},
]
CONSENT = {"announced": True, "all_agreed": True, "lang": "de"}


class MeetingsAPIBase(TestCase):
    def setUp(self):
        notify.BACKGROUND = False
        self.addCleanup(setattr, notify, "BACKGROUND", True)
        deliver = patch("agent.notify.deliver", return_value="🔔 Повідомлено: пошта")
        deliver.start()
        self.addCleanup(deliver.stop)
        cache.clear()                                 # ліміти запитів (DRF throttle) — у кеші за замовчуванням
        s = MeetingSettings.load()
        s.enabled = True
        s.save()
        self.owner = User.objects.create_superuser("rec", "owner@sevskiy.example", "p")
        self.project = Project.objects.create(name="Antenne 5G", owner=self.owner)
        self.key = APIKey.objects.create(name="RAG-ПК — наради", scopes=["meetings:write"], log_mode=APIKey.LOG_ALL)
        self.api = self.client_for(self.key)

    # ── помічники ──
    @staticmethod
    def client_for(key):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        return c

    def post(self, path, body, token=None, client=None, **extra):
        if token is not None:
            extra["HTTP_X_MEETING_TOKEN"] = token
        raw = body if isinstance(body, (str, bytes)) else json.dumps(body, ensure_ascii=False)
        return (client or self.api).post("/api/v1/meetings/" + path, raw, content_type="application/json", **extra)

    def meeting(self, **data):
        base = {"title": "Abstimmung Antenne", "project": self.project.pk,
                "participants": [{"name": "Max Muster", "email": "max@client.example", "source": "manual"}]}
        base.update(data)
        return services.create(self.owner, base)

    def redeemed(self, m=None):
        """Нарада, для якої вікно запису обміняло квиток через API → (нарада, push_token)."""
        m = m or self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        r = self.post("redeem/", {"ticket": ticket})
        self.assertEqual(r.status_code, 200, r.content)
        return Meeting.objects.get(pk=m.pk), r.json()["push_token"]

    def status(self, m, token, state, seq, session="s1", **data):
        body = {"session": session, "uid": str(m.uid), "state": state, "seq": seq,
                "at": timezone.now().isoformat(), **data}
        return self.post(f"{m.pk}/status/", body, token)

    def recording(self):
        m, token = self.redeemed()
        r = self.status(m, token, "recording", 1, consent=CONSENT, capture={"mode": "both", "processing": "standard"})
        self.assertEqual(r.status_code, 200, r.content)
        return Meeting.objects.get(pk=m.pk), token

    def transcript(self, m, token, segments=SEGMENTS, upload="u1", parts=1, part=1, session="s1"):
        return self.post(f"{m.pk}/transcript/", {"session": session, "uid": str(m.uid), "upload_id": upload,
                                                 "parts": parts, "part": part, "segments": segments,
                                                 "model": "large-v3"}, token)

    def processed(self):
        m, token = self.recording()
        self.assertEqual(self.status(m, token, "recorded", 2, duration=130.0).status_code, 200)
        r = self.transcript(m, token)
        self.assertEqual(r.status_code, 200, r.content)
        return Meeting.objects.get(pk=m.pk), token

    def assertError(self, r, status, code, retryable=False):
        self.assertEqual(r.status_code, status, r.content)
        data = r.json()
        self.assertEqual(data["code"], code, data)
        self.assertEqual(data["retryable"], retryable, data)
        self.assertTrue(data["request_id"].startswith("req-"))
        self.assertIsInstance(data["message"], str)
        return data


class AuthTests(MeetingsAPIBase):
    def test_without_key_401(self):
        r = APIClient().post("/api/v1/meetings/redeem/", "{}", content_type="application/json")
        self.assertError(r, 401, "unauthorized")
        self.assertEqual(r["WWW-Authenticate"], "Token")
        self.assertError(APIClient().get("/api/v1/meetings/ping/"), 401, "unauthorized")

    def test_unknown_or_inactive_key_401(self):
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION="Token " + "0" * 64)
        self.assertError(self.post("sync/", {}, client=bad), 401, "unauthorized")
        APIKey.objects.filter(pk=self.key.pk).update(is_active=False)
        self.assertError(self.post("sync/", {}), 401, "unauthorized")

    def test_wrong_scope_403(self):
        m = self.meeting()
        for scopes in (["stock:read", "orders:write"], ["knowledge:read", "knowledge:write"]):
            key = APIKey.objects.create(name="other", scopes=scopes)
            c = self.client_for(key)
            self.assertError(self.post("redeem/", {"ticket": "x" * 40}, client=c), 403, "forbidden_scope")
            self.assertError(self.post(f"{m.pk}/status/", {"seq": 1}, client=c), 403, "forbidden_scope")
            self.assertError(self.post("sync/", {"sessions": []}, client=c), 403, "forbidden_scope")
            data = self.assertError(c.get("/api/v1/meetings/ping/"), 403, "forbidden_scope")
            self.assertIn("meetings:write", data["message"])          # ping — теж лише з meetings:write

    def test_shop_key_403_even_with_scope(self):
        from shop.models import Shop
        shop = Shop.objects.create(name="Sevskiy", slug="webshop", url="https://web.example.com")
        key = APIKey.objects.create(name="site", scopes=["meetings:write", "products:read"], shop=shop)
        c = self.client_for(key)
        self.assertError(self.post("sync/", {"sessions": []}, client=c), 403, "shop_key_forbidden")
        self.assertError(c.get("/api/v1/meetings/ping/"), 403, "shop_key_forbidden")
        self.assertError(self.post("redeem/", {"ticket": "x" * 40}, client=c), 403, "shop_key_forbidden")

    def test_admin_session_is_not_accepted(self):
        admin = Client()
        admin.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.assertError(admin.get("/api/v1/meetings/ping/"), 401, "unauthorized")
        self.assertError(admin.post("/api/v1/meetings/sync/", "{}", content_type="application/json"),
                         401, "unauthorized")

    def test_ping_needs_write_and_does_not_mark_bench_seen(self):
        r = self.api.get("/api/v1/meetings/ping/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"ok": True, "scopes": ["meetings:write"]})
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertIsNone(MeetingSettings.load().bench_seen_at)

    def test_method_not_allowed(self):
        self.assertError(self.api.get("/api/v1/meetings/redeem/"), 405, "invalid_request")

    def test_throttle_is_own_scope(self):
        self.assertIn(mv.MeetingsRateThrottle, mv.MeetingsBenchView.throttle_classes)
        self.assertEqual(mv.MeetingsRateThrottle.scope, "meetings")
        from django.conf import settings
        self.assertIn("meetings", settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
        self.assertEqual(mv.MeetingsRateThrottle().get_rate(),
                         settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["meetings"])
        APIKey.objects.filter(pk=self.key.pk).update(limit_minute=2)   # свій ліміт ключа діє й тут
        for _ in range(2):
            self.assertEqual(self.post("sync/", {}).status_code, 200)
        data = self.assertError(self.post("sync/", {}), 429, "rate_limited", retryable=True)
        self.assertGreaterEqual(data["details"]["retry_after_seconds"], 1)


class RedeemTests(MeetingsAPIBase):
    def test_happy_path(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        r = self.post("redeem/", {"ticket": ticket})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r["Cache-Control"], "no-store")
        data = r.json()
        self.assertEqual(set(data), {"meeting", "push_token"})
        info = data["meeting"]
        self.assertEqual((info["id"], info["uid"], info["title"]), (m.pk, str(m.uid), "Abstimmung Antenne"))
        self.assertEqual(info["project_code"], self.project.code)
        self.assertIn("de", info["consent_texts"])
        self.assertTrue(info["return_url"].endswith(f"/meetings/meeting/{m.pk}/page/"))
        m.refresh_from_db()
        self.assertEqual(m.ticket_hash, "")
        self.assertIsNotNone(m.ticket_redeemed_at)
        self.assertTrue(m.push_hash)
        self.assertNotIn(data["push_token"], json.dumps(info))

    def test_invalid_ticket_404(self):
        self.assertError(self.post("redeem/", {"ticket": "nope-" * 8}), 404, "ticket_invalid")
        self.assertError(self.post("redeem/", {}), 404, "ticket_invalid")
        self.assertError(self.post("redeem/", {"ticket": 12345}), 404, "ticket_invalid")

    def test_expired_ticket_410(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        Meeting.objects.filter(pk=m.pk).update(ticket_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertError(self.post("redeem/", {"ticket": ticket}), 410, "ticket_expired")

    def test_single_use(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        self.assertEqual(self.post("redeem/", {"ticket": ticket}).status_code, 200)
        self.assertError(self.post("redeem/", {"ticket": ticket}), 404, "ticket_invalid")

    def test_ticket_only_in_body(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        r = self.api.post(f"/api/v1/meetings/redeem/?ticket={ticket}", "{}", content_type="application/json")
        self.assertError(r, 404, "ticket_invalid")
        self.assertEqual(self.post("redeem/", {"ticket": ticket}).status_code, 200)   # квиток не витрачено


class PushTests(MeetingsAPIBase):
    def test_status_recording_with_consent(self):
        m, token = self.recording()
        self.assertEqual(m.status, Meeting.RECORDING)
        self.assertTrue(m.consent_all_agreed)
        r = self.status(m, token, "recorded", 2, duration=130.0)
        self.assertEqual(r.json(), {"ok": True, "applied": True, "status": Meeting.PROCESSING})
        r = self.status(m, token, "recorded", 2)                          # той самий seq — без змін
        self.assertEqual(r.json(), {"ok": True, "applied": False, "status": Meeting.PROCESSING})

    def test_token_errors(self):
        m, token = self.redeemed()
        body = {"session": "s1", "uid": str(m.uid), "state": "recording", "seq": 1, "consent": CONSENT}
        self.assertError(self.post(f"{m.pk}/status/", body), 403, "push_forbidden")                  # без заголовка
        self.assertError(self.post(f"{m.pk}/status/", body, "wrong-token"), 403, "push_forbidden")
        r = self.post(f"{m.pk}/status/", {**body, "push_token": token})                               # не в тілі
        self.assertError(r, 403, "push_forbidden")
        self.assertEqual(self.post(f"{m.pk}/status/", body, token).status_code, 200)

    def test_unknown_and_gone(self):
        self.assertError(self.post("99999/status/", {"session": "s1", "seq": 1}, "t" * 40), 404, "meeting_unknown")
        m, token = self.recording()
        pk, uid = m.pk, str(m.uid)
        services.delete_meeting(self.owner, m)
        for path, body in ((f"{pk}/status/", {"session": "s1", "uid": uid, "seq": 5, "state": "done"}),
                           (f"{pk}/transcript/", {"session": "s1", "uid": uid}),
                           (f"{pk}/moments/", {"session": "s1", "uid": uid, "items": []})):
            self.assertError(self.post(path, body, token), 410, "meeting_gone")

    def test_session_mismatch_409(self):
        m, token = self.recording()                                      # сесія s1 прив'язалась
        self.assertError(self.status(m, token, "recorded", 2, session="s2"), 409, "session_mismatch")

    def test_consent_missing_409(self):
        m, token = self.redeemed()
        self.assertError(self.status(m, token, "recording", 1, consent={"announced": True}), 409, "consent_missing")
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, Meeting.READY)
        self.assertError(self.transcript(m, token), 409, "consent_missing")

    def test_invalid_request_400(self):
        m, token = self.recording()
        data = self.assertError(self.status(m, token, "recorded", 0), 400, "invalid_request")
        self.assertEqual(data["details"]["field"], "seq")
        bad = [dict(SEGMENTS[0], ch="X")]
        data = self.assertError(self.transcript(m, token, segments=bad), 400, "invalid_request")
        self.assertEqual(data["details"]["field"], "segments[0].ch")

    def test_body_must_be_json_object(self):
        m, token = self.recording()
        data = self.assertError(self.post(f"{m.pk}/status/", "not json", token), 400, "invalid_request")
        self.assertEqual(data["details"]["field"], "body")
        self.assertError(self.post(f"{m.pk}/status/", "[1, 2]", token), 400, "invalid_request")
        self.assertError(self.post("sync/", {"sessions": "x"}), 400, "invalid_request")

    def test_transcript_parts_and_exists(self):
        m, token = self.recording()
        r = self.transcript(m, token, segments=SEGMENTS[:2], upload="u1", parts=2, part=1)
        self.assertEqual(r.json(), {"ok": True, "complete": False, "missing": 1})
        r = self.transcript(m, token, segments=SEGMENTS[2:], upload="u1", parts=2, part=2)
        self.assertEqual(r.json(), {"ok": True, "complete": True, "segments": 3})
        self.assertNotIn("Antennen", r.content.decode())                 # тексту розшифровки у відповіді немає
        self.assertEqual(len(MeetingTranscript.objects.get(meeting=m).segments), 3)
        r = self.transcript(m, token, segments=SEGMENTS[2:], upload="u1", parts=2, part=2)   # повтор — без змін
        self.assertEqual(r.json()["complete"], True)
        self.assertError(self.transcript(m, token, upload="u2"), 409, "transcript_exists")

    def test_moments(self):
        m, token = self.recording()
        body = {"session": "s1", "uid": str(m.uid), "model": "qwen", "summary": "Kurz", "items": ITEMS}
        self.assertError(self.post(f"{m.pk}/moments/", body, token), 409, "transcript_incomplete", retryable=True)
        self.assertEqual(self.transcript(m, token).status_code, 200)
        r = self.post(f"{m.pk}/moments/", body, token)
        self.assertEqual(r.json(), {"ok": True, "added": 1, "skipped": 1, "known": 0})
        self.assertNotIn("liefern", r.content.decode())                  # цитат у відповіді немає
        self.assertEqual(MeetingItem.objects.filter(meeting=m).count(), 1)
        self.assertEqual(Meeting.objects.get(pk=m.pk).status, Meeting.REVIEW)
        r = self.post(f"{m.pk}/moments/", body, token)
        self.assertEqual(r.json(), {"ok": True, "added": 0, "skipped": 1, "known": 1})

    def test_payload_too_large_413(self):
        m, token = self.recording()
        big = {"session": "s1", "uid": str(m.uid), "upload_id": "u1", "parts": 1, "part": 1,
               "segments": [dict(SEGMENTS[0], n=i + 1, text="я" * 1900) for i in range(5)]}
        with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=4096):          # межа Django менша за BODY_MAX
            data = self.assertError(self.post(f"{m.pk}/transcript/", big, token), 413, "payload_too_large")
            self.assertEqual(data["details"]["max_bytes"], 4096)
        with patch.object(mv, "BODY_MAX", 2048):                          # власна межа
            self.assertError(self.post(f"{m.pk}/transcript/", big, token), 413, "payload_too_large")
        # RequestDataTooBig від самого Django (власна перевірка довжини обійдена) — теж 413, не 500
        with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=4096), patch.object(mv, "body_limit", return_value=10 ** 9):
            self.assertError(self.post(f"{m.pk}/transcript/", big, token), 413, "payload_too_large")
        self.assertFalse(MeetingTranscript.objects.filter(meeting=m).exclude(segments=[]).exists())

    def test_unexpected_error_500_without_text(self):
        m, token = self.recording()
        with patch("meetings.services.push_status", side_effect=RuntimeError("SECRET-TRANSCRIPT-TEXT")), \
                self.assertLogs("api.meetings_views", "ERROR"):
            r = self.status(m, token, "recorded", 2)
        self.assertError(r, 500, "internal_error", retryable=True)
        self.assertNotIn("SECRET", r.content.decode())


class SyncTests(MeetingsAPIBase):
    def test_empty_without_bench_keeps_bench_seen(self):
        r = self.post("sync/", {"sessions": []})
        self.assertEqual(r.json(), {"sessions": []})
        self.assertIsNone(MeetingSettings.load().bench_seen_at)
        r = self.post("sync/", {"sessions": [], "bench": {"version": "0.3.0", "live_busy": True, "queue": 2,
                                                         "secret_extra": "x" * 500}})
        self.assertEqual(r.status_code, 200)
        s = MeetingSettings.load()
        self.assertIsNotNone(s.bench_seen_at)
        self.assertEqual(s.bench_info, {"version": "0.3.0", "live_busy": True, "queue": 2})

    def test_verdicts(self):
        m, token = self.recording()
        gone, gone_token = self.recording()
        gone_pk = gone.pk
        services.delete_meeting(self.owner, gone)
        sessions = [
            {"meeting": m.pk, "uid": str(m.uid), "session": "s1", "stage": "recording"},
            {"meeting": m.pk, "uid": str(m.uid), "session": "old-one"},
            {"meeting": gone_pk, "uid": str(gone.uid), "session": "s1"},
            {"meeting": 99999, "uid": "6f1c5a40-0000-4000-8000-000000000000", "session": "s9"},
        ]
        r = self.post("sync/", {"sessions": sessions})
        actions = [(e["meeting"], e["session"], e["action"]) for e in r.json()["sessions"]]
        self.assertEqual(actions, [(m.pk, "s1", "keep"), (m.pk, "old-one", "delete"), (gone_pk, "s1", "delete"),
                                   (99999, "s9", "unknown")])
        self.assertEqual(set(r.json()["sessions"][0]), {"meeting", "uid", "session", "action"})

    def test_entries_alias(self):
        m, token = self.recording()
        r = self.post("sync/", {"entries": [{"meeting": m.pk, "uid": str(m.uid), "session": "s1"}]})
        self.assertEqual(r.json()["sessions"][0]["action"], "keep")


class InsightTests(MeetingsAPIBase):
    PATHS = [("POST", "/api/v1/meetings/redeem/"), ("POST", "/api/v1/meetings/sync/"),
             ("GET", "/api/v1/meetings/ping/"), ("POST", "/api/v1/meetings/{meeting_id}/status/"),
             ("POST", "/api/v1/meetings/{meeting_id}/transcript/"), ("POST", "/api/v1/meetings/{meeting_id}/moments/")]

    def rows(self, key):
        return {(r["method"], r["path"]): r for r in key_insight.capabilities(key)}

    def test_capabilities_for_bench_key(self):
        rows = self.rows(self.key)
        for method, path in self.PATHS:
            row = rows[(method, path)]
            self.assertEqual((row["scope"], row["access"], row["section"]), ("meetings:write", "allowed", "meetings"),
                             path)
            self.assertTrue(row["summary"].startswith("RAG-ПК"), path)
            self.assertFalse(row["preview"])
            self.assertTrue(row["no_preview"])
            self.assertFalse(row["simulate"])
            self.assertTrue(row["no_simulate"] or method == "GET")
        self.assertIn("ticket", rows[("POST", "/api/v1/meetings/redeem/")]["accepts"]["text"])
        self.assertIn("scopes", rows[("GET", "/api/v1/meetings/ping/")]["returns"]["text"])
        self.assertIn("🎙 Наради (RAG-ПК)", [title for title, _ in key_insight.grouped(list(rows.values()))])

    def test_other_and_shop_keys_are_denied(self):
        rows = self.rows(APIKey.objects.create(name="stock", scopes=["stock:read"]))
        self.assertTrue(all(rows[p]["access"] == "denied" for p in self.PATHS))
        self.assertEqual(key_insight.summary(list(rows.values()))["write"], 0)
        from shop.models import Shop
        shop = Shop.objects.create(name="Sevskiy", slug="webshop", url="https://web.example.com")
        rows = self.rows(APIKey.objects.create(name="site", scopes=["meetings:write"], shop=shop))
        for p in self.PATHS:
            self.assertEqual((rows[p]["access"], rows[p]["access_text"]),
                             ("denied", "ключ магазину не має доступу до нарад"), p)

    def test_preview_and_simulate_never_run(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        admin = Client()
        admin.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        r = admin.post(f"/admin/api/apikey/{self.key.pk}/insight/preview/", {"path": "/api/v1/meetings/ping/"})
        self.assertFalse(r.json()["ok"])
        self.assertIn("RAG-ПК", r.json()["error"])
        with patch("meetings.services.push_status") as push:
            r = admin.post(f"/admin/api/apikey/{self.key.pk}/insight/simulate/",
                           {"method": "POST", "path": "/api/v1/meetings/1/status/", "body": "{}"})
            push.assert_not_called()
        self.assertFalse(r.json()["ran"])

    def test_guide_shows_write_scope(self):
        from guide import api_docs
        eps = {e["path"]: e for e in api_docs.endpoints("meetings")}
        self.assertEqual({p for _, p in self.PATHS}, set(eps))
        self.assertTrue(all(m["scope"] == "meetings:write" for e in eps.values() for m in e["methods"]))
        self.assertEqual(eps["/api/v1/meetings/ping/"]["methods"][0]["method"], "GET")


class LogTests(MeetingsAPIBase):
    def test_log_has_no_ticket_or_token(self):
        m = self.meeting()
        ticket = services.issue_ticket(m, self.owner)
        r = self.api.post(f"/api/v1/meetings/redeem/?ticket={ticket}&x=1", json.dumps({"ticket": ticket}),
                          content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        token = r.json()["push_token"]
        m = Meeting.objects.get(pk=m.pk)
        self.status(m, token, "recording", 1, consent=CONSENT)
        self.status(m, "wrong-" + token, "recorded", 2)
        self.transcript(m, token)
        self.api.get("/api/v1/meetings/ping/")
        rows = list(APIRequestLog.objects.order_by("pk"))
        self.assertEqual(len(rows), 5)
        for row in rows:
            dump = json.dumps({f.name: str(getattr(row, f.name)) for f in APIRequestLog._meta.fields},
                              ensure_ascii=False)
            self.assertNotIn(ticket, dump)
            self.assertNotIn(token, dump)
            self.assertNotIn(self.key.key, dump)
            self.assertNotIn("Antennen", dump)                              # і тексту розшифровки
        redeem, recording, refused, part, ping = rows
        self.assertEqual(redeem.query, "ticket=%2A%2A%2A&x=%2A%2A%2A")             # «***», як інші секрети
        self.assertEqual((redeem.view, redeem.scope), ("MeetingRedeemView", "meetings:write"))
        self.assertEqual((refused.status, refused.code), (403, "push_forbidden"))
        self.assertEqual(ping.view, "MeetingPingView.get")
        self.assertIn("обміняв квиток вікна запису наради", log_analysis.plain_line(redeem))
        self.assertIn("надіслав частину розшифровки наради", log_analysis.plain_line(part))
        self.assertIn("перевірив ключ RAG-ПК", log_analysis.plain_line(ping))
        self.assertIn("не той ключ надсилання", log_analysis.plain_line(refused))

    def test_error_codes_have_texts(self):
        for code in ("ticket_invalid", "ticket_expired", "meeting_unknown", "meeting_gone", "push_forbidden",
                     "session_mismatch", "consent_missing", "transcript_exists", "transcript_incomplete",
                     "payload_too_large", "invalid_request", "shop_key_forbidden", "forbidden_scope", "unauthorized",
                     "rate_limited", "internal_error"):
            self.assertIn(code, log_analysis.CODE_TEXT, code)
        for name in key_insight.MEETING_VIEWS:
            self.assertTrue(any(k.startswith(name + ".") for k in log_analysis.PHRASES), name)
