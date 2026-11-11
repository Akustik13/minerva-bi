"""Minerva client for the recording window (stage 1). Standard library only (the RAG GUI may load this file too).

Talks only to /api/v1/meetings/... of ONE configured origin: no proxies (ProxyHandler({})), no redirects, TLS
certificates verified, 20 s timeout, answers up to 1 MB. Every failure becomes RemoteError with a Ukrainian message
that never contains the key; `classify` decides what the outbox does with it by the JSON `code` (not by the HTTP
status alone: during a Minerva deploy the proxy answers 502 / 504 HTML pages).

The payload builders read only what the session's JSON files hold (transcript.json, moments.json, marks.json, levels
from meta.json) — never audio. Protocol: meetings/docs/_stage1_work/stage1_spec.md §1.6 and §5.5.
"""
import hashlib
import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

KEY_SLOT = "minerva_meetings"        # RAG key store slot; "minerva" belongs to the public RAG's shop key
TIMEOUT = 20
CLAIM_TIMEOUT = 8                    # the `recording` status before capture starts
RESPONSE_CAP = 1 << 20
PART_BYTES = 512 * 1024              # one transcript part, json.dumps(ensure_ascii=False) encoded
PART_BYTES_MIN = 32 * 1024
PART_SEGMENTS = 1000                 # and at most this many segments (Minerva refuses > 5000 per part)
SEGMENT_TEXT_MAX = 2000
TOTAL_CHARS_MAX = 3_000_000
SUMMARY_MAX = 4000
TITLE_MAX = 300
WHO_MAX = 120
DUE_MAX = 120
QUOTE_MAX = 2000
DETAIL_MAX = 300
MARKS_MAX = 500
KINDS = ("decision", "task", "deadline", "risk", "question", "promise", "info")   # = moments.KINDS
CHANNELS = ("L", "R", "M")
LANGS = ("", "uk", "de", "en", "ru")
CONSENT_LANGS = ("de", "en", "uk")
RETENTIONS = ("text_only", "audio_text")
MARK_KINDS = ("user", "auto", "consent_ok", "consent_join")
MARK_LABELS = {"consent_ok": "✅ Згоду підтверджено вголос", "consent_join": "🙋 Новий учасник погодився"}

PATH_RE = re.compile(r"^/api/v1/meetings/(?:redeem/|sync/|ping/|[1-9]\d{0,11}/(?:status|transcript|moments)/)$")
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
UID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# codes Minerva answers with (JSON {code, message, retryable, request_id, details})
GONE = {"meeting_gone"}
UNKNOWN = {"meeting_unknown"}
BLOCKED = {"push_forbidden", "session_mismatch"}
DROP = {"invalid_request", "bad_request", "consent_missing"}
PAUSE = {"unauthorized", "forbidden_scope", "shop_key_forbidden"}
ALREADY = {"transcript_exists"}
STALE_CLAIM = GONE | UNKNOWN | BLOCKED

TEXTS = {
    "network": "Minerva не відповідає — немає з’єднання з RAG-ПК.",
    "timeout": "Minerva не відповіла вчасно.",
    "tls": "Не вдалося перевірити сертифікат Minerva (HTTPS).",
    "redirect": "Адреса Minerva переадресовує на іншу — у «RAG — керування» вкажіть точну адресу (https://…).",
    "too_big": "Відповідь Minerva завелика.",
    "bad_answer": "Minerva відповіла незрозуміло (не JSON) — можливо, саме оновлюється.",
    "unauthorized": "Minerva не прийняла ключ RAG-ПК.",
    "forbidden_scope": "Ключу RAG-ПК бракує права «🎙 Наради».",
    "shop_key_forbidden": "Це ключ магазину — для нарад потрібен окремий ключ RAG-ПК.",
    "meeting_gone": "Нараду видалено в Minerva.",
    "meeting_unknown": "Minerva не знає цієї наради.",
    "push_forbidden": "Цей запис більше не прив’язаний до наради: у Minerva нараду відкрили для запису знову.",
    "session_mismatch": "У Minerva до цієї наради вже прив’язано інший запис.",
    "consent_missing": "Minerva не отримала підтвердження згоди.",
    "ticket_invalid": "Посилання застаріло — натисніть «🎙 Відкрити вікно запису» в Minerva ще раз.",
    "ticket_expired": "Посилання застаріло — натисніть «🎙 Відкрити вікно запису» в Minerva ще раз.",
    "rate_limited": "Minerva просить зачекати (забагато запитів).",
    "transcript_incomplete": "Minerva ще складає розшифровку з частин.",
    "transcript_exists": "Розшифровка вже є в Minerva.",
    "payload_too_large": "Частина розшифровки завелика для Minerva.",
    "invalid_request": "Minerva не прийняла дані (некоректний запит).",
    "bad_request": "Minerva не прийняла дані (некоректний запит).",
}


