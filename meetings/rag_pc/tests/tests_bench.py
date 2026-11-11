"""Unit tests of the bench without models or GPU:  python -m unittest discover -s tests -p "tests*.py"

(run from meetings/rag_pc; numpy is needed for the ring buffer test)
"""
import json
import tempfile
import time
import unittest

from bench import access, config, moments, store


class AccessTests(unittest.TestCase):
    def test_proxy_headers_only_from_trusted_proxy(self):
        h = {"X-Real-IP": "192.168.2.108", "X-Forwarded-For": "1.2.3.4, 10.0.0.9"}
        self.assertEqual(access.client_ip("192.168.2.142", h, ["192.168.2.142"]), "192.168.2.108")
        self.assertEqual(access.client_ip("192.168.2.50", h, ["192.168.2.142"]), "192.168.2.50")
        h2 = {"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}
        self.assertEqual(access.client_ip("192.168.2.142", h2, ["192.168.2.142"]), "10.0.0.9")

    def test_allow_list_with_networks_and_mapped_v4(self):
        allowed = ["127.0.0.1", "192.168.2.0/24", "not an address"]
        self.assertTrue(access.allowed("192.168.2.77", allowed))
        self.assertTrue(access.allowed("::ffff:192.168.2.77", allowed))
        self.assertFalse(access.allowed("192.168.3.1", allowed))
        self.assertFalse(access.allowed("", allowed))

    def test_same_origin(self):
        ok = {"Origin": "https://192.168.2.68:8791", "Host": "192.168.2.68:8791"}
        self.assertTrue(access.same_origin(ok, "https"))
        self.assertFalse(access.same_origin(ok, "http"))
        self.assertFalse(access.same_origin({"Origin": "https://evil.example", "Host": "192.168.2.68:8791"}, "https"))
        self.assertFalse(access.same_origin({"Host": "192.168.2.68:8791"}, "https"))
        proxied = {"Origin": "https://meet.sevskiy.com", "Host": "192.168.2.68:8791", "X-Forwarded-Host": "meet.sevskiy.com"}
        self.assertTrue(access.same_origin(proxied, "https", via_proxy=True))
        self.assertFalse(access.same_origin(proxied, "https", via_proxy=False))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)
        self.store = store.Store(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ids_never_leave_the_sessions_folder(self):
        for bad in ("..", "../data", "20261109-120000-zzzz", "x", "20261109-120000-abcd/../.."):
            with self.assertRaises(KeyError):
                self.store.path(bad)

    def test_create_list_delete(self):
        sid, folder, meta = self.store.create("upload", "  Тест  ", lang="de")
        self.assertEqual(meta["title"], "Тест")
        (folder / "source.mp3").write_bytes(b"x")
        self.assertEqual(self.store.audio_file(sid).name, "source.mp3")
        self.assertEqual([s["id"] for s in self.store.list()], [sid])
        self.store.delete(sid)
        self.assertEqual(self.store.list(), [])

    def test_purge_removes_only_old(self):
        old = self.cfg.sessions_dir / time.strftime("%Y%m%d-%H%M%S-0000", time.localtime(time.time() - 40 * 86400))
        old.mkdir()
        sid, _, _ = self.store.create("live", "new")
        self.assertEqual(self.store.purge(14), 1)
        self.assertFalse(old.exists())
        self.assertEqual([s["id"] for s in self.store.list()], [sid])


class MomentsTests(unittest.TestCase):
    LINES = [
        (1, 0.0, "Співрозмовники", "Der Kunde braucht die Ware spätestens am 20. November in München."),
        (2, 6.0, "Ви", "Добре, домовились: першу партію відправляємо в п’ятницю через UPS."),
        (3, 9.0, "Ви", "Олено, підготуй Commercial Invoice до четверга."),
    ]

    def test_quote_must_exist(self):
        items = [
            {"kind": "decision", "text": "UPS у п'ятницю", "line": 2, "quote": "першу партію відправляємо в п'ятницю через UPS"},
            {"kind": "task", "text": "вигадка", "line": 3, "quote": "Олена надішле все в понеділок"},
            {"kind": "deadline", "text": "20.11", "line": 9, "quote": "spätestens am 20. November in München"},
            {"kind": "weird", "text": "?", "line": "x", "quote": "Commercial Invoice до четверга"},
        ]
        ok, rejected = moments.validate(items, self.LINES)
        self.assertEqual([(i["kind"], i["line"]) for i in ok], [("deadline", 1), ("decision", 2), ("info", 3)])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(ok[1]["speaker"], "Ви")

    def test_short_or_duplicate_quotes(self):
        items = [{"kind": "task", "text": "a", "line": 3, "quote": "до"},
                 {"kind": "task", "text": "b", "line": 3, "quote": "підготуй Commercial Invoice"},
                 {"kind": "task", "text": "c", "line": 3, "quote": "Підготуй  Commercial   Invoice"}]
        ok, rejected = moments.validate(items, self.LINES)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(rejected), 1)

    def test_chunks_and_render(self):
        lines = [(i, i * 2.0, "Ви", "слово " * 50) for i in range(1, 200)]
        parts = list(moments.chunks(lines, limit=3000))
        self.assertGreater(len(parts), 1)
        self.assertEqual(sum(len(p) for p in parts), len(lines))
        self.assertTrue(moments.render(parts[0]).startswith("[#1] [00:02] Ви: слово"))
        self.assertEqual(moments.clock(3725), "1:02:05")


class SamplerTests(unittest.TestCase):
    def test_start_and_stop(self):
        from bench import gpu

        s = gpu.Sampler(every=0.05)
        s.start()
        time.sleep(0.1)
        s.stop()
        self.assertFalse(s.is_alive())


class RingTests(unittest.TestCase):
    def test_absolute_reads(self):
        import numpy as np

        from bench.live import Ring

        r = Ring(10)
        r.write(np.arange(7, dtype=np.float32))
        r.write(np.arange(7, 13, dtype=np.float32))
        self.assertEqual(r.end, 13)
        self.assertEqual(r.read(8).tolist(), [8, 9, 10, 11, 12])
        self.assertEqual(r.read(0).tolist(), list(range(3, 13)))  # older samples are gone
        self.assertEqual(r.read(13).tolist(), [])


