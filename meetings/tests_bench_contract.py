"""🎙 Стик «вікно запису на RAG-ПК ↔ Minerva»: тіла запитів будує справжній код стенду (meetings/rag_pc/bench/minerva.py —
лише стандартна бібліотека), а приймає справжній API Minerva (/api/v1/meetings/…).

Так обидві сторони перевіряються однією й тією самою розмовою: квиток → «йде запис» зі згодою → запис збережено →
розшифровка кількома частинами → ключові моменти → готово; «🔄 Спробувати ще раз» після збою; звірка.

    python manage.py test meetings.tests_bench_contract --settings=tabele.settings_test
"""
import importlib.util
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from meetings import notify, services
from meetings.models import Meeting, MeetingItem, MeetingSettings
from projects.models import Project

BENCH = Path(__file__).resolve().parent / "rag_pc" / "bench" / "minerva.py"


def _bench_client_module():
    spec = importlib.util.spec_from_file_location("bench_minerva_contract", BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _bench_client_module()

# transcript.json, як його пише воркер стенду (Whisper): канали L / R, час у секундах
TRANSCRIPT = {
    "model": "large-v3",
    "languages": {"L": {"language": "de", "probability": 0.97}, "R": {"language": "de", "probability": 0.95}},
    "segments": [
        {"start": 0.4, "end": 3.9, "ch": "L", "text": "Sind alle einverstanden, dass wir das Gespräch aufzeichnen?"},
        {"start": 4.2, "end": 5.0, "ch": "R", "text": "Ja, einverstanden."},
        {"start": 60.0, "end": 66.0, "ch": "L", "text": "Wir schicken Ihnen das Angebot für die Antennen bis Montag."},
        {"start": 70.0, "end": 78.0, "ch": "R", "text": "Die Lieferung nach Köln muss spätestens am 20. November da sein."},
        {"start": 80.0, "end": 88.0, "ch": "R", "text": "Wenn der Stecker nicht passt, verschiebt sich das ganze Projekt."},
    ] + [{"start": 100.0 + i, "end": 100.5 + i, "ch": "L" if i % 2 else "R",
          "text": f"Füllsatz Nummer {i} über Kabel, Stecker und Prüfprotokolle für die Abnahme."} for i in range(40)],
}
# moments.json, як його пише крок «ключові моменти» стенду (line = рядок розшифровки, з 1)
MOMENTS = {
    "model": "qwen3.5:9b-q4_K_M", "lang": "uk", "summary": "Домовились про пропозицію й строк поставки.",
    "items": [
        {"kind": "promise", "text": "Надіслати пропозицію до понеділка", "who": "ми", "due": "bis Montag",
         "quote": "Wir schicken Ihnen das Angebot für die Antennen bis Montag", "line": 3},
        {"kind": "deadline", "text": "Поставка в Кельн до 20 листопада", "who": "", "due": "20. November",
         "quote": "muss spätestens am 20. November da sein", "line": 4},
        {"kind": "risk", "text": "Невідповідний роз'єм зсуне проєкт", "who": "", "due": "",
         "quote": "verschiebt sich das ganze Projekt", "line": 5},
        {"kind": "decision", "text": "Вигадане", "quote": "Wir stornieren alles sofort", "line": 3},
    ],
    "rejected": [{"quote": "x"}],
}
MARKS = [{"t": 65.0, "note": "", "kind": "user"}, {"t": 2.0, "note": "consent_ok"},
         {"t": 90.0, "note": "мікрофон від’єднано", "kind": "auto"}]


class BenchContractTests(TestCase):
    def setUp(self):
        notify.BACKGROUND = False
        self.addCleanup(setattr, notify, "BACKGROUND", True)
        deliver = patch("agent.notify.deliver", return_value="🔔 Повідомлено: пошта")
        self.deliver = deliver.start()
        self.addCleanup(deliver.stop)
        cache.clear()
        s = MeetingSettings.load()
        s.enabled = True
        s.save()
        self.owner = User.objects.create_superuser("rec", "owner@sevskiy.example", "p")
        self.project = Project.objects.create(name="Antenne 5G", owner=self.owner)
        self.key = APIKey.objects.create(name="RAG-ПК — наради", scopes=["meetings:write"],
                                         log_mode=APIKey.LOG_ERRORS)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {self.key.key}")
        self.seq = 0
        self.session = "20261111-100000-abcd"

    # ── як Client.call стенду: JSON з ensure_ascii=False, токен наради в заголовку ──
    def call(self, path, body, token=None, method="post"):
        extra = {"HTTP_X_MEETING_TOKEN": token} if token else {}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if method == "get":
            r = self.api.get("/api/v1/meetings/" + path, **extra)
        else:
            r = self.api.post("/api/v1/meetings/" + path, raw, content_type="application/json", **extra)
        return r.status_code, (r.json() if r.content else {})

    def redeem(self):
        m = services.create(self.owner, {"title": "Abstimmung Antenne", "project": self.project.pk,
                                         "participants": [{"name": "Max", "email": "max@client.example",
                                                           "source": "manual"}]})
        ticket = services.issue_ticket(m, self.owner)
        code, data = self.call("redeem/", {"ticket": ticket})
        self.assertEqual(code, 200, data)
        binding = bm.parse_redeem(data)                       # стенд приймає відповідь Minerva як є
        self.assertEqual(binding["meeting"], m.pk)
        self.assertTrue(binding["consent_texts"], "redeem має віддати тексти для читання вголос")
        return binding

    def status(self, b, state, **data):
        """Тіло — як link._status_body: uid, session, seq, at, started_at, state, bench_version + дані стану."""
        self.seq += 1
        body = {"uid": b["uid"], "session": self.session, "seq": self.seq, "at": bm.now_iso(),
                "started_at": b.get("started_at"), "state": state, "bench_version": "0.3.0"}
        body.update({k: v for k, v in data.items() if v is not None})
        return self.call(f"{b['meeting']}/status/", body, b["push_token"])

    def transcript(self, b, part_bytes=4096, upload_id=None):
        segs, truncated = bm.segments_of(TRANSCRIPT)
        upload_id = upload_id or uuid.uuid4().hex
        base = {"uid": b["uid"], "session": self.session, "upload_id": upload_id,
                "languages": bm.languages_of(TRANSCRIPT["languages"]), "model": "large-v3", "duration": 140.5,
                "marks": bm.marks_of(MARKS), "total_segments": len(segs), "truncated": truncated}
        ranges = bm.split_parts(base, segs, part_bytes)
        self.assertGreater(len(ranges), 1, "малий розмір частини має дати кілька частин")
        last = None
        for i, (first, end) in enumerate(ranges, 1):
            body = {**base, "parts": len(ranges), "part": i, "segments": segs[first:end]}
            self.assertLessEqual(bm.encoded_size(body), part_bytes)
            last = self.call(f"{b['meeting']}/transcript/", body, b["push_token"])
            self.assertEqual(last[0], 200, last[1])
        return last[1], len(segs)

    def moments(self, b):
        base = {"uid": b["uid"], "session": self.session}
        return self.call(f"{b['meeting']}/moments/", bm.moments_payload(base, MOMENTS, TRANSCRIPT), b["push_token"])

    def recording(self):
        b = self.redeem()
        b["started_at"] = bm.now_iso()
        code, data = self.status(b, "recording", consent={"announced": True, "all_agreed": True, "lang": "de",
                                                          "at": bm.now_iso()},
                                 capture={"mode": "both", "processing": "headset", "headset": True},
                                 lang_l="de", lang_r="")
        self.assertEqual(code, 200, data)
        return b

    # ── повний шлях ──
    def test_full_pipeline_with_bench_payloads(self):
        b = self.recording()
        m = Meeting.objects.get(pk=b["meeting"])
        self.assertEqual(m.status, "recording")
        self.assertEqual(m.bench_session, self.session)

        code, data = self.status(b, "recorded", duration=140.5, silent=[],
                                 levels=bm.levels_of({"L": {"peak_dbfs": -6.2, "sound_s": 40, "sound_share": 0.3},
                                                      "R": {"peak_dbfs": -9.0}}),
                                 marks=bm.marks_of(MARKS))
        self.assertEqual(code, 200, data)
        self.assertEqual(self.status(b, "transcribing", progress=40)[0], 200)

        data, total = self.transcript(b)
        self.assertTrue(data.get("complete"), data)
        m = Meeting.objects.get(pk=b["meeting"])
        self.assertEqual(len(m.transcript.segments), total)
        self.assertNotIn("sound_s", json.dumps(m.levels))       # жодного «хто скільки говорив»
        self.assertNotIn("sound_share", json.dumps(m.levels))
        # повтор тієї самої частини після збирання — без змін
        self.assertEqual(self.status(b, "transcribed", segments=total)[0], 200)
        self.assertEqual(self.status(b, "finding_moments", detail="qwen3.5")[0], 200)

        with self.captureOnCommitCallbacks(execute=True):      # сповіщення йде після commit
            code, data = self.moments(b)
            self.assertEqual(code, 200, data)
            self.assertEqual(data.get("added"), 3, data)        # вигадана цитата відкинута
            code, data = self.status(b, "done", segments=total, items=3)
            self.assertEqual(code, 200, data)
        code, data = self.status(b, "audio_deleted", detail="Аудіо видалено на RAG-ПК")
        self.assertEqual(code, 200, data)
        code, data = self.status(b, "purged")                  # нового стану Minerva не знає — не помилка
        self.assertEqual(code, 200, data)

        m = Meeting.objects.get(pk=b["meeting"])
        self.assertEqual(m.status, "review")
        kinds = set(MeetingItem.objects.filter(meeting=m).values_list("kind", flat=True))
        self.assertTrue({"promise", "deadline", "risk"} <= kinds, kinds)
        self.assertIn("mark", kinds)                            # 📌 людини стала пунктом, автоматична — ні
        self.assertEqual(MeetingItem.objects.filter(meeting=m, kind="mark").count(), 1)
        self.assertTrue(m.audio_deleted_at)
        self.assertEqual(self.deliver.call_count, 1)            # одне сповіщення

        # звірка: стенд знає сесію → keep
        code, data = self.call("sync/", {"sessions": [{"meeting": m.pk, "uid": b["uid"], "session": self.session,
                                                       "token_sha256": bm.token_sha256(b["push_token"]),
                                                       "stage": "done", "progress": 100, "queue_pos": 0,
                                                       "audio_on_bench": False}],
                                         "bench": {"version": "0.3.0", "live_busy": False, "queue": 0, "outbox": 0,
                                                   "failed": 0, "last_error": "", "retention_days": 30,
                                                   "standalone": False}})
        self.assertEqual(code, 200, data)
        verdicts = {v["session"]: v["action"] for v in data["sessions"]}
        self.assertEqual(verdicts[self.session], "keep")

    def test_part_size_halving_restarts_with_new_upload(self):
        b = self.recording()
        self.status(b, "recorded", duration=140.5)
        first, _ = self.transcript(b, part_bytes=4096)
        self.assertTrue(first["complete"])
        # інший upload_id після збирання — 409 transcript_exists, стенд рахує розшифровку доставленою
        segs, _ = bm.segments_of(TRANSCRIPT)
        code, data = self.call(f"{b['meeting']}/transcript/",
                               {"uid": b["uid"], "session": self.session, "upload_id": uuid.uuid4().hex,
                                "parts": 1, "part": 1, "segments": segs}, b["push_token"])
        self.assertEqual(code, 409, data)
        self.assertEqual(data["code"], "transcript_exists")
        self.assertEqual(bm.classify(bm.RemoteError(code, data["code"], coded=True)), "done")

    def test_retry_after_failed_transcription(self):
        b = self.recording()
        self.status(b, "recorded", duration=140.5)
        code, data = self.status(b, "error", stage="transcribe", message="Забракло відеопам’яті")
        self.assertEqual(code, 200, data)
        m = Meeting.objects.get(pk=b["meeting"])
        self.assertEqual(m.status, "failed")
        self.assertTrue(services.can_retry(m))
        services.retry(self.owner, m)

        entry = {"meeting": m.pk, "uid": b["uid"], "session": self.session,
                 "token_sha256": bm.token_sha256(b["push_token"]), "stage": "failed", "progress": 0,
                 "queue_pos": 0, "audio_on_bench": True}
        code, data = self.call("sync/", {"sessions": [entry]})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["sessions"][0]["action"], "retry")

        # стенд розшифровує знову й шле хід, поки нарада в Minerva ще «Не вдалося»
        code, data = self.status(b, "transcribing", progress=10, detail="спроба 2")
        self.assertEqual(code, 200, data)
        data, total = self.transcript(b)
        self.assertTrue(data["complete"])
        code, data = self.moments(b)
        self.assertEqual(code, 200, data)
        self.status(b, "done", segments=total, items=3)
        m = Meeting.objects.get(pk=b["meeting"])
        self.assertEqual(m.status, "review")

    def test_discard_and_stale_window(self):
        b = self.recording()
        code, data = self.status(b, "discarded", detail="учасник не погодився")
        self.assertEqual(code, 200, data)
        m = Meeting.objects.get(pk=b["meeting"])
        self.assertEqual(m.status, "ready")
        # нове вікно запису: старий токен більше не діє
        ticket = services.issue_ticket(m, self.owner)
        code, data = self.call("redeem/", {"ticket": ticket})
        self.assertEqual(code, 200, data)
        code, data = self.status(b, "recording", consent={"announced": True, "all_agreed": True, "lang": "de"})
        self.assertEqual(code, 403, data)
        self.assertEqual(data["code"], "push_forbidden")
        self.assertEqual(bm.classify(bm.RemoteError(code, data["code"], coded=True)), "blocked")

    def test_ping_for_the_gui(self):
        code, data = self.call("ping/", None, method="get")
        self.assertEqual(code, 200, data)
        self.assertIn("meetings:write", data["scopes"])