class RemoteError(Exception):
    """status: HTTP status (0 — no answer); code: Minerva's JSON code or network / timeout / tls / redirect /
    bad_answer / too_big; coded: the code came from Minerva's JSON body (only then is it trusted)."""

    def __init__(self, status, code, message="", retryable=False, retry_after=None, coded=False):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or TEXTS.get(code) or f"Minerva відповіла помилкою {status}."
        self.retryable = retryable
        self.retry_after = retry_after
        self.coded = coded


def classify(err):
    """What the outbox does with a failed request: gone | unknown | blocked | drop | too_large | pause | done | retry.

    Only a code from Minerva's JSON body counts; HTML error pages, timeouts, TLS and anything unknown are transient."""
    if err.status == 413 or (err.coded and err.code == "payload_too_large"):
        return "too_large"
    if not err.coded:
        return "retry"
    for kind, codes in (("gone", GONE), ("unknown", UNKNOWN), ("blocked", BLOCKED), ("drop", DROP),
                        ("pause", PAUSE), ("done", ALREADY)):
        if err.code in codes:
            return kind
    return "retry"


def text_of(err):
    """Ukrainian, short, never the key or a transcript."""
    if isinstance(err, RemoteError):
        return err.message
    return "Minerva: невідома помилка."


def origin(url):
    """'https://host[:port]' of the configured address, or None. A path (/admin/) is ignored; userinfo, query and
    fragment are refused; plain http only for this PC (tests)."""
    url = (url or "").strip()
    if not url:
        return None
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        return None
    if p.username or p.password or p.query or p.fragment or not p.hostname:
        return None
    if not (p.scheme == "https" or (p.scheme == "http" and p.hostname in LOCAL_HOSTS)):
        return None
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    return f"{p.scheme}://{host}" + (f":{port}" if port else "")


def https_url(url, limit=500):
    """An absolute https link (the meeting page) or '' — the page shows only such a «← Повернутися в Minerva»."""
    url = str(url or "").strip()
    if not url or len(url) > limit:
        return ""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return ""
    return url


