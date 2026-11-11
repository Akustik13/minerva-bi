"""Settings in <root>/data/config.json; missing keys fall back to DEFAULTS.

The tab «🎙 Наради» of the RAG GUI writes config.json while the bench runs; `Config.refresh()` (called for
every request) picks the change up, so allowed addresses, retention and the LLM choice apply without a restart.
"""
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("bench.config")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8791,
    "tls": True,
    # names written into the self-signed certificate (IP addresses and DNS names)
    "cert_names": ["192.168.2.68", "localhost", "127.0.0.1", "meet.sevskiy.com"],
    # who may open the bench: single addresses or networks ("192.168.2.0/24")
    "allowed_clients": ["127.0.0.1", "::1"],
    # reverse proxies whose X-Real-IP / X-Forwarded-For we trust (DSM later)
    "trusted_proxies": [],
    "retention_days": 14,
    "max_upload_mb": 1024,
    "ollama_url": "http://127.0.0.1:11434",
    # LLMs offered for key moments (only those installed in Ollama are shown)
    "llm_models": ["qwen3.5:9b-q4_K_M", "qwen3.5:4b-q4_K_M", "qwen2.5:7b"],
    # 2026-11-09 comparison on the GTX 1070: best Ukrainian, no invented items, 100 % in VRAM at 8K
    "llm_default": "qwen3.5:9b-q4_K_M",
    "llm_num_ctx": 8192,
    # which model the page proposes first: "ollama" (local, free) or "anthropic" (paid, by API key)
    "llm_provider": "ollama",
    "llm_model": "",  # empty: llm_default for Ollama, the first cloud model for a cloud provider
    # paid models offered per provider; the key itself lives in the RAG key store, never in this file
    "llm_cloud_models": {"anthropic": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    # effort per cloud model; empty: the defaults in cloud.MODELS (Opus medium, Sonnet low, Haiku medium)
    "llm_cloud_effort": {},
    # a cloud model has a 1M-token context: one request per meeting up to this many characters
    "llm_cloud_chunk_chars": 400000,
    "secrets_file": r"D:\RAG\provider-secrets.dpapi",
    "whisper_models": {"accurate": "large-v3", "fast": "large-v3-turbo"},
    "whisper_live_model": "large-v3-turbo",
    "whisper_compute_type": "int8",
    "whisper_beam_size": 5,
    "whisper_batch_size": 8,
    # free VRAM (MB) needed before a Whisper model starts on the GPU; measured 2026-11-09 on the
    # GTX 1070 with batch 8: large-v3 ~5.6 GB, turbo ~2.5 GB (live turbo, batch 1: ~1.2 GB)
    "vram_needed_mb": {"large-v3": 5800, "large-v3-turbo": 2800},
    "cpu_threads": 4,
    "live_threads": 4,
    "vad_threshold": 0.5,
    "vad_min_silence": 0.5,
    "vad_max_speech": 15.0,
    "partial_every": 1.2,
    # ---- Minerva (stage 1) ------------------------------------------------------------------------------------
    # https origin of Minerva, e.g. "https://sevskiy.minerva-bi.com"; empty = not connected. The service key lives in
    # the RAG key store (slot "minerva_meetings"), never in this file.
    "minerva_url": "",
    "minerva_poll_seconds": 120,         # sync with Minerva (what to keep / delete / retry) every N seconds
    "minerva_retry_max_seconds": 3600,   # longest pause between two tries of one undelivered item
    "minerva_binding_minutes": 240,      # a window opened from Minerva may start recording within this time
    "minerva_max_minutes": 240,          # a Minerva recording stops by itself after this long
    # recording without Minerva (stage-0 test bench: upload, list, metrics). Off in production: such recordings have
    # no consent protocol in Minerva and every allowed PC sees them.
    "standalone_enabled": False,
}


PROVIDERS = ("ollama", "anthropic")
# type of the values inside the dict settings (a key -> this type)
NESTED = {"whisper_models": str, "vram_needed_mb": int, "llm_cloud_models": list, "llm_cloud_effort": str}


def _typed(default, value):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, type(default))


def _nested_ok(kind, value):
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind is list:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, str) and bool(value)