class ConfigTests(unittest.TestCase):
    """config.json is edited by the RAG GUI tab while the bench runs (2026-11-10)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bad_types_fall_back_to_defaults(self):
        out = config.clean({"retention_days": "7", "allowed_clients": "1.2.3.4", "llm_provider": "skynet", "port": True,
                            "vad_threshold": 1, "llm_model": "claude-sonnet-5-5", "future_key": 5})
        self.assertEqual(out["retention_days"], config.DEFAULTS["retention_days"])
        self.assertEqual(out["allowed_clients"], config.DEFAULTS["allowed_clients"])
        self.assertEqual(out["llm_provider"], "ollama")
        self.assertEqual(out["port"], 8791)
        self.assertEqual(out["vad_threshold"], 1)            # int is fine where a float is expected
        self.assertEqual(out["llm_model"], "claude-sonnet-5-5")
        self.assertEqual(out["future_key"], 5)
        # nested dicts merge one level: a partial whisper_models keeps the other model
        merged = config.clean({"whisper_models": {"fast": "small"}, "llm_cloud_models": "x"})
        self.assertEqual(merged["whisper_models"], {"accurate": "large-v3", "fast": "small"})
        self.assertEqual(merged["llm_cloud_models"], config.DEFAULTS["llm_cloud_models"])

    def test_loopback_is_always_allowed(self):
        self.assertEqual(config.clean({"allowed_clients": ["192.168.2.108"]})["allowed_clients"],
                         ["127.0.0.1", "::1", "192.168.2.108"])
        self.assertEqual(config.clean({"allowed_clients": ["::1", "127.0.0.1"]})["allowed_clients"], ["::1", "127.0.0.1"])

    def test_refresh_picks_up_changes_keeps_overrides_and_survives_bad_files(self):
        import os

        self.cfg.override("port", 9000)
        config.write_file(self.cfg.path, {"retention_days": 3, "allowed_clients": ["192.168.2.108"], "llm_provider": "anthropic"})
        os.utime(self.cfg.path, (time.time() + 5, time.time() + 5))
        self.assertTrue(self.cfg.refresh())
        self.assertEqual((self.cfg["retention_days"], self.cfg["llm_provider"], self.cfg["port"]), (3, "anthropic", 9000))
        self.assertFalse(self.cfg.refresh())                 # unchanged file: no reload
        self.cfg.path.write_text("{broken", encoding="utf-8")
        os.utime(self.cfg.path, (time.time() + 10, time.time() + 10))
        self.assertFalse(self.cfg.refresh())
        self.assertEqual(self.cfg["retention_days"], 3)      # last good values stay

    def test_bom_written_by_powershell_is_read(self):
        self.cfg.path.write_bytes("﻿".encode("utf-8") + b'{"retention_days": 9}')
        self.assertEqual(config.load(self.tmp.name)["retention_days"], 9)


class LevelsTests(unittest.TestCase):
    """Loudness of each recorded channel, kept in meta.json so a silent microphone shows up (2026-11-10)."""

    def test_report_and_silence(self):
        import numpy as np

        from bench.live import Levels, silent_channels

        lv = Levels()
        t = np.arange(16000) / 16000
        mic = (np.sin(2 * np.pi * 440 * t) * 16384).astype(np.int16)           # -6 dBFS tone
        quiet = (np.sin(2 * np.pi * 440 * t) * 30).astype(np.int16)            # about -61 dBFS
        lv.add(np.stack([mic, quiet], axis=1))
        rep = lv.report(("L", "R"))
        self.assertAlmostEqual(rep["L"]["peak_dbfs"], -6.0, delta=0.2)
        self.assertAlmostEqual(rep["L"]["rms_dbfs"], -9.0, delta=0.3)
        self.assertGreater(rep["L"]["sound_share"], 0.9)
        self.assertLess(rep["R"]["peak_dbfs"], -50)
        self.assertEqual(silent_channels(rep), ["R"])
        self.assertEqual(lv.report(("L",)).keys(), {"L"})

    def test_thump_at_start_is_not_sound(self):
        import numpy as np

        from bench.live import Levels, silent_channels

        rng = np.random.default_rng(1)
        x = np.round(rng.normal(0, 2.1, 16000 * 3)).astype(np.int16)          # -85 dBFS noise floor
        x[2700:3300] = (np.exp(-np.arange(600) / 560) * 5800).astype(np.int16)  # -15 dBFS thump at 0.17 s
        lv = Levels()
        lv.add(np.stack([x, np.zeros_like(x)], axis=1))
        rep = lv.report(("L",))
        self.assertLess(rep["L"]["peak_dbfs"], -60)
        self.assertEqual(silent_channels(rep), ["L"])

    def test_digital_silence_and_full_scale(self):
        import numpy as np

        from bench.live import Levels, silent_channels

        lv = Levels()
        lv.add(np.zeros((800, 2), np.int16))
        rep = lv.report(("L", "R"))
        self.assertIsNone(rep["L"]["peak_dbfs"])
        self.assertEqual(silent_channels(rep), ["L", "R"])
        # the user's take of 10.11: one click to -15 dBFS, sound in 0.4 % of the time -> still a silent microphone
        self.assertEqual(silent_channels({"L": {"peak_dbfs": -15.1, "sound_share": 0.004, "rms_dbfs": -52.4}}), ["L"])
        self.assertEqual(silent_channels({"L": {"peak_dbfs": -12.0, "sound_share": 0.03, "rms_dbfs": -35.0}}), [])
        lv2 = Levels()
        lv2.add(np.full((8004, 2), -32768, np.int16))   # the first 0.5 s is skipped; abs(int16 -32768) must not overflow
        self.assertEqual(lv2.report(("L",))["L"]["peak_dbfs"], 0.0)
        self.assertEqual(Levels().report(("L",))["L"]["sound_share"], 0.0)


class CaptureInfoTests(unittest.TestCase):
    def test_only_known_short_values_survive(self):
        from bench.capture import clean as clean_capture

        raw = {"mic": {"label": "Headset (Jabra) " * 20, "processing": "standard", "muted": "yes",
                       "settings": {"echoCancellation": "all", "sampleRate": 48000, "deviceId": "secret-id", "latency": 0.0123456,
                                    "groupId": "g", "noiseSuppression": True, "evil": {"x": 1}}},
               "display": {"label": "tab", "settings": {"channelCount": [2]}}, "ctx_rate": 16000, "browser": "Chrome 157",
               "other": "dropped"}
        out = clean_capture(raw)
        self.assertEqual(len(out["mic"]["label"]), 120)
        self.assertIs(out["mic"]["muted"], False)
        self.assertEqual(out["mic"]["settings"], {"echoCancellation": "all", "sampleRate": 48000, "latency": 0.0123,
                                                  "noiseSuppression": True})
        self.assertEqual(out["display"]["settings"], {"channelCount": None})
        self.assertNotIn("other", out)
        self.assertEqual(out["ctx_rate"], 16000)
        devices = clean_capture({"inputs": [{"label": "Mikrofon (USBAudio2.0)", "default": True, "comm": "x", "groupId": "g"}] * 20,
                                 "outputs": "nope", "surface": "monitor", "ec_modes": "true,false,all"})
        self.assertEqual(len(devices["inputs"]), 12)
        self.assertEqual(devices["inputs"][0], {"label": "Mikrofon (USBAudio2.0)", "default": True, "comm": False})
        self.assertNotIn("outputs", devices)
        self.assertEqual((devices["surface"], devices["ec_modes"]), ("monitor", "true,false,all"))
        self.assertIsNone(clean_capture("x"))
        self.assertIsNone(clean_capture({}))


class _FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class _FakeMessages:
    def __init__(self, client, beta):
        self.client, self.beta = client, beta

    def stream(self, **kw):
        self.client.calls.append({**kw, "beta": self.beta})
        a = self.client.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return _FakeStream(a)


class _FakeClient:
    """messages.stream / beta.messages.stream like the Anthropic SDK; answers / exceptions are queued per call."""

    def __init__(self, *answers):
        from types import SimpleNamespace as NS

        self.answers = list(answers)
        self.calls = []
        self.messages = _FakeMessages(self, False)
        self.beta = NS(messages=_FakeMessages(self, True))


def _msg(data, stop="end_turn", model="claude-opus-5-5", input_tokens=1000, output_tokens=200):
    from types import SimpleNamespace as NS

    text = data if isinstance(data, str) else __import__("json").dumps(data, ensure_ascii=False)
    return NS(model=model, stop_reason=stop, content=[NS(type="thinking", thinking="…"), NS(type="text", text=text)],
              usage=NS(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0, iterations=None))


TRANSCRIPT = {"segments": [{"start": 0.0, "ch": "L", "text": "Добрий день, починаємо нараду про антену."},
                           {"start": 4.0, "ch": "R", "text": "Ми домовились відправити зразки до п'ятниці."},
                           {"start": 9.0, "ch": "L", "text": "Ризик: постачальник затримує кабелі на два тижні."}]}


class CloudTests(unittest.TestCase):
    """Paid model (Anthropic) for key moments; no network: the SDK client is replaced (2026-11-10)."""

    def test_extract_validates_quotes_and_counts_cost(self):
        from bench import cloud

        data = {"summary": "Нарада про зразки.", "items": [
            {"kind": "decision", "text": "Зразки до п'ятниці", "who": "", "due": "п'ятниця", "line": 2,
             "quote": "домовились відправити зразки до п'ятниці"},
            {"kind": "risk", "text": "Вигадане", "who": "", "due": "", "line": 3, "quote": "цього в розшифровці немає зовсім"}]}
        client = _FakeClient(_msg(data))
        res = cloud.extract("anthropic", "claude-opus-5-5", TRANSCRIPT, title="Тест", client=client, effort="medium")
        self.assertEqual([i["kind"] for i in res["items"]], ["decision"])
        self.assertEqual(len(res["rejected"]), 1)
        m = res["metrics"]
        self.assertEqual((m["provider"], m["input_tokens"], m["output_tokens"]), ("anthropic", 1000, 200))
        self.assertAlmostEqual(m["cost_usd"], (1000 * 4 + 200 * 20) / 1e6, places=6)
        kw = client.calls[0]
        self.assertTrue(kw["beta"])                                    # Opus: beta client with the refusal fallback
        self.assertEqual((kw["betas"], kw["fallbacks"]), ([cloud.FALLBACK_BETA], "default"))
        self.assertEqual(kw["output_config"]["effort"], "medium")
        self.assertIs(kw["output_config"]["format"]["schema"], cloud.SCHEMA)
        self.assertNotIn("temperature", kw)
        self.assertIn("<<<TRANSCRIPT", kw["messages"][0]["content"])

    def test_haiku_has_no_fallback_and_cut_json_is_bad(self):
        from bench import cloud

        client = _FakeClient(_msg('{"summary": "обрі', stop="max_tokens", model="claude-haiku-5-5"))
        res = cloud.extract("anthropic", "claude-haiku-5-5", TRANSCRIPT, client=client)
        self.assertEqual(res["metrics"]["bad_json"], 1)
        self.assertEqual(res["items"], [])
        self.assertFalse(client.calls[0]["beta"])
        self.assertNotIn("fallbacks", client.calls[0])

    def test_refusal_raises_with_cost(self):
        from bench import cloud

        with self.assertRaises(cloud.CloudError) as ctx:
            cloud.extract("anthropic", "claude-sonnet-5-5", TRANSCRIPT, client=_FakeClient(_msg("", stop="refusal", model="claude-sonnet-5-5")))
        self.assertIn("відмовився", str(ctx.exception))
        self.assertGreater(ctx.exception.usage["cost_usd"], 0)

    def test_fallback_beta_rejected_then_plain_request(self):
        try:
            import anthropic
            import httpx2
        except ImportError:
            self.skipTest("anthropic SDK is not installed here")
        from bench import cloud

        bad = anthropic.BadRequestError("fallbacks not allowed", body=None,
                                        response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")))
        client = _FakeClient(bad, _msg({"summary": "ok", "items": []}))
        res = cloud.extract("anthropic", "claude-opus-5-5", TRANSCRIPT, client=client)
        self.assertEqual(res["summary"], "ok")
        self.assertEqual([c["beta"] for c in client.calls], [True, False])
        self.assertNotIn("fallbacks", client.calls[1])

    def test_sdk_errors_become_readable(self):
        try:
            import anthropic
            import httpx2
        except ImportError:
            self.skipTest("anthropic SDK is not installed here")
        from bench import cloud

        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [(anthropic.AuthenticationError("x", body=None, response=httpx2.Response(401, request=req)), "не прийняв ключ"),
                 (anthropic.APIStatusError("x", body={"type": "error", "error": {"type": "billing_error"}},
                                           response=httpx2.Response(402, request=req)), "немає коштів"),
                 (anthropic.RateLimitError("x", body=None, response=httpx2.Response(429, request=req)), "Ліміт"),
                 (anthropic.APIConnectionError(request=req), "з’єднання")]
        for exc, text in cases:
            with self.subTest(exc=type(exc).__name__), self.assertRaises(cloud.CloudError) as ctx:
                cloud.extract("anthropic", "claude-haiku-5-5", TRANSCRIPT, client=_FakeClient(exc))
            self.assertIn(text, str(ctx.exception))

    def test_no_key_and_unknown_provider(self):
        from unittest.mock import patch

        from bench import cloud

        with patch.object(cloud.keystore, "get", return_value=""), self.assertRaises(cloud.CloudError) as ctx:
            cloud.extract("anthropic", "claude-opus-5-5", TRANSCRIPT)
        self.assertIn("не збережено", str(ctx.exception))
        with self.assertRaises(cloud.CloudError):
            cloud.extract("openai", "gpt", TRANSCRIPT, client=_FakeClient())

    def test_choices_follow_key_and_config(self):
        from unittest.mock import patch

        from bench import cloud

        cfg = dict(config.DEFAULTS)
        with patch.object(cloud.keystore, "get", return_value="sk-ant-x"):
            offered = cloud.choices(cfg, ["qwen3.5:9b-q4_K_M"])
        self.assertEqual(offered[0]["value"], "ollama:qwen3.5:9b-q4_K_M")
        self.assertTrue(all(c["ready"] for c in offered))
        self.assertTrue(any(c["value"] == "anthropic:claude-opus-5-5" and "≈ $" in c["label"] for c in offered))
        self.assertEqual(cloud.default_choice({**cfg, "llm_provider": "anthropic"}, offered), "anthropic:claude-opus-5-5")
        with patch.object(cloud.keystore, "get", return_value=""):
            offered = cloud.choices(cfg, ["qwen3.5:9b-q4_K_M"])
        # no key: cloud models are listed but not ready, and the default falls back to the local model
        self.assertFalse(any(c["ready"] for c in offered if c["cloud"]))
        self.assertEqual(cloud.default_choice({**cfg, "llm_provider": "anthropic", "llm_model": "claude-haiku-5-5"}, offered),
                         "ollama:qwen3.5:9b-q4_K_M")

    def test_one_refused_piece_keeps_the_others(self):
        from bench import cloud

        good = {"summary": "Частина 1.", "items": [{"kind": "decision", "text": "Зразки", "who": "", "due": "", "line": 2,
                                                    "quote": "домовились відправити зразки до п'ятниці"}]}
        client = _FakeClient(_msg(good, model="claude-haiku-5-5"), _msg("", stop="refusal", model="claude-haiku-5-5"))
        res = cloud.extract("anthropic", "claude-haiku-5-5", TRANSCRIPT, client=client, chunk_chars=150)
        self.assertEqual(res["metrics"]["refused"], 1)
        self.assertEqual(len(res["items"]), 1)

    def test_unknown_price_is_not_zero(self):
        from types import SimpleNamespace as NS

        from bench import cloud

        msg = NS(model="claude-mystery-9", stop_reason="end_turn", content=[],
                 usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0, iterations=None))
        self.assertIsNone(cloud._usage(msg, "claude-mystery-9")["cost_usd"])
        cfg = {**config.DEFAULTS, "llm_cloud_models": {"anthropic": ["claude-opus-5-5", "claude-mystery-9"]}}
        from unittest.mock import patch

        with patch.object(cloud.keystore, "get", return_value="k"):
            values = [c["value"] for c in cloud.choices(cfg, [])]
        self.assertEqual(values, ["anthropic:claude-opus-5-5"])          # no price, no effort -> not offered

    def test_haiku_long_prompt_price(self):
        from bench import cloud

        short = cloud.cost_usd("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 0})
        long = cloud.cost_usd("claude-haiku-5-5", {"input_tokens": 100_001, "output_tokens": 0})
        self.assertAlmostEqual(short, 0.01)
        self.assertAlmostEqual(long, 100_001 * 0.50 / 1e6)


class CloudSdkTests(unittest.TestCase):
    """The real anthropic SDK over a mock HTTP transport: wire shapes of fallbacks, broken streams and errors."""

    def setUp(self):
        try:
            import anthropic  # noqa: F401
            import httpx2  # noqa: F401
        except ImportError:
            self.skipTest("anthropic SDK is not installed here")

    @staticmethod
    def sse(events):
        import json

        return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()

    def client(self, *responses):
        import anthropic
        import httpx2

        queue = list(responses)
        self.sent = []

        def handler(request):
            import json

            self.sent.append({"headers": dict(request.headers), "body": json.loads(request.content or b"{}")})
            r = queue.pop(0)
            return r() if callable(r) else r
        return anthropic.Anthropic(api_key="sk-ant-test", max_retries=0, base_url="https://api.anthropic.com",
                                   http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))

    def stream(self, events):
        import httpx2

        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=self.sse(events))

    @staticmethod
    def start(model, usage_extra=None):
        u = {"input_tokens": 30000, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        u.update(usage_extra or {})
        return {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                                      "content": [], "stop_reason": None, "stop_sequence": None, "usage": u}}

    @staticmethod
    def text(i, t):
        return [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": t}},
                {"type": "content_block_stop", "index": i}]

    @staticmethod
    def delta(reason, out, iterations=None):
        u = {"output_tokens": out}
        if iterations is not None:
            u["iterations"] = iterations
        return {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": u}

    ANSWER = '{"summary": "S", "items": []}'

    def test_normal_answer_through_the_beta_client(self):
        from bench import cloud

        c = self.client(self.stream([self.start("claude-opus-5-5"), *self.text(0, self.ANSWER),
                                     self.delta("end_turn", 1500), {"type": "message_stop"}]))
        data, usage = cloud.call(c, "claude-opus-5-5", "SYS", "USER", "medium")
        self.assertEqual(data, {"summary": "S", "items": []})
        self.assertAlmostEqual(usage["cost_usd"], (30000 * 4 + 1500 * 20) / 1e6)
        self.assertIn(cloud.FALLBACK_BETA, self.sent[0]["headers"].get("anthropic-beta", ""))
        body = self.sent[0]["body"]
        self.assertEqual((body["fallbacks"], body["output_config"]["effort"]), ("default", "medium"))
        self.assertNotIn("temperature", body)

    def test_mid_stream_fallback_is_priced_per_attempt(self):
        from bench import cloud

        half = len(self.ANSWER) // 2
        iters = [{"type": "message", "model": "claude-opus-5-5", "input_tokens": 30000, "output_tokens": 400,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                 {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 30050, "output_tokens": 800,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]
        events = [self.start("claude-opus-5-5"), *self.text(0, self.ANSWER[:half]),
                  {"type": "content_block_start", "index": 1, "content_block": {
                      "type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"},
                      "trigger": {"type": "refusal", "category": "frontier_llm"}}},
                  {"type": "content_block_stop", "index": 1}, *self.text(2, self.ANSWER[half:]),
                  self.delta("end_turn", 800, iters), {"type": "message_stop"}]
        data, usage = cloud.call(self.client(self.stream(events)), "claude-opus-5-5", "SYS", "USER", "medium")
        self.assertEqual(data["summary"], "S")
        self.assertTrue(usage["fallback"])
        self.assertEqual(usage["model"], "claude-opus-4-8")
        self.assertAlmostEqual(usage["cost_usd"], (30000 * 4 + 400 * 20 + 30050 * 5 + 800 * 25) / 1e6)

    def test_stream_broken_after_200(self):
        import httpx2

        from bench import cloud

        sse = self.sse([self.start("claude-sonnet-5-5")])

        class Drop(httpx2.SyncByteStream):
            def __iter__(self):
                yield sse
                raise httpx2.RemoteProtocolError("peer closed connection")

            def close(self):
                pass

        c = self.client(lambda: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Drop()))
        with self.assertRaises(cloud.CloudError) as ctx:
            cloud.call(c, "claude-sonnet-5-5", "SYS", "USER", "low")
        self.assertIn("обірвалось", str(ctx.exception))

    def test_error_event_inside_the_stream_and_low_balance(self):
        import json

        import httpx2

        from bench import cloud

        body = self.sse([self.start("claude-haiku-5-5")]) + \
            f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': 'x'}})}\n\n".encode()
        c = self.client(httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body))
        with self.assertRaises(cloud.CloudError) as ctx:
            cloud.call(c, "claude-haiku-5-5", "SYS", "USER", "medium")
        self.assertIn("обірвалась", str(ctx.exception))
        low = {"type": "error", "error": {"type": "invalid_request_error",
                                          "message": "Your credit balance is too low to access the Anthropic API."}}
        c = self.client(httpx2.Response(400, json=low))
        with self.assertRaises(cloud.CloudError) as ctx:
            cloud.call(c, "claude-haiku-5-5", "SYS", "USER", "medium")
        self.assertIn("немає коштів", str(ctx.exception))


class CloudJobTests(unittest.TestCase):
    """The job checks the consent again right before the text leaves the RAG PC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)
        self.store = store.Store(self.cfg)
        self.sid, folder, _ = self.store.create("upload", "Нарада")
        store.write_json(folder / "transcript.json", TRANSCRIPT)

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, params):
        import asyncio

        from bench.jobs import Jobs

        jobs = Jobs(self.cfg, self.store, live_busy=lambda: False)
        st = {}
        asyncio.run(jobs._moments(self.sid, params, st))
        return st

    def test_without_consent_nothing_is_sent(self):
        from unittest.mock import patch

        from bench import cloud
        from bench.jobs import JobError

        with patch.object(cloud, "extract") as ext, self.assertRaises(JobError):
            self.run_job({"provider": "anthropic", "model": "claude-opus-5-5", "lang": "uk"})
        ext.assert_not_called()

    def test_with_consent_result_and_failed_run_are_kept(self):
        from unittest.mock import patch

        from bench import cloud
        from bench.jobs import JobError

        self.store.update_meta(self.sid, cloud_consent={"provider": "anthropic", "model": "claude-opus-5-5"})
        ok = {"model": "claude-opus-5-5", "provider": "anthropic", "lang": "uk", "summary": "", "items": [], "rejected": [],
              "metrics": {"provider": "anthropic", "model": "claude-opus-5-5", "cost_usd": 0.12}}
        with patch.object(cloud, "extract", return_value=ok) as ext:
            self.run_job({"provider": "anthropic", "model": "claude-opus-5-5", "lang": "de"})
        self.assertEqual(ext.call_args.kwargs["out_lang"], "de")
        folder = self.store.path(self.sid)
        self.assertEqual(store.read_json(folder / "moments.json")["metrics"]["cost_usd"], 0.12)
        with patch.object(cloud, "extract", side_effect=cloud.CloudError("Ліміт", {"cost_usd": 0.05})), self.assertRaises(JobError):
            self.run_job({"provider": "anthropic", "model": "claude-opus-5-5", "lang": "uk"})
        runs = store.read_json(folder / "moments_runs.json")
        self.assertEqual([r.get("error") for r in runs], [None, "Ліміт"])
        self.assertEqual(runs[-1]["cost_usd"], 0.05)


class LiveStopTests(unittest.TestCase):
    """stop() keeps a rename made during the recording and stores channel levels (bugs found 10.11)."""

    def test_rename_survives_and_levels_saved(self):
        import numpy as np

        from bench.live import LiveSession

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = config.load(tmp.name)
        st = store.Store(cfg)
        sid, folder, meta = st.create("live", "Старт", mode="both")
        sess = LiveSession(cfg, folder, meta, None, lambda m: None)
        tone = (np.sin(np.arange(16000) / 16000 * 2 * np.pi * 300) * 12000).astype(np.int16)
        sess.feed(np.stack([tone, np.zeros_like(tone)], axis=1).tobytes())
        st.update_meta(sid, title="Перейменовано під час запису")
        sess.stop()
        saved = st.meta(sid)
        self.assertEqual(saved["title"], "Перейменовано під час запису")
        self.assertEqual(saved["silent"], ["R"])
        self.assertGreater(saved["levels"]["L"]["sound_share"], 0.5)
        self.assertEqual(saved["duration"], 1.0)