def token_sha256(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def now_iso(when=None):
    """ISO-8601 with the UTC offset (Minerva derives dates in Europe/Berlin from it)."""
    dt = datetime.fromtimestamp(when).astimezone() if when is not None else datetime.now().astimezone()
    return dt.isoformat(timespec="seconds")


def load_key(secrets_file=None):
    """The service key from the RAG key store (slot minerva_meetings) or ''. Lazy import: the GUI loads this file
    on its own and never calls it."""
    from . import keystore

    return keystore.get(KEY_SLOT, secrets_file)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        return None  # urlopen then raises HTTPError(3xx) -> RemoteError redirect


class Client:
    """One Minerva origin + the service key. Never logs or returns the key (repr hides it)."""

    def __init__(self, base, key, timeout=TIMEOUT, context=None, version=""):
        o = origin(base)
        if not o:
            raise ValueError("bad Minerva address")
        if not key:
            raise ValueError("no key")
        self.base = o
        self._key = key
        self.timeout = timeout
        self.agent = f"minerva-meeting-bench/{version or '?'}"
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if o.startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=context or ssl.create_default_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def __repr__(self):
        return f"<Minerva client {self.base}>"

    # ---- calls ------------------------------------------------------------------------------------------------
    def redeem(self, ticket):
        return self.call("POST", "/api/v1/meetings/redeem/", {"ticket": ticket})

    def push(self, meeting, kind, token, body, idem=None, timeout=None):
        if kind not in ("status", "transcript", "moments"):
            raise ValueError("unknown push kind")
        return self.call("POST", f"/api/v1/meetings/{int(meeting)}/{kind}/", body, token=token, idem=idem, timeout=timeout)

    def sync(self, body):
        """{sessions: [{meeting, uid, session, action}]} — a bare list of verdicts is accepted as well."""
        return self.call("POST", "/api/v1/meetings/sync/", body, wrap_list="sessions")

    def ping(self):
        return self.call("GET", "/api/v1/meetings/ping/")

    def call(self, method, path, body=None, token=None, idem=None, timeout=None, wrap_list=None):
        if not PATH_RE.match(path):
            raise ValueError("path not allowed")
        headers = {"Authorization": "Token " + self._key, "Accept": "application/json", "User-Agent": self.agent}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Meeting-Token"] = token
        if idem:
            headers["Idempotency-Key"] = idem
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                status = resp.status
                raw = resp.read(RESPONSE_CAP + 1)
        except urllib.error.HTTPError as e:
            raise _http_error(e) from None
        except urllib.error.URLError as e:
            raise _net_error(e.reason) from None
        except (ssl.SSLError, ssl.CertificateError) as e:
            raise _net_error(e) from None
        except (TimeoutError, socket.timeout) as e:
            raise _net_error(e) from None
        except (OSError, http.client.HTTPException) as e:
            raise _net_error(e) from None
        if len(raw) > RESPONSE_CAP:
            raise RemoteError(status, "too_big", retryable=True)
        try:
            value = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except ValueError:
            raise RemoteError(status, "bad_answer", retryable=True) from None
        if isinstance(value, list) and wrap_list:
            value = {wrap_list: value}
        if not isinstance(value, dict):
            raise RemoteError(status, "bad_answer", retryable=True)
        return value


def _net_error(reason):
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return RemoteError(0, "tls", retryable=True)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return RemoteError(0, "timeout", retryable=True)
    return RemoteError(0, "network", retryable=True)


def _retry_after(value, headers):
    for raw in (((value or {}).get("details") or {}).get("retry_after_seconds") if isinstance(value, dict) else None,
                (headers or {}).get("Retry-After")):
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= seconds <= 86400:
            return seconds
    return None


def _http_error(e):
    status = e.code
    if 300 <= status < 400:
        return RemoteError(status, "redirect", retryable=True)
    value = None
    try:
        value = json.loads((e.read(65536) or b"").decode("utf-8"))
    except Exception:  # noqa: BLE001 - an HTML page from the proxy: no code
        value = None
    headers = dict(e.headers or {})
    code = value.get("code") if isinstance(value, dict) else None
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{2,40}", code):
        return RemoteError(status, code, retryable=bool(value.get("retryable")), retry_after=_retry_after(value, headers),
                           coded=True)
    return RemoteError(status, f"http_{status}", f"Minerva відповіла помилкою {status}.", retryable=True,
                       retry_after=_retry_after(None, headers))


# ---- redeem answer ----------------------------------------------------------------------------------------------
def parse_redeem(data):
    """Minerva's redeem answer -> the binding fields kept on the RAG PC (ValueError when it is not usable)."""
    m = data.get("meeting") if isinstance(data, dict) else None
    token = data.get("push_token") if isinstance(data, dict) else None
    if not isinstance(m, dict) or not isinstance(token, str) or not TOKEN_RE.match(token):
        raise ValueError("no meeting or push token")
    mid = m.get("id")
    if not isinstance(mid, int) or isinstance(mid, bool) or not 0 < mid < 10**12:
        raise ValueError("bad meeting id")
    uid = str(m.get("uid") or "")
    if not UID_RE.match(uid):
        raise ValueError("bad meeting uid")
    texts = m.get("consent_texts") if isinstance(m.get("consent_texts"), dict) else {}

    def days(value, default, hi):
        return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= hi else default

    def consent(value):
        """{ask, after}: the question read before ▶ and the sentence said once the recording runs (a plain string
        is the question alone)."""
        if isinstance(value, str):
            value = {"ask": value}
        if not isinstance(value, dict) or not isinstance(value.get("ask"), str) or not value["ask"].strip():
            return None
        after = value.get("after") if isinstance(value.get("after"), str) else ""
        return {"ask": value["ask"].strip()[:8000], "after": after.strip()[:2000]}

    return {
        "meeting": mid, "uid": uid.lower(), "push_token": token,
        "title": str(m.get("title") or "").strip()[:200],
        "project_code": str(m.get("project_code") or "").strip()[:40],
        "retention": m.get("retention") if m.get("retention") in RETENTIONS else "text_only",
        "retention_days": days(m.get("retention_days"), 365, 36500),
        "audio_days": days(m.get("audio_days"), 30, 30),
        "lang_l": m.get("lang_l") if m.get("lang_l") in LANGS else "",
        "lang_r": m.get("lang_r") if m.get("lang_r") in LANGS else "",
        "consent_texts": {k: consent(texts[k]) for k in CONSENT_LANGS if consent(texts.get(k))},
        "consent_lang": m.get("consent_lang") if m.get("consent_lang") in CONSENT_LANGS else "de",
        "hotwords": str(m.get("hotwords") or "").strip()[:300],
        "return_url": https_url(m.get("return_url")),
    }


# ---- payloads (JSON files only, never audio) ----------------------------------------------------------------------
def _num(value, default=0.0):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if x == x and x not in (float("inf"), float("-inf")) else default


def segments_of(transcript):
    """transcript.json segments -> [{n, start, end, ch, text}], n = 1-based index (the `line` of the moments).

    Pre-validated with Minerva's rules (start ≥ 0, end ≥ start, ch L/R/M, text ≤ 2000) so a part is never refused for
    one odd segment; the whole transcript stops at 3 000 000 characters (returns (segments, truncated))."""
    out, total, truncated = [], 0, False
    for n, seg in enumerate((transcript or {}).get("segments") or [], 1):
        seg = seg if isinstance(seg, dict) else {}
        start = max(0.0, _num(seg.get("start")))
        end = max(start, _num(seg.get("end"), start))
        text = str(seg.get("text") or "").strip()[:SEGMENT_TEXT_MAX]
        if total + len(text) > TOTAL_CHARS_MAX:
            truncated = True
            break
        total += len(text)
        out.append({"n": n, "start": round(start, 2), "end": round(end, 2),
                    "ch": seg.get("ch") if seg.get("ch") in CHANNELS else "M", "text": text})
    return out, truncated


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def split_parts(base, segments, limit=PART_BYTES, max_segments=PART_SEGMENTS):
    """[(first, end)] index ranges of `segments` so that every request body {**base, segments: part} stays ≤ limit
    bytes when encoded the way Client.call sends it, with at most max_segments segments per part (short «Ja.» lines
    would otherwise put thousands into one part, and Minerva refuses more than 5000)."""
    room = max(1024, limit - encoded_size({**base, "segments": [], "part": 999, "parts": 999}) - 64)
    parts, first, size = [], 0, 0
    for i, seg in enumerate(segments):
        n = encoded_size(seg) + 2
        if i > first and (size + n > room or i - first >= max_segments):
            parts.append((first, i))
            first, size = i, 0
        size += n
    if segments:
        parts.append((first, len(segments)))
    return parts


def mark_kind(mark):
    """user (📌) | auto (microphone lost / back, pause, auto-stop) | consent_ok | consent_join."""
    note = str(mark.get("note") or "").strip()
    if note in MARK_LABELS:
        return note
    return mark.get("kind") if mark.get("kind") in MARK_KINDS else "user"


def marks_of(marks):
    """marks.json [{t, note, kind?}] -> [{t, note, auto}] as Minerva reads them: note consent_ok / consent_join ->
    the consent protocol; auto: true -> a marker in the transcript; every other (user) mark -> an item for ✓ / ✕."""
    out = []
    for m in (marks or [])[:MARKS_MAX]:
        if not isinstance(m, dict):
            continue
        kind = mark_kind(m)
        note = kind if kind in MARK_LABELS else str(m.get("note") or "").strip()[:200]
        mark = {"t": round(max(0.0, _num(m.get("t"))), 2), "note": note, "auto": kind == "auto"}
        # t counts recorded audio (a ⏸ pause is not in it); `at` is the wall clock for the consent protocol
        if isinstance(m.get("at"), str) and 10 <= len(m["at"]) <= 40:
            mark["at"] = m["at"]
        out.append(mark)
    return out


def levels_of(levels):
    """Only the peak per channel (no talk-time / sound share: that would be monitoring data)."""
    out = {}
    for ch, lv in (levels or {}).items():
        if ch in ("L", "R") and isinstance(lv, dict):
            peak = lv.get("peak_dbfs")
            out[ch] = {"peak_dbfs": round(_num(peak), 1) if peak is not None else None}
    return out


def languages_of(languages):
    """Whisper's {ch: {language, probability}} -> {ch: "de"} (the probability stays on the RAG PC)."""
    out = {}
    for ch, v in (languages or {}).items():
        lang = v.get("language") if isinstance(v, dict) else v
        if ch in CHANNELS and isinstance(lang, str) and lang.strip():
            out[ch] = lang.strip()[:8]
    return out


def ack_ok(kind, answer):
    """A real acknowledgement from Minerva: ok: true, or the documented fields of that call. Anything else (an empty
    body from a proxy, a login page turned JSON) is not proof of delivery."""
    if not isinstance(answer, dict):
        return False
    if answer.get("ok") is True:
        return True
    return {"status": "applied", "transcript": "complete", "moments": "added"}.get(kind) in answer


def moments_payload(base, moments, transcript):
    """Items with n / start / end / ch taken from transcript.segments[n-1] (the line moments.validate placed)."""
    segs = (transcript or {}).get("segments") or []
    items = []
    for it in (moments or {}).get("items") or []:
        if not isinstance(it, dict):
            continue
        try:
            n = int(it.get("line"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(segs):
            continue
        seg = segs[n - 1] if isinstance(segs[n - 1], dict) else {}
        start = max(0.0, _num(seg.get("start"), _num(it.get("start"))))
        items.append({
            "kind": it.get("kind") if it.get("kind") in KINDS else "info",
            "text": str(it.get("text") or "").strip()[:TITLE_MAX],
            "who": str(it.get("who") or "").strip()[:WHO_MAX],
            "due": str(it.get("due") or "").strip()[:DUE_MAX],
            "quote": str(it.get("quote") or "").strip()[:QUOTE_MAX],
            "n": n, "line": n, "start": round(start, 2), "end": round(max(start, _num(seg.get("end"), start)), 2),
            "ch": seg.get("ch") if seg.get("ch") in CHANNELS else "M",
        })
    return {**base, "model": str((moments or {}).get("model") or "")[:80], "provider": "ollama",
            "lang": (moments or {}).get("lang") if (moments or {}).get("lang") in ("uk", "de", "en", "ru") else "uk",
            "summary": str((moments or {}).get("summary") or "").strip()[:SUMMARY_MAX], "items": items,
            "rejected": len((moments or {}).get("rejected") or [])}