def clean(values, problems=None):
    """DEFAULTS overlaid with the file, keeping only values of the expected type (a bad edit cannot break the bench).

    `problems` (a list) gets one Ukrainian line per value that was ignored; refresh() and load() show them."""
    out = dict(DEFAULTS)
    bad = []
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            out[key] = value  # unknown keys are kept for forward compatibility, but nothing reads them
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                bad.append(key)
                continue
            kind = NESTED.get(key, str)
            good = {k: v for k, v in value.items() if isinstance(k, str) and _nested_ok(kind, v)}
            if len(good) != len(value):
                bad.append(key)
            out[key] = {**default, **good}  # one level deep: a partial dict keeps the other defaults
            continue
        if isinstance(default, list) and isinstance(value, list):
            good = [v for v in value if isinstance(v, str)]  # one stray null must not drop the whole list of addresses
            if len(good) != len(value):
                bad.append(key)
            out[key] = good
        elif _typed(default, value):
            out[key] = value
        else:
            bad.append(key)
    if out["llm_provider"] not in PROVIDERS:
        bad.append("llm_provider")
        out["llm_provider"] = "ollama"
    if problems is not None and bad:
        problems.append("у config.json неправильні значення: " + ", ".join(dict.fromkeys(bad)) + " — для них діють стандартні")
    # this PC itself (the GUI tab «🎙 Наради», a browser on the RAG PC) is never locked out by a hand edit
    out["allowed_clients"] = [a for a in ("127.0.0.1", "::1") if a not in out["allowed_clients"]] + list(out["allowed_clients"])
    return out


def read_file(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config.json must hold an object")
    return data


def write_file(path, values):
    """Atomic write (temp file + replace), so the bench never reads half a file."""
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(values, f, ensure_ascii=False, indent=2)
    for attempt in range(40):  # Windows: the bench may be reading config.json this very moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


class Config(dict):
    """dict of settings plus the folder layout under root (D:\\Meeting)."""

    def __init__(self, root, values, mtime=None):
        super().__init__(values)
        self.root = Path(root)
        self.mtime = mtime
        self.overrides = {}
        self.error = None  # why the last change of config.json was not applied (shown on the page and in the GUI)

    @property
    def path(self):
        return self.data_dir / "config.json"

    def override(self, key, value):
        """A command-line value that survives refresh()."""
        self.overrides[key] = value
        self[key] = value

    def refresh(self):
        """Re-read config.json when it changed; an unreadable file keeps the current values. True if reloaded."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        self.mtime = mtime
        problems = []
        try:
            values = clean(read_file(self.path), problems)
        except (OSError, ValueError) as exc:
            self.error = f"config.json не прочитано ({exc}); діють попередні налаштування"
            log.warning("%s", self.error)
            return False
        values.update(self.overrides)
        # update first, then drop stale keys: a reader in another thread never sees a missing key
        self.update(values)
        for key in [k for k in self if k not in values]:
            del self[key]
        self.error = "; ".join(problems) or None
        if self.error:
            log.warning("%s", self.error)
        return True

    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def sessions_dir(self):
        return self.data_dir / "sessions"

    @property
    def models_dir(self):
        return self.root / "models"

    @property
    def logs_dir(self):
        return self.data_dir / "logs"

    @property
    def tls_dir(self):
        return self.data_dir / "tls"

    @property
    def parakeet_dir(self):
        return self.models_dir / "parakeet" / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

    @property
    def vad_model(self):
        return self.models_dir / "vad" / "silero_vad.onnx"

    @property
    def whisper_dir(self):
        return self.models_dir / "whisper"


def load(root):
    """config.json holds only what differs from DEFAULTS, so new defaults arrive with code updates.

    A broken file does not stop the bench (it would not start after a reboot): it starts with DEFAULTS, which allow
    only this PC, and cfg.error says why - on the page, in the RAG GUI tab and in bench.log."""
    root = Path(root)
    path = root / "data" / "config.json"
    problems, error = [], None
    if path.exists():
        try:
            values = clean(read_file(path), problems)
        except (OSError, ValueError) as exc:
            values = clean({})
            error = f"config.json не прочитано ({exc}); стенд запущено зі стандартними налаштуваннями (лише цей ПК)"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_file(path, {"allowed_clients": DEFAULTS["allowed_clients"]})
        values = dict(DEFAULTS)
    cfg = Config(root, values, mtime=path.stat().st_mtime)
    cfg.error = error or "; ".join(problems) or None
    for d in (cfg.sessions_dir, cfg.logs_dir, cfg.tls_dir, cfg.models_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