class ReviewFixTests(unittest.TestCase):
    """Fixes after the review of 10.11.2026 (store, worker, config, levels, default model)."""

    def setUp(self):
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_json_retries_while_a_reader_holds_the_file(self):
        import os
        from unittest.mock import patch

        path = self.root / "progress.json"
        real, calls = os.replace, []

        def busy_twice(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, "Access is denied")
            return real(src, dst)

        with patch.object(store.os, "replace", side_effect=busy_twice), patch.object(store.time, "sleep"):
            store.write_json(path, {"a": 1})
        self.assertEqual(store.read_json(path), {"a": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual([f.name for f in self.root.iterdir()], ["progress.json"])  # no temp file left behind

    def test_write_json_gives_up_after_the_retries(self):
        from unittest.mock import patch

        with patch.object(store.os, "replace", side_effect=PermissionError(13, "denied")), \
             patch.object(store.time, "sleep") as sleep, self.assertRaises(PermissionError):
            store.write_json(self.root / "x.json", {})
        self.assertEqual(sleep.call_count, store.REPLACE_TRIES - 1)

    def test_progress_that_cannot_be_written_does_not_stop_the_transcription(self):
        from unittest.mock import patch

        from bench import worker

        prog = worker.Progress(self.root)
        with patch.object(worker, "write_json", side_effect=PermissionError(13, "denied")):
            prog.set("transcribe", "50 %")
        self.assertEqual(prog.state["text"], "50 %")

    def test_nested_values_are_typed_and_reported(self):
        problems = []
        out = config.clean({"whisper_models": {"fast": None, "accurate": "large-v3"}, "vram_needed_mb": {"large-v3": "x"},
                            "llm_cloud_models": {"anthropic": ["claude-haiku-5-5", 3]},
                            "allowed_clients": ["192.168.2.108", None], "retention_days": "7"}, problems)
        self.assertEqual(out["whisper_models"], config.DEFAULTS["whisper_models"])
        self.assertEqual(out["vram_needed_mb"], config.DEFAULTS["vram_needed_mb"])
        self.assertEqual(out["llm_cloud_models"], config.DEFAULTS["llm_cloud_models"])
        self.assertEqual(out["allowed_clients"], ["127.0.0.1", "::1", "192.168.2.108"])  # only the stray null is dropped
        self.assertEqual(out["retention_days"], config.DEFAULTS["retention_days"])
        self.assertEqual(len(problems), 1)
        for key in ("whisper_models", "vram_needed_mb", "llm_cloud_models", "allowed_clients", "retention_days"):
            self.assertIn(key, problems[0])
        self.assertEqual(config.clean({"retention_days": 7}, problems)["retention_days"], 7)
        self.assertEqual(len(problems), 1)                    # a good file adds nothing

    def test_a_broken_file_at_start_up_does_not_stop_the_bench(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "config.json").write_text('{"retention_days": 3,}', encoding="utf-8")
        cfg = config.load(self.root)
        self.assertEqual(cfg["retention_days"], config.DEFAULTS["retention_days"])
        self.assertEqual(cfg["allowed_clients"], ["127.0.0.1", "::1"])
        self.assertIn("config.json не прочитано", cfg.error)

    def test_refresh_reports_ignored_values(self):
        import os

        cfg = config.load(self.root)
        self.assertIsNone(cfg.error)
        config.write_file(cfg.path, {"retention_days": "x", "allowed_clients": ["192.168.2.108"]})
        os.utime(cfg.path, (time.time() + 5, time.time() + 5))
        self.assertTrue(cfg.refresh())
        self.assertIn("retention_days", cfg.error)
        self.assertEqual(cfg["allowed_clients"], ["127.0.0.1", "::1", "192.168.2.108"])
        config.write_file(cfg.path, {"retention_days": 9})
        os.utime(cfg.path, (time.time() + 10, time.time() + 10))
        self.assertTrue(cfg.refresh())
        self.assertIsNone(cfg.error)

    def test_silence_is_judged_by_seconds_of_sound(self):
        import numpy as np

        from bench.live import Levels, silent_channels

        # an hour-long meeting where the person said a greeting and one question: 0.1 % of the time, 5 s of sound
        self.assertEqual(silent_channels({"L": {"peak_dbfs": -12.0, "sound_share": 0.0014, "sound_s": 5.0}}), [])
        self.assertEqual(silent_channels({"L": {"peak_dbfs": -15.0, "sound_share": 0.004, "sound_s": 0.1}}), ["L"])  # a click
        lv = Levels()
        t = np.arange(16000 * 2) / 16000
        lv.add(np.stack([(np.sin(2 * np.pi * 440 * t) * 16384).astype(np.int16), np.zeros(len(t), np.int16)], axis=1))
        rep = lv.report(("L", "R"))
        self.assertAlmostEqual(rep["L"]["sound_s"], 1.5, delta=0.1)   # 2 s minus the skipped first 0.5 s
        self.assertEqual(silent_channels(rep), ["R"])

    def test_the_default_never_falls_back_to_a_paid_model(self):
        from unittest.mock import patch

        from bench import cloud

        cfg = dict(config.DEFAULTS)
        with patch.object(cloud.keystore, "get", return_value="sk-ant-x"):
            offered = cloud.choices(cfg, [])                  # Ollama restarting: no local model offered right now
        self.assertTrue(any(c["ready"] for c in offered))
        self.assertEqual(cloud.default_choice(cfg, offered), "")
        self.assertEqual(cloud.default_choice({**cfg, "llm_provider": "anthropic", "llm_model": "claude-sonnet-5-5"}, offered),
                         "anthropic:claude-sonnet-5-5")
        # a paid model that is not offered any more -> another one of the SAME provider, chosen in the settings
        self.assertEqual(cloud.default_choice({**cfg, "llm_provider": "anthropic", "llm_model": "claude-opus-4-1"}, offered),
                         "anthropic:claude-opus-5-5")


class ServerTests(unittest.IsolatedAsyncioTestCase):
    """The aiohttp app itself: consent for the cloud and a live stop that fails (review 10.11)."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        from bench import server

        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)
        self.cfg.override("standalone_enabled", True)       # these tests use the stage-0 test bench
        self.server = server
        self.app = server.create_app(self.cfg)
        self.client = TestClient(TestServer(self.app, host="127.0.0.1"))
        await self.client.start_server()
        self.ctx = self.app[server.APP_KEY]
        self.headers = {"X-Bench": "1", "Origin": str(self.client.make_url("/"))[:-1]}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_cloud_consent_must_name_this_recording(self):
        from unittest.mock import patch

        from bench import keystore

        sid, folder, _ = self.ctx["store"].create("upload", "Нарада")
        store.write_json(folder / "transcript.json", TRANSCRIPT)
        other, _, _ = self.ctx["store"].create("upload", "Інша")
        url = f"/api/sessions/{sid}/moments"
        body = {"choice": "anthropic:claude-haiku-5-5", "lang": "uk"}
        queued = {"kind": "moments", "state": "queued", "text": "У черзі…"}
        with patch.object(keystore, "get", return_value="sk-ant-x"), \
             patch.object(self.ctx["jobs"], "submit", return_value=queued) as submit:
            for consent in (True, "1", other, None):
                r = await self.client.post(url, json={**body, "cloud_consent": consent}, headers=self.headers)
                self.assertEqual(r.status, 400, consent)
            submit.assert_not_called()
            self.assertNotIn("cloud_consent", self.ctx["store"].meta(sid))
            r = await self.client.post(url, json={**body, "cloud_consent": sid}, headers=self.headers)
            self.assertEqual(r.status, 200, await r.text())
            submit.assert_called_once()
        consent = self.ctx["store"].meta(sid)["cloud_consent"]
        self.assertEqual((consent["provider"], consent["model"]), ("anthropic", "claude-haiku-5-5"))
        with patch.object(keystore, "get", return_value=""):  # no key -> refused before anything is queued
            r = await self.client.post(url, json={**body, "cloud_consent": sid}, headers=self.headers)
        self.assertEqual(r.status, 400)

    async def test_a_failing_live_stop_still_frees_the_gpu_and_tells_the_page(self):
        import asyncio
        import json
        from unittest.mock import patch

        from aiohttp import WSMsgType

        from bench import live

        class FakeVad:
            def accept_waveform(self, x):
                pass

            def empty(self):
                return True

            def is_speech_detected(self):
                return False

            def flush(self):
                pass

        class FakeWhisper:
            name, device, load_s, closed = "whisper", "cuda", 0.1, False

            def transcribe(self, samples, language=None):
                return "", None

            def close(self):
                FakeWhisper.closed = True

        async def fake_engine(ctx, name):
            return FakeWhisper()

        real_write = live.write_json

        def failing_write(path, data):  # as on Windows while another handle reads meta.json
            if str(path).endswith("meta.json"):
                raise PermissionError(13, "Access is denied (meta.json open elsewhere)")
            return real_write(path, data)

        with patch.object(live, "make_vad", lambda cfg: (FakeVad(), 512)), patch.object(self.server, "_engine", fake_engine), \
             patch.object(live, "write_json", failing_write):
            ws = await self.client.ws_connect("/ws", headers={"Origin": self.headers["Origin"]})
            await ws.send_str(json.dumps({"type": "start", "consent": True, "engine": "whisper", "mode": "both", "title": "x"}))
            got = []
            while True:
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type != WSMsgType.TEXT:
                    break
                m = json.loads(msg.data)
                got.append(m["type"])
                if m["type"] == "ready":
                    await ws.send_bytes(b"\x00\x00" * 3200)
                    await ws.send_str(json.dumps({"type": "stop"}))
            await ws.close()
        self.assertIn("ready", got)
        self.assertEqual(got[-1], "error")                     # the page leaves «Зберігаю…» with a reason
        self.assertNotIn("saved", got)
        self.assertIsNone(self.ctx["live_engine"])             # transcriptions do not wait for a live session forever
        self.assertIsNone(self.ctx["live_sid"])
        self.assertTrue(FakeWhisper.closed)

    async def test_static_files_are_revalidated(self):
        r = await self.client.get("/static/capture-worklet.js")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")


# ==== stage 1: the recording window <-> Minerva (0.3.0) ==============================================================
UID = "0f8fad5b-d9cb-469f-a165-70867728950e"
PUSH = "tok_" + "A" * 40
KEY = "ab" * 32
CONSENT = {"announced": True, "all_agreed": True, "lang": "de", "at": "2026-11-10T10:00:00+01:00"}
TRANSCRIPT_MN = {"model": "large-v3",
                 "languages": {"L": {"language": "uk", "probability": 0.98}, "R": {"language": "de", "probability": 0.91}},
                 "segments": [{"start": 0.0, "end": 3.5, "ch": "L", "text": "Добрий день, починаємо нараду про антену."},
                              {"start": 4.0, "end": 8.0, "ch": "R", "text": "Wir liefern die Muster bis Freitag."},
                              {"start": 9.0, "end": 12.0, "ch": "L", "text": "Ризик: постачальник затримує кабелі на два тижні."}]}
MOMENTS_MN = {"model": "qwen3.5:9b-q4_K_M", "lang": "uk", "summary": "Нарада про зразки й кабелі.", "rejected": [{"x": 1}],
              "items": [{"kind": "promise", "text": "Зразки до п'ятниці", "who": "Клієнт", "due": "п'ятниця", "line": 2,
                         "quote": "Wir liefern die Muster bis Freitag", "start": 4.0, "speaker": "Співрозмовники"},
                        {"kind": "risk", "text": "Затримка кабелів", "who": "", "due": "", "line": 99, "quote": "немає такого рядка"}]}


def redeem_answer(meeting=12, retention="text_only", **extra):
    m = {"id": meeting, "uid": UID, "title": "Нарада P-2026-001 · 10.11.2026", "project_code": "P-2026-001",
         "retention": retention, "retention_days": 365, "audio_days": 30, "lang_l": "uk", "lang_r": "de",
         "consent_texts": {"de": {"ask": "Wir möchten dieses Gespräch aufzeichnen. Sind alle einverstanden?",
                                  "after": "Danke, die Aufnahme läuft jetzt."},
                           "en": {"ask": "We would like to record this meeting.", "after": "Thank you, we are recording now."},
                           "uk": {"ask": "Ми хочемо записати нараду.", "after": "Дякую, запис почався."}},
         "consent_lang": "de", "hotwords": "Sevskiy, P-2026-001, AN220213-01C",
         "return_url": "https://minerva.test/admin/meetings/meeting/12/page/", **extra}
    return {"meeting": m, "push_token": PUSH}


def remote(status, code, coded=True, retry_after=None):
    from bench import minerva

    return minerva.RemoteError(status, code, retryable=not coded, retry_after=retry_after, coded=coded)


class FakeMinerva:
    """Minerva as the link sees it: answers (or exceptions) queued per call kind; every call is recorded."""

    def __init__(self):
        self.calls = []
        self.answers = {}

    def _answer(self, key, default):
        queue = self.answers.get(key)
        if queue:
            a = queue.pop(0)
            if callable(a):                                  # e.g. a slow answer: lambda: (time.sleep(1), {...})[1]
                a = a()
            if isinstance(a, Exception):
                raise a
            return a
        return default

    def redeem(self, ticket):
        self.calls.append(("redeem", None, {"ticket": ticket}, None))
        return self._answer("redeem", redeem_answer())

    def push(self, meeting, kind, token, body, idem=None, timeout=None):
        __import__("json").dumps(body)                       # what goes over the wire must be plain JSON
        self.calls.append((kind, meeting, body, token))
        return self._answer(kind, {"ok": True, "complete": True} if kind == "transcript" else {"ok": True})

    def sync(self, body):
        self.calls.append(("sync", None, body, None))
        return self._answer("sync", {"sessions": []})

    def ping(self):
        self.calls.append(("ping", None, None, None))
        return self._answer("ping", {"ok": True, "scopes": ["meetings:write"]})


class FakeJobs:
    def __init__(self):
        from types import SimpleNamespace

        self.status, self.submitted, self.cancelled = {}, [], []
        self.queue = SimpleNamespace(qsize=lambda: 0)

    def get(self, sid):
        return self.status.get(sid)

    def busy(self, sid):
        st = self.status.get(sid)
        return bool(st and st["state"] in ("queued", "running"))

    def submit(self, sid, kind, params):
        self.submitted.append((sid, kind, params))
        self.status[sid] = {"kind": kind, "state": "queued", "queued": time.time(), "params": params}
        return self.status[sid]

    def position(self, sid):
        return 1 if self.busy(sid) else 0

    def cancel(self, sid):
        self.cancelled.append(sid)
        if sid in self.status:
            self.status[sid]["state"] = "cancelled"


def write_wav(path, seconds=1.0):
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 2 * int(16000 * seconds))


class MinervaClientTests(unittest.TestCase):
    """bench/minerva.py: one origin, allow-listed paths, no proxies, no redirects, errors by JSON code."""

    def test_origin_and_return_url(self):
        from bench import minerva

        self.assertEqual(minerva.origin("https://Sevskiy.Minerva-BI.com/admin/"), "https://sevskiy.minerva-bi.com")
        self.assertEqual(minerva.origin(" https://minerva.test:8443 "), "https://minerva.test:8443")
        self.assertEqual(minerva.origin("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        for bad in ("", "http://sevskiy.minerva-bi.com", "https://user:pw@minerva.test", "https://minerva.test/?a=1",
                    "https://minerva.test/#x", "ftp://minerva.test", "minerva.test", "https://minerva.test:99999"):
            self.assertIsNone(minerva.origin(bad), bad)
        self.assertEqual(minerva.https_url("https://minerva.test/x/"), "https://minerva.test/x/")
        for bad in ("http://minerva.test/", "javascript:alert(1)", "https://u:p@minerva.test/", "/relative"):
            self.assertEqual(minerva.https_url(bad), "", bad)

    def test_paths_are_allow_listed_and_the_key_is_hidden(self):
        from bench import minerva

        c = minerva.Client("https://minerva.test", KEY)
        self.assertNotIn(KEY, repr(c))
        for bad in ("/api/v1/knowledge/changes/", "/api/v1/meetings/12/audio/", "/api/v1/meetings/0/status/",
                    "/api/v1/meetings/x/status/", "/api/v1/meetings/redeem", "//evil/api/v1/meetings/sync/"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.call("POST", bad, {})
        with self.assertRaises(ValueError):
            minerva.Client("http://minerva.test", KEY)          # https only (plain http just for this PC)
        with self.assertRaises(ValueError):
            minerva.Client("https://minerva.test", "")

    def test_classification_by_json_code(self):
        from bench import minerva

        cases = [(remote(410, "meeting_gone"), "gone"), (remote(404, "meeting_unknown"), "unknown"),
                 (remote(403, "push_forbidden"), "blocked"), (remote(409, "session_mismatch"), "blocked"),
                 (remote(400, "invalid_request"), "drop"), (remote(413, "payload_too_large"), "too_large"),
                 (remote(413, "http_413", coded=False), "too_large"), (remote(401, "unauthorized"), "pause"),
                 (remote(403, "forbidden_scope"), "pause"), (remote(403, "shop_key_forbidden"), "pause"),
                 (remote(409, "transcript_incomplete"), "retry"), (remote(409, "transcript_exists"), "done"),
                 (remote(409, "consent_missing"), "drop"),
                 (remote(429, "rate_limited", retry_after=60), "retry"), (remote(500, "internal_error"), "retry"),
                 (remote(502, "http_502", coded=False), "retry"), (remote(404, "http_404", coded=False), "retry"),
                 (remote(0, "network", coded=False), "retry"), (remote(0, "tls", coded=False), "retry")]
        for err, kind in cases:
            with self.subTest(code=err.code, status=err.status):
                self.assertEqual(minerva.classify(err), kind)
                self.assertNotIn(KEY, minerva.text_of(err))

    def test_parse_redeem(self):
        from bench import minerva

        b = minerva.parse_redeem(redeem_answer(return_url="http://minerva.test/x", retention="weird", lang_l="pl",
                                               consent_texts={"de": "Text", "fr": "Texte", "uk": 5}))
        self.assertEqual((b["meeting"], b["uid"], b["push_token"]), (12, UID, PUSH))
        self.assertEqual((b["retention"], b["lang_l"], b["return_url"]), ("text_only", "", ""))
        self.assertEqual(b["consent_texts"], {"de": {"ask": "Text", "after": ""}})
        b = minerva.parse_redeem(redeem_answer())
        self.assertEqual(b["consent_texts"]["de"]["after"], "Danke, die Aufnahme läuft jetzt.")
        for bad in ({"meeting": {"id": "12", "uid": UID}, "push_token": PUSH}, {"meeting": {"id": 12, "uid": "x"}, "push_token": PUSH},
                    {"meeting": {"id": 12, "uid": UID}, "push_token": "short"}, {"push_token": PUSH}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                minerva.parse_redeem(bad)


class MinervaHttpTests(unittest.TestCase):
    """The real urllib client against a local HTTP server: headers, no proxy, redirects refused, answers capped."""

    def setUp(self):
        import http.server
        import threading

        test = self
        self.requests = []
        self.reply = (200, {"Content-Type": "application/json"}, b'{"ok": true, "complete": true}')

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                test.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                      "body": self.rfile.read(n) if n else b""})
                status, headers, body = test.reply
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def client(self):
        from bench import minerva

        return minerva.Client(self.base, KEY, timeout=5, version="0.3.0")

    def test_headers_body_and_no_proxy(self):
        from unittest.mock import patch

        body = {"uid": UID, "segments": [{"n": 1, "text": "Добрий день"}]}
        with patch.dict("os.environ", {"HTTP_PROXY": "http://10.255.255.1:9", "http_proxy": "http://10.255.255.1:9",
                                       "NO_PROXY": "", "no_proxy": ""}):
            answer = self.client().push(12, "transcript", PUSH, body, idem="u:transcript:x:1")
        self.assertEqual(answer, {"ok": True, "complete": True})
        r = self.requests[0]
        self.assertEqual((r["method"], r["path"]), ("POST", "/api/v1/meetings/12/transcript/"))
        h = {k.lower(): v for k, v in r["headers"].items()}
        self.assertEqual(h["authorization"], "Token " + KEY)
        self.assertEqual(h["x-meeting-token"], PUSH)
        self.assertEqual(h["idempotency-key"], "u:transcript:x:1")
        self.assertEqual(h["content-type"], "application/json")
        self.assertIn("Добрий день".encode(), r["body"])             # ensure_ascii=False: Cyrillic is 2 bytes, not 6
        self.client().ping()
        self.assertEqual((self.requests[1]["method"], self.requests[1]["path"]), ("GET", "/api/v1/meetings/ping/"))
        self.assertNotIn("x-meeting-token", {k.lower() for k in self.requests[1]["headers"]})

    def test_errors(self):
        from bench import minerva

        cases = [
            ((410, {"Content-Type": "application/json"}, json.dumps({"code": "meeting_gone", "message": "x", "retryable": False}).encode()),
             "meeting_gone", True),
            ((429, {"Retry-After": "120", "Content-Type": "application/json"},
              b'{"code": "rate_limited", "retryable": true, "details": {"retry_after_seconds": 90}}'), "rate_limited", True),
            ((502, {"Content-Type": "text/html"}, b"<html>Bad Gateway</html>"), "http_502", False),
            ((302, {"Location": "https://elsewhere.test/"}, b""), "redirect", False),
            ((200, {"Content-Type": "text/html"}, b"<html>login</html>"), "bad_answer", False),
            ((200, {"Content-Type": "application/json"}, b'"just text"'), "bad_answer", False),
            ((200, {"Content-Type": "application/json"}, b'{"x": "' + b"a" * (1 << 20) + b'"}'), "too_big", False),
        ]
        for reply, code, coded in cases:
            self.reply = reply
            with self.subTest(code=code), self.assertRaises(minerva.RemoteError) as ctx:
                self.client().sync({"sessions": []})
            self.assertEqual((ctx.exception.code, ctx.exception.coded), (code, coded))
            self.assertNotIn(KEY, str(ctx.exception) + ctx.exception.message)
            if code == "rate_limited":
                self.assertEqual(ctx.exception.retry_after, 90)
        self.reply = (200, {"Content-Type": "application/json"}, b'[{"meeting": 12, "session": "s", "action": "keep"}]')
        self.assertEqual(self.client().sync({"sessions": []}), {"sessions": [{"meeting": 12, "session": "s", "action": "keep"}]})
        with self.assertRaises(minerva.RemoteError):
            self.client().ping()                                    # a list is accepted only as the sync answer

    def test_no_answer(self):
        import socket

        from bench import minerva

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(minerva.RemoteError) as ctx:
            minerva.Client(f"http://127.0.0.1:{port}", KEY, timeout=3).ping()
        self.assertEqual((ctx.exception.status, ctx.exception.code), (0, "network"))
        self.assertEqual(minerva.classify(ctx.exception), "retry")


class MinervaBuilderTests(unittest.TestCase):
    """Payloads are built from JSON files only: segments numbered n, parts ≤ 512 KB encoded, no audio fields."""

    def test_segments_are_numbered_validated_and_capped(self):
        from unittest.mock import patch

        from bench import minerva

        tr = {"segments": [{"start": -1, "end": -5, "ch": "X", "text": "  " + "я" * 2500}, {"start": 2, "end": 3, "ch": "R", "text": "ok"},
                           {"start": "bad", "text": None}]}
        segs, cut = minerva.segments_of(tr)
        self.assertFalse(cut)
        self.assertEqual([s["n"] for s in segs], [1, 2, 3])
        self.assertEqual((segs[0]["start"], segs[0]["end"], segs[0]["ch"], len(segs[0]["text"])), (0.0, 0.0, "M", 2000))
        self.assertEqual(segs[2], {"n": 3, "start": 0.0, "end": 0.0, "ch": "M", "text": ""})
        with patch.object(minerva, "TOTAL_CHARS_MAX", 2001):
            segs, cut = minerva.segments_of(tr)
        self.assertTrue(cut)
        self.assertEqual(len(segs), 1)

    def test_parts_stay_under_the_byte_limit(self):
        from bench import minerva

        segs = [{"n": i, "start": i * 2.0, "end": i * 2.0 + 1.5, "ch": "LR"[i % 2], "text": "Привіт, як справи сьогодні? " * 60}
                for i in range(1, 400)]
        base = {"uid": UID, "session": "20261110-100000-abcd", "upload_id": "u" * 32, "languages": {}, "model": "large-v3",
                "duration": 3600.0, "marks": [], "total_segments": len(segs), "truncated": False}
        limit = 64 * 1024
        parts = minerva.split_parts(base, segs, limit)
        self.assertGreater(len(parts), 5)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], len(segs))
        for (a, b), (c, _) in zip(parts, parts[1:]):
            self.assertEqual(b, c)                                   # contiguous, nothing lost
        for k, (a, b) in enumerate(parts, 1):
            body = {**base, "part": k, "parts": len(parts), "segments": segs[a:b]}
            self.assertLessEqual(minerva.encoded_size(body), limit)
        self.assertEqual(minerva.split_parts(base, [], limit), [])

    def test_moments_marks_levels(self):
        from bench import minerva

        body = minerva.moments_payload({"uid": UID, "session": "s"}, MOMENTS_MN, TRANSCRIPT_MN)
        self.assertEqual(len(body["items"]), 1)                      # the item with line 99 has no segment
        it = body["items"][0]
        self.assertEqual((it["n"], it["line"], it["start"], it["end"], it["ch"]), (2, 2, 4.0, 8.0, "R"))
        self.assertEqual((body["provider"], body["rejected"], body["lang"]), ("ollama", 1, "uk"))
        marks = minerva.marks_of([{"t": 5, "note": ""}, {"t": 6, "note": "consent_ok"}, {"t": 7, "note": "пауза", "kind": "auto"},
                                  {"t": 8, "note": "x", "kind": "consent_join"}, {"t": -3, "note": "Ціна", "kind": "user"}, "junk"])
        # exactly what Minerva reads: note consent_ok / consent_join -> protocol, auto -> marker, the rest -> items
        self.assertEqual(marks, [{"t": 5.0, "note": "", "auto": False}, {"t": 6.0, "note": "consent_ok", "auto": False},
                                 {"t": 7.0, "note": "пауза", "auto": True}, {"t": 8.0, "note": "consent_join", "auto": False},
                                 {"t": 0.0, "note": "Ціна", "auto": False}])
        self.assertEqual(minerva.levels_of({"L": {"peak_dbfs": -12.04, "rms_dbfs": -30, "sound_share": 0.4, "sound_s": 99},
                                            "R": {"peak_dbfs": None}, "X": {}}),
                         {"L": {"peak_dbfs": -12.0}, "R": {"peak_dbfs": None}})

    def test_no_audio_ever_leaves(self):
        """Every request body is JSON text built from transcript/moments/meta: no bytes, no audio keys, bounded size."""
        from bench import minerva

        segs, _ = minerva.segments_of(TRANSCRIPT_MN)
        bodies = [minerva.moments_payload({"uid": UID, "session": "s"}, MOMENTS_MN, TRANSCRIPT_MN),
                  {"uid": UID, "segments": segs, "marks": minerva.marks_of([{"t": 1, "note": "a"}])}]
        banned = {"audio", "wav", "pcm", "samples", "source", "file", "path", "sound_share", "sound_s"}

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    self.assertNotIn(k, banned)
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            else:
                self.assertIsInstance(x, (str, int, float, bool, type(None)))
        for body in bodies:
            walk(body)
            self.assertLess(minerva.encoded_size(body), minerva.PART_BYTES)


class GpuCloudTagTests(unittest.TestCase):
    def test_cloud_tags_are_not_local(self):
        from unittest.mock import patch

        from bench import gpu, jobs

        tags = {"models": [{"name": "qwen3.5:9b-q4_K_M"}, {"name": "gpt-oss:120b-cloud"}, {"name": "deepseek-v3.1:671b-cloud"},
                           {"name": "llama:latest", "remote_host": "https://ollama.com"}, {"name": "qwen2.5:7b"}]}
        with patch.object(gpu, "_ollama", return_value=tags):
            local = gpu.ollama_local("http://127.0.0.1:11434")
        self.assertEqual(local, ["qwen3.5:9b-q4_K_M", "qwen2.5:7b"])
        cfg = {**config.DEFAULTS, "llm_provider": "anthropic", "llm_model": "claude-opus-5-5"}
        self.assertEqual(jobs.local_model(cfg, local), "qwen3.5:9b-q4_K_M")     # never the cloud default
        self.assertEqual(jobs.local_model({**config.DEFAULTS, "llm_default": "x"}, ["qwen2.5:7b"]), "qwen2.5:7b")
        self.assertEqual(jobs.local_model(dict(config.DEFAULTS), []), "")
        self.assertEqual(jobs.local_model({**config.DEFAULTS, "llm_default": "big-cloud"}, ["big-cloud"]), "")


class LinkTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from bench import link as link_mod

        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)
        self.cfg.override("minerva_url", "https://minerva.test")
        self.store = store.Store(self.cfg)
        self.jobs = FakeJobs()
        self.fake = FakeMinerva()
        self.link_mod = link_mod
        self.link = self.new_link()
        self.live = None

    def new_link(self):
        lk = self.link_mod.Link(self.cfg, self.store, self.jobs, live_sid=lambda: self.live)
        lk.make_client = lambda url, key: self.fake
        lk.load_key = lambda: KEY
        return lk

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def bound(self, retention="text_only", offline=False, meeting=12):
        """A session as the WS start makes it: redeem -> reserve -> store.create -> claim; then the WAV."""
        self.fake.answers["redeem"] = [redeem_answer(meeting=meeting, retention=retention)]
        bid, _ = await self.link.redeem("t" * 43)
        b = self.link.reserve(bid)
        sid, folder, _ = self.store.create("live", b["title"], mode="both",
                                           minerva={"meeting": b["meeting"], "uid": b["uid"], "retention": b["retention"]})
        if offline:
            self.fake.answers["status"] = [remote(0, "network", coded=False)]
        _, token = await self.link.claim(bid, sid, consent=CONSENT, mode="both", processing="standard", lang_l="uk", lang_r="de")
        self.link.release(bid)
        write_wav(folder / "audio.wav")
        return sid, folder, token

    async def finish(self, sid, kind, state="done", text=""):
        self.jobs.status[sid]["state"] = state
        await self.link._job_finished(sid, kind, state, text)

    async def pipeline(self, sid, folder, transcript=TRANSCRIPT_MN, moments=MOMENTS_MN):
        await self.link.on_live_end(sid, {"duration": 1.0, "levels": {"L": {"peak_dbfs": -10.0, "sound_share": 0.5}},
                                          "silent": ["R"]})
        store.write_json(folder / "transcript.json", transcript)
        await self.finish(sid, "transcribe")
        if moments is not None and transcript["segments"]:
            store.write_json(folder / "moments.json", moments)
            await self.finish(sid, "moments")

    def sent(self, kinds=("status", "transcript", "moments")):
        return [(c[0], c[2].get("state") or c[2].get("part")) for c in self.fake.calls if c[0] in kinds]


class LinkBindingTests(LinkTestBase):
    async def test_redeem_keeps_a_binding_without_the_push_token(self):
        bid, view = await self.link.redeem("t" * 43)
        self.assertNotIn("push_token", view)
        self.assertNotIn(PUSH, json.dumps(view))
        self.assertEqual((view["title"], view["retention"], view["consent_lang"], view["max_minutes"]),
                         ("Нарада P-2026-001 · 10.11.2026", "text_only", "de", 240))
        self.assertEqual(set(view["consent_texts"]), {"de", "en", "uk"})
        saved = store.read_json(self.cfg.data_dir / "minerva-bindings.json")
        self.assertEqual(saved[bid]["push_token"], PUSH)                  # kept on the RAG PC (user + SYSTEM only)
        self.assertGreater(saved[bid]["expires"] - time.time(), 239 * 60)
        restarted = self.new_link()                                     # F5 after a bench restart: still there
        restarted.load()
        self.assertEqual(restarted.binding_view(bid)["title"], view["title"])
        bid2, _ = await self.link.redeem("u" * 43)                      # a second window for the same meeting
        self.assertIsNone(self.link.binding_view(bid))
        self.assertIsNotNone(self.link.binding_view(bid2))
        self.link.bindings[bid2]["expires"] = time.time() - 1
        self.assertIsNone(self.link.binding_view(bid2))
        with self.assertRaises(self.link_mod.LinkError):
            self.link.reserve(bid2)

    async def test_redeem_errors_in_ukrainian(self):
        LinkError = self.link_mod.LinkError
        cases = [(remote(410, "ticket_expired"), 410, "Посилання застаріло"), (remote(404, "ticket_invalid"), 410, "Посилання застаріло"),
                 (remote(0, "timeout", coded=False), 504, "не відповіла вчасно"), (remote(0, "network", coded=False), 502, "Minerva не відповідає"),
                 (remote(401, "unauthorized"), 502, "ключ"), ({"meeting": {}}, 502, "незрозуміло")]
        for answer, status, text in cases:
            self.fake.answers["redeem"] = [answer]
            with self.subTest(answer=answer), self.assertRaises(LinkError) as ctx:
                await self.link.redeem("t" * 43)
            self.assertEqual(ctx.exception.status, status)
            self.assertIn(text, ctx.exception.text)
            self.assertNotIn(KEY, ctx.exception.text)
        with self.assertRaises(LinkError):
            await self.link.redeem("bad ticket")
        self.cfg.override("minerva_url", "")
        with self.assertRaises(LinkError) as ctx:
            await self.link.redeem("t" * 43)
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("ще не підключено", ctx.exception.text)
        self.assertEqual(sum(1 for c in self.fake.calls if c[0] == "redeem"), len(cases))   # never retried by itself

    async def test_reserve_twice_and_used(self):
        bid, _ = await self.link.redeem("t" * 43)
        self.link.reserve(bid)
        with self.assertRaises(self.link_mod.LinkError):
            self.link.reserve(bid)                                       # a duplicated tab with the same binding
        self.link.release(bid)
        self.link._consume(bid, "20261110-100000-abcd")
        with self.assertRaises(self.link_mod.LinkError) as ctx:
            self.link.reserve(bid)
        self.assertIn("Записати продовження", ctx.exception.text)
        self.assertNotIn("push_token", self.link.bindings[bid])
        self.assertTrue(self.link.binding_view(bid)["used"])


class LinkClaimTests(LinkTestBase):
    async def test_claim_pushes_recording_first(self):
        sid, folder, token = await self.bound()
        kind, meeting, body, push = self.fake.calls[-1]
        self.assertEqual((kind, meeting, push, body["state"], body["seq"], body["session"], body["uid"]),
                         ("status", 12, PUSH, "recording", 1, sid, UID))
        self.assertEqual(body["consent"], CONSENT)
        self.assertEqual(body["capture"], {"mode": "both", "processing": "standard"})
        self.assertEqual((body["lang_l"], body["lang_r"]), ("uk", "de"))
        self.assertRegex(body["at"], r"[+-]\d\d:\d\d$")                # ISO-8601 with the offset
        self.assertEqual(body["started_at"], body["at"])
        st = store.read_json(folder / "minerva.json")
        self.assertEqual((st["outbox"], st["seq"], st["steps"]["claimed"]), ([], 1, True))
        self.assertEqual(st["hotwords"], "Sevskiy, P-2026-001, AN220213-01C")
        self.assertEqual(self.link.progress(token)["state"], "processing")
        self.assertIsNone(self.link.progress("x" * 24))

    async def test_stale_window_is_refused(self):
        for code in ("push_forbidden", "session_mismatch", "meeting_gone", "meeting_unknown"):
            self.fake.answers["redeem"] = [redeem_answer()]
            bid, _ = await self.link.redeem("t" * 43)
            self.link.reserve(bid)
            sid, _, _ = self.store.create("live", "x", minerva={"meeting": 12})
            self.fake.answers["status"] = [remote(409, code)]
            with self.subTest(code=code), self.assertRaises(self.link_mod.LinkError) as ctx:
                await self.link.claim(bid, sid, consent=CONSENT, mode="both", processing=None, lang_l="", lang_r="")
            self.assertIn("застаріло", ctx.exception.text)
            self.assertNotIn(sid, self.link.states)
            self.assertNotIn(bid, self.link.bindings)                   # the window is spent
        # consent_missing is a refusal of the data (like invalid_request), not a stale window
        self.fake.answers["redeem"] = [redeem_answer()]
        bid, _ = await self.link.redeem("t" * 43)
        self.link.reserve(bid)
        sid, _, _ = self.store.create("live", "x", minerva={"meeting": 12})
        self.fake.answers["status"] = [remote(409, "consent_missing")]
        with self.assertRaises(self.link_mod.LinkError) as ctx:
            await self.link.claim(bid, sid, consent=CONSENT, mode="both", processing="system", lang_l="", lang_r="")
        self.assertIn("не прийняла", ctx.exception.text)
        self.assertIn(bid, self.link.bindings)
        self.assertEqual(self.fake.calls[-1][2]["capture"], {"mode": "both", "processing": "system", "headset": False})

    async def test_minerva_down_at_start_records_anyway(self):
        sid, folder, _ = await self.bound(offline=True)
        st = self.link.states[sid]
        self.assertEqual([(i["kind"], i["state"]) for i in st["outbox"]], [("status", "recording")])
        self.assertGreater(st["outbox"][0]["next_at"], time.time())
        self.assertTrue(self.link.bindings and all(b["used"] for b in self.link.bindings.values()))


class LinkPipelineTests(LinkTestBase):
    async def test_text_only_full_run_in_order(self):
        sid, folder, token = await self.bound()
        await self.pipeline(sid, folder)
        self.assertEqual([k for _, k, _ in self.jobs.submitted], ["transcribe", "moments"])
        tparams = self.jobs.submitted[0][2]
        self.assertNotIn("hotwords", tparams)                           # hotwords reach the worker via job.json only
        self.assertEqual((tparams["lang_l"], tparams["lang_r"]), ("uk", "de"))
        mparams = self.jobs.submitted[1][2]
        self.assertEqual((mparams["provider"], mparams["minerva"]), ("ollama", True))
        self.assertIsNone(self.store.audio_file(sid))                   # «лише текст»: gone right after transcript.json
        await self.link.tick()
        self.assertEqual(self.sent(), [("status", "recording"), ("status", "recorded"), ("status", "audio_deleted"), ("transcript", 1),
                                       ("status", "transcribed"), ("status", "finding_moments"), ("moments", None),
                                       ("status", "done")])
        statuses = [c[2] for c in self.fake.calls if c[0] == "status"]
        self.assertEqual([b["seq"] for b in statuses], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(b["started_at"] == statuses[0]["started_at"] and b["at"] for b in statuses))
        recorded = statuses[1]
        self.assertEqual((recorded["duration"], recorded["silent"], recorded["levels"]),
                         (1.0, ["R"], {"L": {"peak_dbfs": -10.0}}))
        part = next(c[2] for c in self.fake.calls if c[0] == "transcript")
        self.assertEqual((part["part"], part["parts"], part["total_segments"], [s["n"] for s in part["segments"]]),
                         (1, 1, 3, [1, 2, 3]))
        self.assertEqual(part["languages"], {"L": "uk", "R": "de"})        # the probability stays on the RAG PC
        self.assertNotIn("marks", part)                                    # no marks.json: Minerva keeps `recorded`'s
        self.assertEqual(statuses[-1]["segments"], 3)
        self.assertEqual(statuses[-1]["items"], 1)                      # the item with no such line is not sent
        # everything delivered: the texts leave the RAG PC, meta.json + minerva.json stay for sync
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["meta.json", "minerva.json"])
        self.assertEqual(self.link.progress(token)["state"], "done")
        sync = next(c[2] for c in self.fake.calls if c[0] == "sync")
        self.assertEqual(sync["sessions"][0], {"meeting": 12, "uid": UID, "session": sid,
                                               "token_sha256": __import__("hashlib").sha256(PUSH.encode()).hexdigest(),
                                               "stage": "done", "progress": None, "queue_pos": 0, "audio_on_bench": False})
        self.assertEqual(sync["bench"], {"version": "0.3.0", "live_busy": False, "queue": 0, "outbox": 0, "failed": 0,
                                         "last_error": "", "retention_days": 30, "standalone": False})

    async def test_empty_transcript_and_audio_text(self):
        sid, folder, _ = await self.bound()
        await self.pipeline(sid, folder, transcript={"model": "large-v3", "languages": {}, "segments": []})
        await self.link.tick()
        self.assertEqual(self.sent(), [("status", "recording"), ("status", "recorded"), ("status", "audio_deleted"),
                                       ("status", "transcribed"), ("status", "done")])
        done = [c[2] for c in self.fake.calls if c[0] == "status"][-1]
        self.assertEqual((done["segments"], done["items"]), (0, 0))
        self.assertEqual([k for _, k, _ in self.jobs.submitted], ["transcribe"])
        self.fake.calls.clear()
        sid2, folder2, _ = await self.bound(retention="audio_text", meeting=13)
        await self.pipeline(sid2, folder2)
        self.assertIsNotNone(self.store.audio_file(sid2))              # «аудіо + текст»: kept for now
        await self.link.tick()
        self.assertNotIn(("status", "audio_deleted"), self.sent())

    async def test_failed_step_is_retried_then_final(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        for attempt in (1, 2):
            await self.finish(sid, "transcribe", "error", "Не вистачило пам'яті відеокарти.")
            st = self.link.states[sid]
            self.assertGreater(st["retry_at"]["transcribe"], time.time())
            self.assertIn(f"Спроба {attempt} з 3", st["outbox"][-1]["data"]["detail"])
            st["retry_at"]["transcribe"] = time.time() - 1
            await self.link.advance(sid)
        self.assertEqual(len(self.jobs.submitted), 3)
        await self.finish(sid, "transcribe", "error", "Не вистачило пам'яті відеокарти.")
        st = self.link.states[sid]
        self.assertTrue(st["failed"]["transcribe"])
        self.assertIsNone(self.store.audio_file(sid))                  # «лише текст»: also after the last failure
        await self.link.tick()
        self.assertEqual(self.sent()[-2:], [("status", "error"), ("status", "audio_deleted")])
        err = [c[2] for c in self.fake.calls if c[0] == "status" and c[2]["state"] == "error"][0]
        self.assertEqual(err["stage"], "transcribe")

    async def test_resume_after_a_restart(self):
        sid, folder, _ = await self.bound()                            # recording cut: no `recorded` was queued
        write_wav(folder / "audio.wav", seconds=2.5)
        again = self.new_link()
        await again.resume()
        st = again.states[sid]
        rec = [i for i in st["outbox"] if i.get("state") == "recorded"][0]
        self.assertEqual((rec["data"]["duration"], rec["data"]["interrupted"]), (2.5, True))
        self.assertIn("перезапустився", rec["data"]["detail"])
        self.assertEqual(self.jobs.submitted[-1][1], "transcribe")
        # the worker wrote transcript.json, then the bench stopped before the parts were queued
        store.write_json(folder / "transcript.json", TRANSCRIPT_MN)
        self.jobs.status[sid]["state"] = "done"
        third = self.new_link()
        await third.resume()
        kinds = [i["kind"] for i in third.states[sid]["outbox"]]
        self.assertIn("transcript", kinds)
        self.assertEqual(self.jobs.submitted[-1][1], "moments")
        self.assertTrue(third.states[sid]["steps"]["transcript"])

    async def test_progress_statuses_are_merged(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        self.jobs.status[sid].update(state="running", started=1.0, progress=0.1)
        self.link._progress()
        self.link.states[sid]["last_progress"]["at"] = 0
        self.jobs.status[sid]["progress"] = 0.4
        self.link._progress()
        items = [(i["state"], i["data"].get("progress")) for i in self.link.states[sid]["outbox"]]
        self.assertEqual(items, [("recorded", None), ("transcribing", 40)])


class LinkOutboxTests(LinkTestBase):
    async def test_transient_backoff_and_reset_on_success(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        self.fake.answers["status"] = [remote(502, "http_502", coded=False)]
        now = time.time()
        await self.link.tick(now)
        item = self.link.states[sid]["outbox"][0]
        self.assertEqual((item["tries"], item["state"]), (1, "recorded"))
        self.assertGreaterEqual(item["next_at"], now + 30)
        self.assertFalse(any(c[0] == "sync" for c in self.fake.calls))   # the round stopped: Minerva is not reachable
        await self.link.tick(now + 1)                                   # not due yet: nothing sent
        self.assertEqual(sum(1 for c in self.fake.calls if c[0] == "status"), 2)
        self.fake.answers["status"] = [remote(429, "rate_limited", retry_after=500)]
        await self.link.tick(now + 31)
        self.assertGreaterEqual(self.link.states[sid]["outbox"][0]["next_at"], now + 31 + 500)
        for n in range(3, 12):
            self.link._backoff(item, remote(503, "http_503", coded=False), now)
        self.assertLessEqual(item["next_at"], now + 3600)               # minerva_retry_max_seconds
        await self.link.tick(now + 4000)
        self.assertEqual(self.link.states[sid]["outbox"], [])

    async def test_one_session_waits_others_go(self):
        sid1, _, _ = await self.bound()
        sid2, _, _ = await self.bound(meeting=13)
        await self.link.on_live_end(sid1, {"duration": 1.0})
        await self.link.on_live_end(sid2, {"duration": 1.0})
        self.link.states[sid1]["outbox"][0]["next_at"] = time.time() + 999   # backed-off head of session 1
        await self.link.tick()
        self.assertEqual([c[2]["session"] for c in self.fake.calls if c[0] == "status" and c[2]["state"] == "recorded"], [sid2])
        self.assertEqual(len(self.link.states[sid1]["outbox"]), 1)

    async def test_pause_on_a_refused_key_and_probe(self):
        sid, _, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        self.fake.answers["status"] = [remote(401, "unauthorized")]
        now = time.time()
        await self.link.tick(now)
        self.assertEqual(self.link.paused["code"], "unauthorized")
        self.assertEqual(self.link.status()["paused"]["code"], "unauthorized")
        await self.link.tick(now + 5)
        self.assertFalse(any(c[0] == "ping" for c in self.fake.calls))   # probes once per poll interval
        self.fake.answers["ping"] = [remote(401, "unauthorized")]
        await self.link.tick(now + 200)
        self.assertTrue(self.link.paused)
        await self.link.tick(now + 400)                                 # ping answers 2xx: everything goes again
        self.assertIsNone(self.link.paused)
        self.assertEqual(self.link.states[sid]["outbox"], [])

    async def test_new_key_or_address_resets_the_pause(self):
        self.link.paused = {"code": "unauthorized", "text": "x", "at": "t"}
        await self.link.client()
        self.link.load_key = lambda: "cd" * 32
        await self.link.client()
        self.assertIsNone(self.link.paused)

    async def test_gone_unknown_blocked(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        self.jobs.status[sid]["state"] = "done"
        self.fake.answers["status"] = [remote(410, "meeting_gone")]
        await self.link.tick()
        self.assertNotIn(sid, self.link.states)
        self.assertFalse(folder.exists())                               # Minerva deleted the meeting: so does the bench
        sid2, folder2, _ = await self.bound(meeting=13)
        await self.link.on_live_end(sid2, {"duration": 1.0})
        self.fake.answers["status"] = [remote(404, "meeting_unknown")]
        await self.link.tick()
        self.assertEqual(self.link.states[sid2]["blocked"]["code"], "meeting_unknown")
        self.assertTrue(folder2.exists())                               # unknown is no reason to delete
        self.assertIn("не знає", self.link._view(sid2, self.link.states[sid2])["text"])
        self.fake.answers["sync"] = [{"sessions": [{"meeting": 13, "session": sid2, "action": "keep"}]}]
        await self.link.sync()
        self.assertIsNone(self.link.states[sid2]["blocked"])
        self.fake.answers["status"] = [remote(403, "push_forbidden")]
        await self.link.tick(time.time() + 999)
        self.assertEqual(self.link.states[sid2]["blocked"]["code"], "push_forbidden")

    async def test_bad_part_is_dropped_and_413_resplits(self):
        sid, folder, _ = await self.bound()
        long = {**TRANSCRIPT_MN, "segments": [{"start": i, "end": i + 1, "ch": "L", "text": "Слово " * 300} for i in range(200)]}
        await self.pipeline(sid, folder, transcript=long, moments=None)
        st = self.link.states[sid]
        first_upload = st["steps"]["upload_id"]
        parts_before = sum(1 for i in st["outbox"] if i["kind"] == "transcript")
        self.fake.answers["transcript"] = [remote(413, "payload_too_large")]
        await self.link.tick()
        self.assertEqual(st["part_bytes"], 256 * 1024)
        self.assertNotEqual(st["steps"]["upload_id"], first_upload)
        uploads = {c[2]["upload_id"] for c in self.fake.calls if c[0] == "transcript"}
        self.assertEqual(len(uploads), 2)
        sent_parts = [c[2]["part"] for c in self.fake.calls if c[0] == "transcript" and c[2]["upload_id"] == st["steps"]["upload_id"]]
        self.assertEqual(sent_parts, list(range(1, len(sent_parts) + 1)))
        self.assertGreater(len(sent_parts), parts_before)
        for c in self.fake.calls:
            if c[0] == "transcript":
                self.assertLessEqual(len(json.dumps(c[2], ensure_ascii=False).encode()), 512 * 1024)

        sid2, folder2, _ = await self.bound(meeting=13)
        await self.pipeline(sid2, folder2)
        self.fake.answers["transcript"] = [remote(400, "invalid_request")]
        await self.link.tick()
        st2 = self.link.states.get(sid2)
        kinds = [c[2].get("state") or c[0] for c in self.fake.calls if c[2] and c[2].get("session") == sid2]
        self.assertIn("error", kinds)
        self.assertNotIn("moments", kinds)                              # no transcript in Minerva -> no moments either
        err = [c[2] for c in self.fake.calls if c[0] == "status" and c[2].get("state") == "error"][-1]
        self.assertEqual(err["stage"], "push")
        self.assertFalse(st2 and st2["outbox"])

    async def test_transcript_exists_counts_as_delivered(self):
        sid, folder, _ = await self.bound()
        await self.pipeline(sid, folder)
        self.fake.answers["transcript"] = [remote(409, "transcript_exists")]
        await self.link.tick()
        self.assertTrue(self.link.states[sid]["delivered"]["transcript"])
        self.assertEqual(self.link.states[sid]["outbox"], [])

    async def test_sync_verdicts(self):
        sid, folder, _ = await self.bound(retention="audio_text")
        await self.pipeline(sid, folder)
        await self.link.tick()
        self.assertIsNotNone(self.store.audio_file(sid))
        self.assertEqual(self.link_mod.Link.verdicts([{"meeting": 12, "session": sid, "action": "retry"}]), {sid: "retry"})
        self.fake.answers["sync"] = [{"drop_audio": [{"meeting": 12, "session": sid}]}]   # the grouped form works too
        await self.link.sync()
        self.assertIsNone(self.store.audio_file(sid))
        self.assertEqual(self.link.states[sid]["outbox"][-1]["state"], "audio_deleted")
        # a delete verdict for a session with a running job is only a flag; a later keep clears it
        sid2, folder2, _ = await self.bound(meeting=13)
        await self.link.on_live_end(sid2, {"duration": 1.0})
        self.fake.answers["sync"] = [{"sessions": [{"session": sid2, "action": "delete"}]}]
        await self.link.sync()
        self.assertEqual(self.jobs.cancelled, [sid2])
        self.jobs.status[sid2]["state"] = "queued"                      # (as if the cancel had not landed yet)
        self.fake.answers["sync"] = [{"sessions": [{"session": sid2, "action": "keep"}]}]
        await self.link.sync()
        self.assertIsNone(self.link.states[sid2]["delete"])
        self.assertTrue(folder2.exists())
        self.jobs.status[sid2]["state"] = "done"
        self.fake.answers["sync"] = [{"sessions": [{"session": sid2, "action": "delete"}]}]
        await self.link.sync()
        self.assertFalse(folder2.exists())

    async def test_sync_retry(self):
        sid, folder, _ = await self.bound(retention="audio_text")
        await self.link.on_live_end(sid, {"duration": 1.0})
        for _ in range(3):
            self.link.states[sid]["retry_at"]["transcribe"] = 0
            await self.finish(sid, "transcribe", "error", "Помилка")
            self.link.states[sid]["retry_at"]["transcribe"] = 0
            await self.link.advance(sid)
        self.assertTrue(self.link.states[sid]["failed"]["transcribe"])
        n = len(self.jobs.submitted)
        self.assertIsNotNone(self.store.audio_file(sid))               # «аудіо + текст» keeps it for a retry
        self.fake.answers["sync"] = [{"sessions": [{"session": sid, "action": "retry"}]}]
        await self.link.sync()
        self.assertEqual(len(self.jobs.submitted), n + 1)
        self.assertFalse(self.link.states[sid]["failed"]["transcribe"])
        self.assertIn("на прохання з Minerva", self.link.states[sid]["outbox"][-1]["data"]["detail"])


class LinkDeletionTests(LinkTestBase):
    async def test_discard_and_notice_after_the_folder_is_gone(self):
        sid, folder, token = await self.bound()
        await self.link.discard(sid)
        self.assertFalse(folder.exists())
        self.assertEqual(self.link.notices[0]["state"], "discarded")
        self.assertEqual(self.link.progress(token)["state"], "discarded")
        await self.link.tick()
        body = [c[2] for c in self.fake.calls if c[0] == "status"][-1]
        self.assertEqual((body["state"], body["seq"], body["session"]), ("discarded", 2, sid))
        self.assertEqual(self.link.notices, [])
        self.assertEqual(store.read_json(self.cfg.data_dir / "minerva-notices.json"), [])

    async def test_locked_file_is_retried_then_hidden(self):
        from pathlib import Path
        from unittest.mock import patch

        sid, folder, _ = await self.bound()
        real, calls = Path.unlink, []

        def flaky(p, missing_ok=False):
            calls.append(p.name)
            if p.name == "audio.wav" and calls.count("audio.wav") == 1:
                raise PermissionError(13, "in use")
            return real(p, missing_ok=missing_ok)

        with patch.object(store.time, "sleep"), patch.object(Path, "unlink", flaky):
            self.assertEqual(self.store.remove(sid), "gone")             # one locked moment: retried
        self.assertGreater(calls.count("audio.wav"), 1)
        sid2, folder2, _ = await self.bound(meeting=13)

        def locked(p, missing_ok=False):
            if p.name == "audio.wav":
                raise PermissionError(13, "in use")
            return real(p, missing_ok=missing_ok)

        with patch.object(store.time, "sleep"), patch.object(Path, "unlink", locked):
            self.assertEqual(self.store.remove(sid2), "hidden")
        self.assertFalse(folder2.exists())
        hidden = self.cfg.sessions_dir / f".deleting-{sid2}"
        self.assertTrue((hidden / "minerva.json").exists())             # removed last: still known as private
        self.assertEqual([s["id"] for s in self.store.list()], [])
        self.assertEqual(self.store.retry_deleting(), [sid2])
        self.assertTrue(self.store.gone(sid2))

    async def test_live_session_deleted_in_minerva_stops(self):
        sid, folder, _ = await self.bound()
        self.live = sid
        stopped = []
        self.link.stop_live = lambda s, text: stopped.append((s, text))
        self.link.states[sid]["delete"] = {"reason": "gone"}
        await self.link._finish_deletions()
        self.assertEqual(stopped[0][0], sid)
        self.assertIn("видалено", stopped[0][1])
        self.assertTrue(folder.exists())                               # the WS handler stops it first
        self.live = None
        await self.link.on_live_end(sid, None)
        self.assertFalse(folder.exists())
        self.assertEqual(self.jobs.submitted, [])                       # nothing transcribed

    async def test_hard_cap_and_audio_days(self):
        sid, folder, _ = await self.bound(retention="audio_text")
        await self.pipeline(sid, folder)
        await self.link.tick()
        now = time.time()
        self.link.states[sid]["audio_days"] = 7
        await self.link.housekeeping(now + 8 * 86400)
        self.assertIsNone(self.store.audio_file(sid))
        self.assertIn("7 днів", self.link.states[sid]["outbox"][-1]["data"]["detail"])
        await self.link.housekeeping(now + 31 * 86400)
        self.assertFalse(folder.exists())
        self.assertEqual(self.link.notices[-1]["state"], "purged")

    async def test_purge_leaves_minerva_sessions_and_busy_ones(self):
        sid, folder, _ = await self.bound()
        old = self.cfg.sessions_dir / time.strftime("%Y%m%d-%H%M%S-0001", time.localtime(time.time() - 40 * 86400))
        old.mkdir()
        store.write_json(old / "meta.json", {"id": old.name, "kind": "live"})
        busy = self.cfg.sessions_dir / time.strftime("%Y%m%d-%H%M%S-0002", time.localtime(time.time() - 40 * 86400))
        busy.mkdir()
        store.write_json(busy / "meta.json", {"id": busy.name})
        self.assertEqual(self.store.purge(1, keep=lambda s: s == busy.name), 1)
        self.assertFalse(old.exists())
        self.assertTrue(busy.exists())
        self.store.purge(0.0000001)
        self.assertTrue(folder.exists())                               # a Minerva session follows its own rules


class LinkStatusTests(LinkTestBase):
    async def test_status_block_has_no_secrets(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        s = self.link.status()
        self.assertEqual((s["configured"], s["key_saved"], s["standalone"], s["outbox"], s["url"]),
                         (True, True, False, 1, "https://minerva.test"))
        text = json.dumps(s, ensure_ascii=False)
        for secret in (KEY, PUSH, "Нарада P-2026", "Sevskiy"):
            self.assertNotIn(secret, text)
        self.cfg.override("minerva_url", "http://minerva.test")
        self.assertTrue(self.link.status()["url_error"])


class JobsMinervaTests(unittest.IsolatedAsyncioTestCase):
    """on_finish, explicit dispatch, cancel, never-cloud and hotwords only through job.json."""

    async def asyncSetUp(self):
        from bench.jobs import Jobs

        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)
        self.store = store.Store(self.cfg)
        self.finished = []
        self.jobs = Jobs(self.cfg, self.store, live_busy=lambda: False, on_finish=lambda *a: self.finished.append(a))
        self.sid, self.folder, _ = self.store.create("live", "Нарада", mode="both", minerva={"meeting": 12})
        store.write_json(self.folder / "minerva.json", {"meeting": 12, "hotwords": "Secret Customer GmbH"})
        store.write_json(self.folder / "transcript.json", TRANSCRIPT_MN)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def run_one(self):
        import asyncio

        task = asyncio.create_task(self.jobs.run_forever())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if self.finished:
                break
        task.cancel()

    async def test_minerva_moments_never_use_a_cloud_model(self):
        from unittest.mock import patch

        from bench import cloud, gpu, moments as moments_mod
        from bench.jobs import JobError

        with patch.object(cloud, "extract") as ext, self.assertRaises(JobError):
            await self.jobs._moments(self.sid, {"provider": "anthropic", "model": "claude-opus-5-5", "minerva": True}, {})
        ext.assert_not_called()
        with patch.object(gpu, "ollama_local", return_value=[]), patch.object(moments_mod, "extract") as ext2, \
             self.assertRaises(JobError):
            await self.jobs._moments(self.sid, {"provider": "ollama", "model": "gpt-oss:120b-cloud", "minerva": True}, {})
        ext2.assert_not_called()
        ok = {"model": "qwen3.5:9b-q4_K_M", "summary": "", "items": [], "rejected": [], "metrics": {}}
        with patch.object(gpu, "ollama_local", return_value=["qwen3.5:9b-q4_K_M"]), \
             patch.object(gpu, "ollama_installed", return_value=["qwen3.5:9b-q4_K_M"]), \
             patch.object(moments_mod, "extract", return_value=ok) as ext3:
            await self.jobs._moments(self.sid, {"provider": "ollama", "model": "", "minerva": True}, {})
        self.assertEqual(ext3.call_args.args[2], "qwen3.5:9b-q4_K_M")

    async def test_hotwords_only_in_job_json_and_on_finish(self):
        import asyncio
        from unittest.mock import patch

        from bench import gpu, jobs as jobs_mod

        write_wav(self.folder / "audio.wav")

        class Proc:
            returncode = 0

            async def wait(self):
                return 0

        async def fake_exec(*a, **kw):
            return Proc()

        with patch.object(gpu, "memory", return_value=None), patch.object(jobs_mod.asyncio, "create_subprocess_exec", fake_exec):
            st = self.jobs.submit(self.sid, "transcribe", {"model": "accurate", "device": "auto", "lang_l": "uk"})
            self.assertNotIn("hotwords", json.dumps(st))
            await self.run_one()
        self.assertEqual(store.read_json(self.folder / "job.json")["hotwords"], "Secret Customer GmbH")
        self.assertNotIn("Secret", json.dumps(self.jobs.status[self.sid]))
        self.assertEqual(self.finished, [(self.sid, "transcribe", "done", "Готово")])
        with self.assertRaises(jobs_mod.JobError):
            self.jobs.submit(self.sid, "cleanup", {})
        self.jobs.submit(self.sid, "moments", {})
        self.jobs.cancel(self.sid)
        self.finished.clear()
        task = asyncio.create_task(self.jobs.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        self.assertEqual(self.finished, [])                             # a cancelled job never runs
        self.assertEqual(self.jobs.status[self.sid]["state"], "cancelled")


class ServerMinervaTests(unittest.IsolatedAsyncioTestCase):
    """The page side of stage 1: privacy of Minerva sessions, the switch for test recordings, redeem and the window."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        from bench import server

        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load(self.tmp.name)
        self.cfg.override("minerva_url", "https://minerva.test")
        self.server = server
        self.app = server.create_app(self.cfg, background=False)
        self.client = TestClient(TestServer(self.app, host="127.0.0.1"))
        await self.client.start_server()
        self.ctx = self.app[server.APP_KEY]
        self.fake = FakeMinerva()
        self.ctx["link"].make_client = lambda url, key: self.fake
        self.ctx["link"].load_key = lambda: KEY
        self.headers = {"X-Bench": "1", "Origin": str(self.client.make_url("/"))[:-1]}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def minerva_session(self):
        sid, folder, _ = self.ctx["store"].create("live", "Таємна нарада", minerva={"meeting": 12, "uid": UID})
        store.write_json(folder / "minerva.json", {"meeting": 12, "uid": UID})
        write_wav(folder / "audio.wav")
        store.write_json(folder / "transcript.json", TRANSCRIPT_MN)
        return sid

    async def test_minerva_sessions_are_never_served(self):
        self.cfg.override("standalone_enabled", True)
        sid = self.minerva_session()
        bare = self.cfg.sessions_dir / "20261110-101010-0abc"            # a folder without meta.json
        bare.mkdir()
        (bare / "audio.wav").write_bytes(b"RIFF")
        test_sid, _, _ = self.ctx["store"].create("live", "Тест")
        for s in (sid, bare.name):
            for path in ("", "/audio", "/export.txt"):
                r = await self.client.get(f"/api/sessions/{s}{path}")
                self.assertEqual(r.status, 404, (s, path))
            for path in ("/transcribe", "/moments", "/rename", "/delete"):
                r = await self.client.post(f"/api/sessions/{s}{path}", json={"choice": "anthropic:claude-opus-5-5",
                                                                            "cloud_consent": s}, headers=self.headers)
                self.assertEqual(r.status, 404, (s, path))
        r = await self.client.get("/api/sessions")
        self.assertEqual([x["id"] for x in (await r.json())["sessions"]], [test_sid])
        r = await self.client.get("/api/metrics")
        self.assertEqual([x["id"] for x in (await r.json())["rows"]], [test_sid])
        self.assertTrue((self.cfg.sessions_dir / sid / "audio.wav").exists())

    async def test_status_shows_no_job_params(self):
        from unittest.mock import patch

        from bench import cloud, gpu

        sid = self.minerva_session()
        jobs = self.ctx["jobs"]
        jobs.status[sid] = {"kind": "transcribe", "state": "running", "text": "Розшифровую…", "progress": 0.4,
                            "params": {"hotwords": "Secret Customer GmbH"}}
        jobs.current = sid
        with patch.object(gpu, "memory", return_value=None), patch.object(gpu, "ollama_loaded", return_value=[]), \
             patch.object(gpu, "ollama_installed", return_value=[]), patch.object(cloud, "choices", return_value=[]):
            r = await self.client.get("/api/status")
            text = await r.text()
            data = json.loads(text)
            self.assertEqual(data["job"], {"kind": "transcribe", "state": "running", "progress": 0.4, "minerva": True})
            self.assertNotIn("Secret", text)
            self.assertEqual((data["standalone"], data["minerva"]["configured"]), (False, True))
            other, _, _ = self.ctx["store"].create("upload", "Тест")
            jobs.status[other] = {"kind": "transcribe", "state": "running", "text": "x", "params": {"hotwords": "Secret"}}
            jobs.current = other
            r = await self.client.get("/api/status")
            self.assertNotIn("Secret", await r.text())
            self.assertNotIn("params", (await r.json())["job"])

    async def test_test_bench_is_off_by_default(self):
        for method, path in (("get", "/api/sessions"), ("get", "/api/metrics"), ("get", "/api/metrics.csv"),
                             ("post", "/api/upload")):
            r = await getattr(self.client, method)(path, headers=self.headers)
            self.assertEqual(r.status, 403, path)
            self.assertIn("Тестові записи без Minerva вимкнено", (await r.json())["error"])
        msgs = await self.ws_run({"type": "start", "consent": True, "engine": "none", "mode": "both", "title": "x"})
        self.assertEqual(msgs[-1]["type"], "error")
        self.assertTrue(msgs[-1]["stale"])
        self.assertEqual(list(self.cfg.sessions_dir.iterdir()), [])

    async def test_refused_pc_gets_a_page_with_steps(self):
        self.cfg.override("trusted_proxies", ["127.0.0.1"])
        r = await self.client.get("/", headers={"X-Real-IP": "192.168.2.77<script>"})
        self.assertEqual(r.status, 403)
        page = await r.text()
        self.assertIn("Дозволені адреси", page)
        self.assertIn("192.168.2.77&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("Content-Security-Policy", r.headers)
        r = await self.client.get("/api/status", headers={"X-Real-IP": "192.168.2.77"})
        self.assertEqual(r.status, 403)

    async def test_redeem_binding_progress(self):
        r = await self.client.post("/api/minerva/redeem", json={"ticket": "t" * 43}, headers=self.headers)
        self.assertEqual(r.status, 200, await r.text())
        text = await r.text()
        view = json.loads(text)
        self.assertNotIn(PUSH, text)
        self.assertEqual((view["live_busy"], view["return_url"]), (False, redeem_answer()["meeting"]["return_url"]))
        r = await self.client.get(f"/api/minerva/binding/{view['binding']}")
        self.assertEqual((await r.json())["title"], view["title"])
        r = await self.client.get("/api/minerva/binding/nope")
        self.assertEqual(r.status, 404)
        self.assertIn("надто давно", (await r.json())["error"])
        r = await self.client.get("/api/minerva/progress/" + "x" * 24)
        self.assertEqual(r.status, 404)
        self.fake.answers["redeem"] = [remote(410, "ticket_expired")]
        r = await self.client.post("/api/minerva/redeem", json={"ticket": "t" * 43}, headers=self.headers)
        self.assertEqual(r.status, 410)
        self.assertIn("Посилання застаріло", (await r.json())["error"])
        r = await self.client.post("/api/minerva/redeem", json={"ticket": "t" * 43})     # not from this page
        self.assertEqual(r.status, 403)

    async def ws_run(self, start, after_ready=None):
        import asyncio

        from aiohttp import WSMsgType

        ws = await self.client.ws_connect("/ws", headers={"Origin": self.headers["Origin"]})
        await ws.send_str(json.dumps(start))
        got = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 10)
            if msg.type != WSMsgType.TEXT:
                break
            m = json.loads(msg.data)
            got.append(m)
            if m["type"] == "ready" and after_ready:
                await after_ready(ws)
        await ws.close()
        return got

    async def binding(self, **extra):
        self.fake.answers["redeem"] = [redeem_answer(**extra)]
        r = await self.client.post("/api/minerva/redeem", json={"ticket": "t" * 43}, headers=self.headers)
        return (await r.json())["binding"]

    async def test_minerva_recording(self):
        engines = []

        async def fake_engine(ctx, name):
            engines.append(name)
            return None

        bid = await self.binding()

        async def record(ws):
            await ws.send_bytes(b"\x10\x00" * 3200)
            await ws.send_str(json.dumps({"type": "mark", "note": "consent_ok"}))
            await ws.send_str(json.dumps({"type": "mark", "note": "мікрофон від’єднано", "auto": True}))
            await ws.send_str(json.dumps({"type": "mark", "note": ""}))
            await ws.send_str(json.dumps({"type": "stop"}))

        from unittest.mock import patch

        with patch.object(self.server, "_engine", fake_engine):
            msgs = await self.ws_run({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "whisper",
                                      "mode": "both", "lang_l": "uk", "lang_r": "auto", "title": "чужа назва"}, record)
        types = [m["type"] for m in msgs]
        self.assertIn("ready", types)
        ready = next(m for m in msgs if m["type"] == "ready")
        self.assertIsNone(ready["id"])
        self.assertTrue(ready["minerva"] and ready["progress"])
        self.assertEqual(engines, ["parakeet"])                         # never live Whisper for a Minerva meeting
        self.assertEqual([m["kind"] for m in msgs if m["type"] == "marked"], ["consent_ok", "auto", "user"])
        saved = next(m for m in msgs if m["type"] == "saved")
        self.assertTrue(saved["minerva"])
        link = self.ctx["link"]
        (sid, st), = link.states.items()
        self.assertEqual(self.ctx["store"].meta(sid)["title"], "Нарада P-2026-001 · 10.11.2026")
        claim = self.fake.calls[1][2]
        self.assertEqual((claim["state"], claim["consent"]["all_agreed"], claim["lang_l"], claim["lang_r"]),
                         ("recording", True, "uk", ""))
        recorded = [i for i in st["outbox"] if i.get("state") == "recorded"][0]["data"]
        self.assertEqual([(m["note"], m["auto"]) for m in recorded["marks"]],
                         [("consent_ok", False), ("мікрофон від’єднано", True), ("", False)])
        self.assertEqual(self.ctx["jobs"].status[sid]["kind"], "transcribe")
        self.assertNotIn("hotwords", self.ctx["jobs"].status[sid]["params"])
        self.assertIsNone(self.ctx["live_sid"])
        r = await self.client.get(f"/api/minerva/progress/{ready['progress']}")
        self.assertEqual((await r.json())["state"], "queued")
        # the same binding cannot record twice
        with patch.object(self.server, "_engine", fake_engine):
            again = await self.ws_run({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"})
        self.assertIn("Записати продовження", again[-1]["message"])

    async def test_minerva_start_refusals(self):
        from unittest.mock import patch

        async def fake_engine(ctx, name):
            return None

        bid = await self.binding()
        with patch.object(self.server, "_engine", fake_engine):
            no_consent = await self.ws_run({"type": "start", "minerva": bid, "consent": {"announced": True}, "engine": "none"})
            self.assertIn("обидві галочки", no_consent[-1]["message"])
            self.fake.answers["status"] = [remote(403, "push_forbidden")]
            stale = await self.ws_run({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"})
            self.assertTrue(stale[-1]["stale"])
            self.assertIn("застаріло", stale[-1]["message"])
            self.assertEqual(list(self.cfg.sessions_dir.iterdir()), [])  # the empty folder is gone
            unknown = await self.ws_run({"type": "start", "minerva": "nope", "consent": CONSENT, "engine": "none"})
            self.assertIn("надто давно", unknown[-1]["message"])

    async def test_discard(self):
        from unittest.mock import patch

        async def fake_engine(ctx, name):
            return None

        bid = await self.binding()

        async def discard(ws):
            await ws.send_bytes(b"\x10\x00" * 3200)
            await ws.send_str(json.dumps({"type": "discard"}))

        with patch.object(self.server, "_engine", fake_engine):
            msgs = await self.ws_run({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"}, discard)
        self.assertEqual(msgs[-1]["type"], "discarded")
        self.assertNotIn("saved", [m["type"] for m in msgs])
        self.assertEqual(list(self.cfg.sessions_dir.iterdir()), [])
        self.assertEqual(self.ctx["link"].notices[0]["state"], "discarded")
        self.assertEqual(self.ctx["jobs"].status, {})                   # nothing transcribed


class LinkOrderTests(LinkTestBase):
    async def test_incomplete_upload_goes_first_again(self):
        sid, folder, _ = await self.bound()
        await self.pipeline(sid, folder)
        self.fake.answers["transcript"] = [{"ok": True, "complete": False}, {"ok": True, "complete": True}]
        await self.link.tick()
        uploads = [c[2]["upload_id"] for c in self.fake.calls if c[0] == "transcript"]
        self.assertEqual(len(set(uploads)), 2)                          # sent again under a new upload_id
        order = self.sent()
        self.assertLess(len(order) - order[::-1].index(("transcript", 1)), order.index(("moments", None)) + 1)
        self.assertEqual(self.link.states[sid]["outbox"], [])

    async def test_minerva_code_retry_waits_only_for_its_session(self):
        sid1, folder1, _ = await self.bound()
        sid2, _, _ = await self.bound(meeting=13)
        await self.link.on_live_end(sid2, {"duration": 1.0})
        self.link.states[sid1]["outbox"].append({"id": 99, "kind": "moments", "tries": 0, "next_at": 0})
        store.write_json(folder1 / "transcript.json", TRANSCRIPT_MN)
        store.write_json(folder1 / "moments.json", MOMENTS_MN)
        self.fake.answers["moments"] = [remote(409, "transcript_incomplete")]
        await self.link.tick()
        self.assertGreater(self.link.states[sid1]["outbox"][0]["next_at"], time.time())
        self.assertIn(("status", "recorded"), self.sent())              # the other meeting went on
        self.assertTrue(any(c[0] == "sync" for c in self.fake.calls))   # Minerva answered: the sync runs too


class LinkReviewTests(LinkTestBase):
    """Fixes after the independent review of 0.3.0 (11.11.2026)."""

    async def test_restarts_are_no_attempts_and_keep_the_audio(self):
        sid, folder, _ = await self.bound(retention="text_only")
        await self.link.on_live_end(sid, {"duration": 1.0})
        for _ in range(3):                                  # the RAG PC restarts while the transcription waits / runs
            self.jobs.status.clear()
            lk = self.new_link()
            await lk.resume()
        st = lk.states[sid]
        self.assertEqual((st["attempts"].get("transcribe", 0), st["failed"]), (0, {}))
        self.assertIsNotNone(self.store.audio_file(sid))   # nothing failed: the recording is not lost
        for _ in range(lk_max := self.link_mod.START_MAX):  # a step that never ends: stopped, but the audio stays
            self.jobs.status.clear()
            lk = self.new_link()
            await lk.resume()
        st = lk.states[sid]
        self.assertTrue(st["failed"]["transcribe"])
        self.assertIn(str(lk_max), st["failed"]["transcribe"])
        self.assertIsNotNone(self.store.audio_file(sid))
        # a real worker failure counts: 3 errors -> final, «лише текст» deletes the audio then
        sid2, _, _ = await self.bound(retention="text_only", meeting=13)
        await self.link.on_live_end(sid2, {"duration": 1.0})
        for _ in range(3):
            self.link.states[sid2]["retry_at"]["transcribe"] = 0
            await self.finish(sid2, "transcribe", "error", "boom")
            self.link.states[sid2]["retry_at"]["transcribe"] = 0
            await self.link.advance(sid2)
        self.assertEqual(self.link.states[sid2]["attempts"]["transcribe"], 3)
        self.assertIsNone(self.store.audio_file(sid2))

    async def test_discard_mark_survives_a_restart(self):
        sid, folder, _ = await self.bound()
        self.assertTrue(self.link.mark_discard(sid))       # the click is saved before the recording stops
        self.assertEqual(store.read_json(folder / "minerva.json")["delete"]["reason"], "discard")
        again = self.new_link()                             # ... and the RAG PC restarts right then
        await again.resume()
        self.assertEqual(self.jobs.submitted, [])           # the refused recording is never transcribed
        await again.tick()
        self.assertFalse(folder.exists())
        body = [c[2] for c in self.fake.calls if c[0] == "status"][-1]
        self.assertEqual(body["state"], "discarded")

    async def test_failed_transcription_keeps_marks_for_a_retry(self):
        sid, folder, _ = await self.bound(retention="audio_text")
        store.write_json(folder / "marks.json", [{"t": 0.3, "note": "", "kind": "user", "at": "2026-11-10T10:00:01+01:00"},
                                                 {"t": 0.5, "note": "consent_ok", "kind": "consent_ok"}])
        store.write_json(folder / "live.jsonl", {})
        await self.link.on_live_end(sid, {"duration": 1.0})
        for _ in range(3):
            self.link.states[sid]["retry_at"]["transcribe"] = 0
            await self.finish(sid, "transcribe", "error", "boom")
            self.link.states[sid]["retry_at"]["transcribe"] = 0
            await self.link.advance(sid)
        await self.link.tick()
        st = self.link.states[sid]
        self.assertTrue(st["failed"]["transcribe"])
        self.assertFalse(st["steps"].get("texts_deleted"))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["audio.wav", "marks.json", "meta.json", "minerva.json"])
        await self.link._verdict(sid, "retry")
        store.write_json(folder / "transcript.json", TRANSCRIPT_MN)
        await self.finish(sid, "transcribe")
        store.write_json(folder / "moments.json", MOMENTS_MN)
        await self.finish(sid, "moments")
        await self.link.tick()
        part = [c[2] for c in self.fake.calls if c[0] == "transcript"][-1]
        self.assertEqual([(m["note"], m["auto"]) for m in part["marks"]], [("", False), ("consent_ok", False)])
        self.assertEqual(part["marks"][0]["at"], "2026-11-10T10:00:01+01:00")
        self.assertTrue(st["steps"]["texts_deleted"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["audio.wav", "meta.json", "minerva.json"])

    async def test_refused_transcript_stays_for_a_retry(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        store.write_json(folder / "transcript.json", TRANSCRIPT_MN)
        await self.finish(sid, "transcribe")
        self.fake.answers["transcript"] = [remote(400, "invalid_request")]
        await self.link.tick()                               # the part is refused while the moments still run
        st = self.link.states[sid]
        self.assertTrue(st["failed"]["push"])
        store.write_json(folder / "moments.json", MOMENTS_MN)
        await self.finish(sid, "moments")                    # moments arrive later: not queued after a refused transcript
        self.assertFalse(any(i["kind"] == "moments" for i in st["outbox"]))
        await self.link.tick()
        self.assertTrue((folder / "transcript.json").exists())   # kept for «Спробувати ще раз»
        self.assertEqual(self.link.stage(sid, st), "failed")
        uploads = len({c[2]["upload_id"] for c in self.fake.calls if c[0] == "transcript"})
        await self.link._verdict(sid, "retry")
        await self.link.tick()
        self.assertEqual(len({c[2]["upload_id"] for c in self.fake.calls if c[0] == "transcript"}), uploads + 1)
        self.assertTrue(any(c[0] == "moments" for c in self.fake.calls))
        self.assertFalse(st["failed"]["push"])
        self.assertEqual(st["outbox"], [])

    async def test_transcript_incomplete_is_not_forever(self):
        sid, folder, _ = await self.bound()
        await self.pipeline(sid, folder)
        self.fake.answers["moments"] = [remote(409, "transcript_incomplete") for _ in range(30)]
        now = time.time()
        for k in range(25):
            await self.link.tick(now + k * 4000)
        st = self.link.states[sid]
        self.assertEqual(st["outbox"], [])
        self.assertTrue(st["failed"]["push"])
        errors = [c[2] for c in self.fake.calls if c[0] == "status" and c[2]["state"] == "error"]
        self.assertEqual(errors[-1]["stage"], "push")
        self.assertEqual(sum(1 for c in self.fake.calls if c[0] == "moments"), self.link_mod.INCOMPLETE_MAX + 1)

    async def test_retry_is_never_ignored(self):
        sid, folder, _ = await self.bound()
        await self.pipeline(sid, folder)
        await self.link.tick()                               # all delivered, texts deleted
        n = len(self.fake.calls)
        await self.link._verdict(sid, "retry")
        await self.link.tick(time.time() + 1)
        err = [c[2] for c in self.fake.calls[n:] if c[0] == "status"][-1]
        self.assertEqual((err["state"], err["stage"]), ("error", "push"))
        self.assertIn("Повторити нічого", err["message"])
        # a transcript that was never delivered (the queue was lost, no local failure recorded) is sent again
        sid2, folder2, _ = await self.bound(retention="audio_text", meeting=13)
        await self.pipeline(sid2, folder2)
        self.link.states[sid2]["outbox"].clear()
        m = len([c for c in self.fake.calls if c[0] == "transcript"])
        await self.link._verdict(sid2, "retry")
        await self.link.tick(time.time() + 1)
        self.assertGreater(len([c for c in self.fake.calls if c[0] == "transcript"]), m)
        self.assertTrue(any(c[0] == "moments" and c[2]["session"] == sid2 for c in self.fake.calls))
        # a job still running: Minerva hears so instead of nothing
        sid3, _, _ = await self.bound(meeting=14)
        await self.link.on_live_end(sid3, {"duration": 1.0})
        await self.link._verdict(sid3, "retry")
        self.assertIn("ще обробляє", self.link.states[sid3]["outbox"][-1]["data"]["detail"])

    async def test_waits_are_kept(self):
        sid, folder, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        await self.link.tick()
        self.jobs.status[sid].update(state="running", started=1.0, progress=0.1, kind="transcribe")
        self.link._progress()
        now = time.time()
        self.fake.answers["status"] = [remote(429, "rate_limited", retry_after=600)]
        await self.link.tick(now)
        head = self.link.states[sid]["outbox"][0]
        self.assertEqual((head["tries"], head["wait"]), (1, "after"))
        self.link.states[sid]["last_progress"]["at"] = 0
        self.jobs.status[sid]["progress"] = 0.2
        self.link._progress()                               # merged into the backed-off head: the wait stays
        head = self.link.states[sid]["outbox"][0]
        self.assertEqual((head["tries"], head["data"]["progress"]), (1, 20))
        self.assertGreaterEqual(head["next_at"], now + 600)
        sid2, _, _ = await self.bound(meeting=13)           # another meeting's success does not cut Retry-After short
        await self.link.on_live_end(sid2, {"duration": 1.0})
        await self.link.tick(now + 6)
        self.assertGreaterEqual(self.link.states[sid]["outbox"][0]["next_at"], now + 600)

    async def test_ack_needs_a_real_answer(self):
        sid, _, _ = await self.bound()
        await self.link.on_live_end(sid, {"duration": 1.0})
        self.fake.answers["status"] = [{}, {"detail": "html login page as json"}]
        await self.link.tick()
        head = self.link.states[sid]["outbox"][0]
        self.assertEqual((head["state"], head["tries"], head["wait"]), ("recorded", 1, "net"))
        from bench import minerva

        self.assertTrue(minerva.ack_ok("status", {"ok": True, "applied": False}))
        self.assertTrue(minerva.ack_ok("transcript", {"complete": False, "missing": [2]}))
        self.assertTrue(minerva.ack_ok("moments", {"added": 0, "skipped": 1}))
        self.assertFalse(minerva.ack_ok("moments", {"ok": "yes"}))
        self.assertFalse(minerva.ack_ok("status", []))

    async def test_one_removal_at_a_time(self):
        import asyncio

        sid, folder, _ = await self.bound()
        self.link.mark_discard(sid)
        await asyncio.gather(self.link.discard(sid), self.link._finish_deletions(), self.link._remove(sid))
        self.assertFalse(folder.exists())
        self.assertEqual([n["state"] for n in self.link.notices], ["discarded"])     # one notice, not two


class ReviewBuilderTests(unittest.TestCase):
    def test_short_lines_stay_under_1000_segments_per_part(self):
        from bench import minerva

        segs, _ = minerva.segments_of({"segments": [{"start": i, "end": i + 0.5, "ch": "LR"[i % 2], "text": "Ja."}
                                                    for i in range(20000)]})
        parts = minerva.split_parts({"uid": UID, "session": "s", "upload_id": "u"}, segs)
        self.assertEqual(max(b - a for a, b in parts), 1000)
        self.assertEqual(parts[-1][1], 20000)

    def test_languages_are_plain_codes_and_marks_carry_the_wall_clock(self):
        from bench import minerva
        from bench.live import LiveSession

        self.assertEqual(minerva.languages_of({"L": {"language": "de", "probability": 0.97}, "R": "uk", "M": {}, "X": "en"}),
                         {"L": "de", "R": "uk"})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = config.load(tmp.name)
        sid, folder, meta = store.Store(cfg).create("live", "x", mode="both")
        sess = LiveSession(cfg, folder, meta, None, lambda m: None)
        mark = sess.mark("consent_ok", "consent_ok")
        sess.stop()
        self.assertRegex(mark["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(minerva.marks_of([mark])[0]["at"], mark["at"])


class ServerReviewTests(unittest.IsolatedAsyncioTestCase):
    """The window side of the review fixes: discard saved first, a recording that never started, no LiveSession."""

    asyncSetUp = ServerMinervaTests.asyncSetUp
    asyncTearDown = ServerMinervaTests.asyncTearDown
    ws_run = ServerMinervaTests.ws_run
    binding = ServerMinervaTests.binding

    async def wait_idle(self):
        import asyncio

        for _ in range(200):
            if not self.ctx["live_lock"].locked():
                return
            await asyncio.sleep(0.02)

    async def test_discard_is_saved_before_the_stop(self):
        from unittest.mock import patch

        from bench import live

        async def fake_engine(ctx, name):
            return None

        seen = {}
        real_stop = live.LiveSession.stop

        def stop(sess, reason="stop"):
            seen["delete"] = (store.read_json(sess.folder / "minerva.json") or {}).get("delete")
            return real_stop(sess, reason)

        bid = await self.binding()

        async def discard(ws):
            await ws.send_bytes(b"\x10\x00" * 3200)
            await ws.send_str(json.dumps({"type": "discard"}))

        with patch.object(self.server, "_engine", fake_engine), patch.object(live.LiveSession, "stop", stop):
            msgs = await self.ws_run({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"}, discard)
        self.assertEqual(msgs[-1]["type"], "discarded")
        self.assertEqual(seen["delete"]["reason"], "discard")

    async def test_window_closed_during_the_claim(self):
        import asyncio
        from unittest.mock import patch

        async def fake_engine(ctx, name):
            return None

        bid = await self.binding()
        self.fake.answers["status"] = [lambda: (time.sleep(0.5), {"ok": True})[1]]
        with patch.object(self.server, "_engine", fake_engine):
            ws = await self.client.ws_connect("/ws", headers={"Origin": self.headers["Origin"]})
            await ws.send_str(json.dumps({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"}))
            await asyncio.sleep(0.1)
            await ws.close()
            await asyncio.sleep(0.2)
            await self.wait_idle()
        self.assertEqual(list(self.cfg.sessions_dir.iterdir()), [])
        self.assertEqual(self.ctx["jobs"].status, {})                   # nothing recorded, nothing transcribed
        self.assertEqual(self.ctx["link"].notices[0]["state"], "discarded")
        self.assertIn("Запис не почався", self.ctx["link"].notices[0]["data"]["detail"])

    async def test_recording_is_handed_over_when_the_window_goes_away(self):
        """aiohttp cancels the handler when the window disappears (and again inside its finally): the recording is still
        stopped and handed over to Minerva, not left until a restart."""
        import asyncio
        from unittest.mock import patch

        from aiohttp import WSMsgType

        async def fake_engine(ctx, name):
            return None

        bid = await self.binding()
        with patch.object(self.server, "_engine", fake_engine):
            ws = await self.client.ws_connect("/ws", headers={"Origin": self.headers["Origin"]})
            await ws.send_str(json.dumps({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"}))
            while True:
                msg = await asyncio.wait_for(ws.receive(), 10)
                if msg.type != WSMsgType.TEXT or json.loads(msg.data)["type"] == "ready":
                    break
            await ws.send_bytes(b"\x10\x00" * 32000)
            await asyncio.sleep(0.2)
            await ws.close()                                      # no «stop»: the tab was closed
            for _ in range(200):
                finish = self.ctx.get("live_finish")
                if finish is not None and finish.done():
                    break
                await asyncio.sleep(0.02)
        (sid, st), = self.ctx["link"].states.items()
        self.assertTrue(st["steps"].get("recorded"))
        self.assertEqual(self.ctx["jobs"].status[sid]["kind"], "transcribe")
        self.assertIsNone(self.ctx["live_sid"])
        self.assertGreater(self.ctx["store"].meta(sid)["duration"], 0)


    async def test_live_session_that_cannot_start(self):
        from unittest.mock import patch

        async def fake_engine(ctx, name):
            return None

        def broken(*a, **kw):
            raise OSError("disk full")

        bid = await self.binding()
        with patch.object(self.server, "_engine", fake_engine), patch.object(self.server, "LiveSession", broken):
            msgs = await self.ws_run({"type": "start", "minerva": bid, "consent": CONSENT, "engine": "none"})
        self.assertIn("Запис не почався", msgs[-1]["message"])
        self.assertEqual(list(self.cfg.sessions_dir.iterdir()), [])
        self.assertEqual(self.ctx["link"].notices[0]["state"], "discarded")  # Minerva does not stay «recording»
        self.assertIsNone(self.ctx["live_sid"])
        self.assertIsNone(self.ctx["live_engine"])


class QuoteParityTests(unittest.TestCase):
    """The same quote cases as Minerva's meetings/quotes.py: meetings/tests_quote_cases.py, loaded by its path."""

    def test_place_matches_minerva(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "tests_quote_cases.py"
        if not path.exists():
            self.skipTest("meetings/tests_quote_cases.py is not here (the RAG PC has only meetings/rag_pc)")
        spec = importlib.util.spec_from_file_location("meeting_quote_cases", path)
        cases = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cases)
        ordered = [(n, 0.0, "", text) for n, text in sorted(cases.LINES.items()) if text]
        by_no = {n: text for n, _, _, text in ordered}
        self.assertGreater(len(cases.CASES), 5)
        for name, quote, line, expected in cases.CASES:
            with self.subTest(name):
                self.assertEqual(moments.place(quote, line, by_no, ordered), expected)


if __name__ == "__main__":
    unittest.main()
