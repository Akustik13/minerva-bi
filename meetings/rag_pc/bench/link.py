"""The recording window <-> Minerva (stage 1): bindings from tickets, per-session state in <sid>/minerva.json, the
durable outbox, sync, resume after a restart and safe deletion. Protocol: stage1_spec.md §5.5 / §5.7.

All state changes happen on the event loop (one thread: no locks needed); network calls, file reads of transcripts
and deletions run in the default executor (a blocked loop would drop a live recording).

Rules worth knowing:
- strict FIFO per session: only the head item is sent; a backed-off head blocks only its own session;
- every status carries seq (monotonic per session, persisted), at and started_at (ISO-8601 with offset);
- the transcript goes in parts of ≤ 512 KB (json, ensure_ascii=False), all with one upload_id; a 413 halves the part
  size and restarts the upload with a new upload_id;
- answers are classified by Minerva's JSON code (minerva.classify), everything unknown is transient;
- «лише текст»: audio is deleted as soon as transcript.json exists (also an empty one, and after the last failed try);
- a delete verdict from sync is never cached: it is a flag that the next sync may clear;
- deletion: content first, checked, then .deleting-<id> (store.remove); Minerva hears about a discarded / purged
  session from data/minerva-notices.json, sent only when the folder is really gone.
"""
import asyncio
import logging
import secrets
import time
import uuid
import wave
from pathlib import Path

from . import VERSION, minerva
from .store import read_json, valid_id, write_json

log = logging.getLogger("bench.link")

STATE = "minerva.json"
BINDINGS = "minerva-bindings.json"
NOTICES = "minerva-notices.json"
LOOP_SECONDS = 5
BACKOFF_FIRST = 30
MAX_ATTEMPTS = 3
RETRY_DELAYS = (120, 600)          # before the 2nd / 3rd automatic try of a failed transcription or LLM step
PROGRESS_EVERY = 10                # at most one «transcribing N %» status per this many seconds
HARD_CAP_DAYS = 30                 # every file of a Minerva session, whatever retention_days says
NOTICE_DAYS = 30
HOUSEKEEP_SECONDS = 600
UPLOAD_RESTARTS = 3
INCOMPLETE_MAX = 20                # «transcript_incomplete» answers before the moments are given up (error stage push)
START_MAX = 10                     # starts of one step that never ended (the RAG PC restarted each time)
SR = 16000
STAGES = ("transcribe", "moments")            # steps run on the RAG PC (attempts, retry_at)
FAILED_KEYS = ("transcribe", "moments", "push")   # final failures; push = Minerva refused the transcript / moments

NOT_CONNECTED = ("RAG-ПК ще не підключено до Minerva. Попросіть адміністратора: на RAG-ПК «RAG — керування» → "
                 "«🎙 Наради» — адреса Minerva й ключ.")
EXPIRED = ("Вікно запису відкрито надто давно або RAG-ПК перезапускався. Поверніться в Minerva й натисніть "
           "«🎙 Відкрити вікно запису» ще раз — це вікно можна закрити.")
STALE = "Це вікно запису застаріло — відкрийте його знову з Minerva (сторінка наради → «🎙 Відкрити вікно запису»)."
OFFLINE = ("⚠️ Minerva зараз не відповідає — запис іде, RAG-ПК передасть усе в Minerva, щойно вона знову буде на "
           "зв’язку.")


class LinkError(Exception):
    """A Ukrainian text for the page plus the HTTP status the bench answers with."""

    def __init__(self, text, status=400):
        super().__init__(text)
        self.text = text
        self.status = status


def wav_seconds(path):
    """Length of a WAV the live capture wrote (wave patches the header after every write, so even a recording cut by
    a crash is readable); from the file size when the header cannot be read."""
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate() or SR), 2)
    except (OSError, EOFError, wave.Error):
        try:
            return round(max(0, Path(path).stat().st_size - 44) / 4 / SR, 2)
        except OSError:
            return 0.0


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def created_ts(sid):
    try:
        return time.mktime(time.strptime(sid[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, TypeError):
        return time.time()


class Link:
    def __init__(self, cfg, store, jobs, live_sid=lambda: None):
        self.cfg = cfg
        self.store = store
        self.jobs = jobs
        self.live_sid = live_sid          # callable: id of the session recording right now (or None)
        self.stop_live = None             # set by the server: callable(sid, text) stops that live recording at once
        self.states = {}                  # sid -> minerva.json content (the only copy that is changed)
        self.bindings = {}
        self.reserved = set()
        self.notices = []
        self.paused = None
        self.last_ok = None
        self.last_error = None
        self.last_sync = None
        self.next_sync = 0.0
        self.next_housekeep = 0.0
        self.url_seen = None
        self.key_seen = None
        self.key_saved = False
        self.inflight = None
        self.tasks = set()
        self.advancing, self.again = set(), set()
        self.removing = set()
        self.make_client = lambda url, key: minerva.Client(url, key, version=VERSION)
        self.load_key = lambda: minerva.load_key(self.cfg.get("secrets_file"))

    # ---- files ---------------------------------------------------------------------------------------------------
    @property
    def bindings_file(self):
        return self.cfg.data_dir / BINDINGS

    @property
    def notices_file(self):
        return self.cfg.data_dir / NOTICES

    def folder(self, sid):
        return self.store.root / sid

    def load(self):
        b = read_json(self.bindings_file, {})
        self.bindings = b if isinstance(b, dict) else {}
        n = read_json(self.notices_file, [])
        self.notices = [x for x in n if isinstance(x, dict)] if isinstance(n, list) else []
        for sid in self.store.minerva_ids():
            st = read_json(self.folder(sid) / STATE)
            if isinstance(st, dict) and st.get("meeting"):
                self._defaults(st)
                self.states[sid] = st

    @staticmethod
    def _defaults(st):
        for key, value in (("outbox", []), ("steps", {}), ("attempts", {}), ("retry_at", {}), ("failed", {}),
                           ("delivered", {}), ("seq", 0), ("next_id", 1), ("part_bytes", minerva.PART_BYTES)):
            if not isinstance(st.get(key), type(value)):
                st[key] = value if not isinstance(value, (list, dict)) else type(value)()
        return st

    def _save(self, sid):
        st = self.states.get(sid)
        if st is None or not self.folder(sid).is_dir():
            return
        try:
            write_json(self.folder(sid) / STATE, st)
        except OSError:
            log.exception("%s: minerva.json not written", sid)

    def _save_bindings(self):
        now = time.time()
        self.bindings = {k: v for k, v in self.bindings.items() if isinstance(v, dict) and v.get("expires", 0) > now}
        try:
            write_json(self.bindings_file, self.bindings)
        except OSError:
            log.exception("minerva-bindings.json not written")

    def _save_notices(self):
        try:
            write_json(self.notices_file, self.notices)
        except OSError:
            log.exception("minerva-notices.json not written")

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # ---- connection ----------------------------------------------------------------------------------------------
    def url(self):
        return minerva.origin(self.cfg.get("minerva_url"))

    async def client(self):
        """(Client, None) or (None, 'url' | 'key'). A changed address or key resets the pause and every backoff."""
        url = self.url()
        key = await self._run(self.load_key) if url else ""
        sig = minerva.token_sha256(key) if key else None
        self.key_saved = bool(key)
        if (url, sig) != (self.url_seen, self.key_seen):
            if self.url_seen is not None or self.key_seen is not None:
                self.paused = None
                self._reset_backoff()
                self.next_sync = 0.0
            self.url_seen, self.key_seen = url, sig
        if not url:
            return None, "url"
        if not key:
            return None, "key"
        return self.make_client(url, key), None

    def _ok(self):
        self.last_ok = minerva.now_iso()
        self.last_error = None

    def _err(self, err):
        self.last_error = {"text": minerva.text_of(err), "at": minerva.now_iso()}

    def _pause(self, err):
        self.paused = {"code": err.code, "text": minerva.text_of(err), "at": minerva.now_iso()}
        self.next_sync = time.time() + int(self.cfg.get("minerva_poll_seconds") or 120)
        log.warning("minerva: paused (%s)", err.code)

    def _reset_backoff(self):
        """Minerva is reachable again: everything that waited only because it was not goes now. A wait Minerva asked
        for (Retry-After) or a Minerva answer such as transcript_incomplete keeps its time."""
        for item in [i for st in self.states.values() for i in st.get("outbox") or []] + self.notices:
            if item.get("wait") in (None, "net"):
                item["next_at"] = 0

    # ---- bindings (ticket -> window) ------------------------------------------------------------------------------
    async def redeem(self, ticket):
        """Ticket from the URL fragment -> a binding the page keeps (sessionStorage). Never retried automatically."""
        if not minerva.TICKET_RE.match(ticket or ""):
            raise LinkError("Посилання неповне — натисніть «🎙 Відкрити вікно запису» в Minerva ще раз.", 400)
        client, _ = await self.client()
        if client is None:
            raise LinkError(NOT_CONNECTED, 503)
        try:
            data = await self._run(client.redeem, ticket)
        except minerva.RemoteError as e:
            self._err(e)
            if e.coded and e.code in ("ticket_invalid", "ticket_expired"):
                raise LinkError(minerva.TEXTS[e.code], 410) from None
            if e.code == "timeout":
                raise LinkError("Minerva не відповіла вчасно — натисніть «🎙 Відкрити вікно запису» ще раз.", 504) from None
            if minerva.classify(e) == "pause":
                raise LinkError(f"{minerva.text_of(e)} Попросіть адміністратора перевірити ключ RAG-ПК "
                                "(«RAG — керування» → «🎙 Наради»).", 502) from None
            if e.status == 0 or not e.coded:
                raise LinkError(f"{minerva.text_of(e)} Спробуйте ще раз за хвилину.", 502) from None
            raise LinkError(f"Minerva не відкрила нараду: {minerva.text_of(e)}", 502) from None
        try:
            b = minerva.parse_redeem(data)
        except ValueError:
            raise LinkError("Minerva відповіла незрозуміло — можливо, її ще не оновлено для «🎙 Нарад».", 502) from None
        self._ok()
        now = time.time()
        # a new window for the same meeting replaces the older ones that were not used (double click, second tab)
        for bid in [k for k, v in self.bindings.items()
                    if v.get("meeting") == b["meeting"] and not v.get("used") and k not in self.reserved]:
            del self.bindings[bid]
        bid = secrets.token_urlsafe(24)
        b.update(created=now, expires=now + 60 * int(self.cfg.get("minerva_binding_minutes") or 240), used=False)
        self.bindings[bid] = b
        self._save_bindings()
        log.info("minerva: meeting %s opened in a recording window", b["meeting"])
        return bid, self.binding_view(bid)

    def binding_view(self, bid):
        """What the page shows (never the push token); None when unknown or expired."""
        b = self.bindings.get(bid) if isinstance(bid, str) else None
        if not b or b.get("expires", 0) <= time.time():
            return None
        return {"binding": bid, "title": b.get("title", ""), "project_code": b.get("project_code", ""),
                "retention": b.get("retention"), "audio_days": b.get("audio_days"), "retention_days": b.get("retention_days"),
                "consent_texts": b.get("consent_texts") or {}, "consent_lang": b.get("consent_lang") or "de",
                "lang_l": b.get("lang_l", ""), "lang_r": b.get("lang_r", ""), "return_url": b.get("return_url", ""),
                "expires_hm": _hm(b["expires"]), "used": bool(b.get("used")),
                "max_minutes": int(self.cfg.get("minerva_max_minutes") or 240)}

    def reserve(self, bid):
        """The WS start takes the binding for itself (two tabs with one binding cannot both record)."""
        b = self.bindings.get(bid) if isinstance(bid, str) else None
        if not b or b.get("expires", 0) <= time.time():
            raise LinkError(EXPIRED)
        if b.get("used"):
            raise LinkError("Цей запис уже передано на розшифровку. Зупинили випадково? У Minerva — «🎙 Записати "
                            "продовження».")
        if bid in self.reserved:
            raise LinkError("Запис цієї наради вже починається в іншій вкладці.")
        self.reserved.add(bid)
        return dict(b)

    def release(self, bid):
        self.reserved.discard(bid)

    def _consume(self, bid, sid=None):
        b = self.bindings.get(bid)
        if b:
            b["used"] = True
            b["sid"] = sid
            b.pop("push_token", None)
            self._save_bindings()

    def _drop_binding(self, bid):
        if self.bindings.pop(bid, None) is not None:
            self._save_bindings()

    # ---- claim: the `recording` status before any audio is kept ------------------------------------------------------
    async def claim(self, bid, sid, *, consent, mode, processing, lang_l, lang_r):
        """Writes minerva.json and pushes `recording` synchronously (≤ 8 s). Returns (offline, progress_token);
        offline=True: Minerva did not answer, the recording starts anyway and the status waits first in the outbox.
        Raises LinkError — the caller removes the (still empty) folder."""
        b = self.bindings.get(bid)
        if not b or not b.get("push_token"):
            raise LinkError(STALE, 409)
        progress = secrets.token_urlsafe(18)
        started = minerva.now_iso()
        st = self._defaults({
            "v": 1, "meeting": b["meeting"], "uid": b["uid"], "retention": b["retention"],
            "retention_days": b.get("retention_days", 365), "audio_days": b.get("audio_days", 30),
            "push_token": b["push_token"], "hotwords": b.get("hotwords", ""),
            "progress_sha": minerva.token_sha256(progress), "created_at": started, "started_at": started,
            "lang_l": lang_l, "lang_r": lang_r, "claiming": True,
        })
        self.states[sid] = st
        capture = {"mode": mode, "processing": processing}
        if processing in ("headset", "system"):
            # what the page knows: «Гарнітура» was picked, or «Без гарнітури, нарада в Teams / Zoom»
            capture["headset"] = processing == "headset"
        item = self._status(sid, "recording", save=False, consent=consent, capture=capture, lang_l=lang_l, lang_r=lang_r)
        self._save(sid)
        offline = False
        try:
            client, why = await self.client()
            if client is None:
                raise LinkError(NOT_CONNECTED, 503)
            try:
                await self._run(lambda: client.push(st["meeting"], "status", st["push_token"],
                                                    self._status_body(sid, st, item), self._idem(st, item),
                                                    minerva.CLAIM_TIMEOUT))
            except minerva.RemoteError as e:
                self._err(e)
                kind = minerva.classify(e)
                if e.coded and (e.code in minerva.STALE_CLAIM or kind in ("gone", "unknown", "blocked")):
                    self._drop_binding(bid)
                    raise LinkError(STALE, 409) from None
                if kind == "pause":
                    self._pause(e)
                    raise LinkError(f"{minerva.text_of(e)} Запис не почато. Попросіть адміністратора перевірити ключ "
                                    "RAG-ПК.", 502) from None
                if kind in ("drop", "too_large"):
                    raise LinkError(f"Minerva не прийняла початок запису: {minerva.text_of(e)}", 502) from None
                offline = True                                   # transient: start anyway, the status waits
                self._backoff(item, e, time.time())
            else:
                self._ok()
                st["outbox"] = [i for i in st["outbox"] if i["id"] != item["id"]]
        except LinkError:
            self.states.pop(sid, None)       # refused: the caller removes the (still empty) folder
            raise
        except BaseException:
            # cancelled (the window went away) or a bug: Minerva may already have `recording` — the session is
            # removed by the loop and Minerva hears `discarded`
            st.pop("claiming", None)
            st["delete"] = {"reason": "discard", "at": minerva.now_iso(),
                            "notice": ["discarded", {"detail": "Запис не почався: вікно запису закрили."}]}
            self._save(sid)
            raise
        st.pop("claiming", None)
        st["steps"]["claimed"] = True
        self._consume(bid, sid)
        self._save(sid)
        log.info("minerva: %s claimed for meeting %s%s", sid, st["meeting"], " (offline)" if offline else "")
        return offline, progress

    # ---- outbox items -------------------------------------------------------------------------------------------
    @staticmethod
    def _next_id(st):
        n = int(st.get("next_id") or 1)
        st["next_id"] = n + 1
        return n

    def _status(self, sid, state, save=True, **data):
        st = self.states[sid]
        st["seq"] = int(st.get("seq") or 0) + 1
        item = {"id": self._next_id(st), "kind": "status", "state": state, "seq": st["seq"], "at": minerva.now_iso(),
                "data": {k: v for k, v in data.items() if v is not None}, "tries": 0, "next_at": 0}
        ob = st["outbox"]
        if (state == "transcribing" and ob and ob[-1]["kind"] == "status" and ob[-1]["state"] == "transcribing"
                and self.inflight != (sid, ob[-1]["id"])):
            # only adjacent progress statuses are merged; a backed-off one keeps its tries and its wait
            for key in ("tries", "next_at", "wait", "error", "incomplete"):
                if key in ob[-1]:
                    item[key] = ob[-1][key]
            ob[-1] = item
        else:
            ob.append(item)
        if save:
            self._save(sid)
        return item

    def _status_body(self, sid, st, item):
        body = {"uid": st["uid"], "session": sid, "seq": item["seq"], "at": item["at"], "started_at": st.get("started_at"),
                "state": item["state"], "bench_version": VERSION}
        body.update(item.get("data") or {})
        return body

    @staticmethod
    def _idem(st, item):
        if item["kind"] == "status":
            return f"{st['uid']}:status:{item['seq']}"
        if item["kind"] == "transcript":
            return f"{st['uid']}:transcript:{item['upload_id']}:{item['part']}"
        return f"{st['uid']}:moments"

    def _transcript_base(self, sid, st, folder, tr, upload_id):
        meta = read_json(folder / "meta.json", {}) or {}
        base = {"uid": st["uid"], "session": sid, "upload_id": upload_id,
                "languages": minerva.languages_of(tr.get("languages")), "model": str(tr.get("model") or "")[:60],
                "duration": meta.get("duration")}
        marks = read_json(folder / "marks.json")
        if isinstance(marks, list):
            # no marks.json -> no `marks` key at all: Minerva keeps the marks it got with `recorded`
            base["marks"] = minerva.marks_of(marks)
        return base

    def _plan_transcript(self, sid, st, part_bytes, upload_id):
        """Executor: read transcript.json and split it -> (ranges, total segments, truncated)."""
        folder = self.folder(sid)
        tr = read_json(folder / "transcript.json")
        if not isinstance(tr, dict):
            raise FileNotFoundError("transcript.json")
        segs, truncated = minerva.segments_of(tr)
        base = self._transcript_base(sid, st, folder, tr, upload_id)
        return minerva.split_parts({**base, "total_segments": len(segs), "truncated": truncated}, segs, part_bytes), \
            len(segs), truncated

    async def _enqueue_transcript(self, sid, restart=False):
        st = self.states[sid]
        upload_id = uuid.uuid4().hex
        snapshot = {"uid": st["uid"]}
        try:
            ranges, total, truncated = await self._run(self._plan_transcript, sid, snapshot, st["part_bytes"], upload_id)
        except FileNotFoundError:
            return False
        if sid not in self.states:
            return False
        ob = st["outbox"]
        # a restart (413, or Minerva did not assemble the parts) happens while the transcript is the head of the
        # queue: the new upload goes first again, before the moments that need it
        pos = 0 if restart else len(ob)
        ob[:] = [x for x in ob if x["kind"] != "transcript"]
        items = [{"id": self._next_id(st), "kind": "transcript", "upload_id": upload_id, "part": k, "parts": len(ranges),
                  "first": a, "end": b, "tries": 0, "next_at": 0} for k, (a, b) in enumerate(ranges, 1)]
        ob[pos:pos] = items
        st["steps"].update(upload_id=upload_id, segments=total, truncated=truncated)
        if restart:
            st["steps"]["restarts"] = int(st["steps"].get("restarts") or 0) + 1
        else:
            st["steps"]["transcript"] = True
            self._status(sid, "transcribed", save=False, segments=total,
                         detail="Розшифровку обрізано: задовга (понад 3 млн знаків)." if truncated else None)
        self._save(sid)
        return True

    def _payload(self, sid, st, item):
        """Executor: the request body of a transcript part or the moments, rebuilt from the JSON files."""
        folder = self.folder(sid)
        tr = read_json(folder / "transcript.json")
        if not isinstance(tr, dict):
            raise FileNotFoundError("transcript.json")
        if item["kind"] == "transcript":
            segs, truncated = minerva.segments_of(tr)
            base = self._transcript_base(sid, st, folder, tr, item["upload_id"])
            return {**base, "part": item["part"], "parts": item["parts"], "total_segments": len(segs),
                    "truncated": truncated, "segments": segs[item["first"]:item["end"]]}
        mo = read_json(folder / "moments.json")
        if not isinstance(mo, dict):
            raise FileNotFoundError("moments.json")
        return minerva.moments_payload({"uid": st["uid"], "session": sid}, mo, tr)

    # ---- the loop -----------------------------------------------------------------------------------------------
    async def run_forever(self):
        try:
            await self.resume()
        except Exception:  # noqa: BLE001
            log.exception("minerva: resume failed")
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must keep running
                log.exception("minerva: tick failed")
            await asyncio.sleep(LOOP_SECONDS)

    async def resume(self):
        """After a (re)start: sessions found on disk continue where they stopped (step flags in minerva.json)."""
        self.load()
        await self.client()
        for sid, st in list(self.states.items()):
            if st.get("claiming"):                     # stopped in the middle of the claim: nothing was recorded
                st.pop("claiming", None)
                st["delete"] = {"reason": "claim", "at": minerva.now_iso()}
                self._save(sid)
            if not st.get("delete"):
                await self.advance(sid)

    async def tick(self, now=None):
        now = time.time() if now is None else now
        self.cfg.refresh()
        await self._finish_deletions()
        self._progress()
        for sid, st in list(self.states.items()):
            if any(0 < float(st["retry_at"].get(k) or 0) <= now for k in STAGES):
                await self.advance(sid)
        if now >= self.next_housekeep:
            self.next_housekeep = now + HOUSEKEEP_SECONDS
            await self.housekeeping(now)
        client, _ = await self.client()
        if client is None:
            return
        if self.paused:
            if now < self.next_sync:
                return
            if not await self._probe(client, now):
                return
        if await self._send_round(client, now) and now >= self.next_sync:
            await self.sync(client, now)

    async def _probe(self, client, now):
        self.next_sync = now + int(self.cfg.get("minerva_poll_seconds") or 120)
        try:
            await self._run(client.ping)
        except minerva.RemoteError as e:
            self._err(e)
            if minerva.classify(e) == "pause":
                self.paused = {"code": e.code, "text": minerva.text_of(e), "at": minerva.now_iso()}
            return False
        self.paused = None
        self._ok()
        self._reset_backoff()
        self.next_sync = 0.0
        return True

    async def _send_round(self, client, now):
        """False when a transient failure or a pause stopped the round (Minerva is not reachable now)."""
        for sid in list(self.states):
            st = self.states.get(sid)
            while st is not None and sid in self.states and not (st.get("claiming") or st.get("blocked") or st.get("delete")):
                ob = st["outbox"]
                if not ob or float(ob[0].get("next_at") or 0) > now:
                    break
                result = await self._send_item(client, sid, st, ob[0], now)
                if result is None:
                    return False
                if result is False:
                    break
        return await self._send_notices(client, now)

    async def _send_item(self, client, sid, st, item, now):
        """True — acknowledged (send the next); False — this session stops for now; None — stop the round."""
        try:
            if item["kind"] == "status":
                body = self._status_body(sid, st, item)
            else:
                body = await self._run(self._payload, sid, {"uid": st["uid"]}, dict(item))
        except FileNotFoundError:
            # the file the item refers to is gone (deleted by hand?): the item can never be delivered
            self._remove_item(st, item)
            if item["kind"] == "moments":
                st["failed"]["moments"] = "Файл ключових моментів зник на RAG-ПК."
                self._status(sid, "error", save=False, stage="moments", message=st["failed"]["moments"])
            self._save(sid)
            return True
        if sid not in self.states or not st["outbox"] or st["outbox"][0]["id"] != item["id"]:
            return False
        self.inflight = (sid, item["id"])
        try:
            answer = await self._run(lambda: client.push(st["meeting"], item["kind"], st["push_token"], body,
                                                         self._idem(st, item)))
            if not minerva.ack_ok(item["kind"], answer):
                # 2xx without a real acknowledgement (a proxy page, an empty body): not proof of delivery
                raise minerva.RemoteError(200, "bad_answer", retryable=True)
        except minerva.RemoteError as e:
            return await self._failed(sid, st, item, e, now)
        finally:
            self.inflight = None
        await self._acked(sid, st, item, answer)
        return True

    @staticmethod
    def _remove_item(st, item):
        st["outbox"][:] = [x for x in st["outbox"] if x["id"] != item["id"]]

    def _backoff(self, item, err, now):
        """Next try after 30 s·2ⁿ (≤ minerva_retry_max_seconds), never before Retry-After. `wait` says why: «net» —
        Minerva not reachable (any success elsewhere lets it go at once); «after» — Minerva asked to wait
        (Retry-After); «minerva» — Minerva answered with its own code (transcript_incomplete)."""
        item["tries"] = int(item.get("tries") or 0) + 1
        top = int(self.cfg.get("minerva_retry_max_seconds") or 3600)
        delay = min(BACKOFF_FIRST * 2 ** (item["tries"] - 1), top)
        item["next_at"] = now + max(delay, int(err.retry_after or 0))
        item["error"] = minerva.text_of(err)
        reachable = err.coded and err.status != 429 and err.status < 500
        item["wait"] = "after" if err.retry_after else "minerva" if reachable else "net"
        return reachable

    async def _failed(self, sid, st, item, err, now):
        self._err(err)
        kind = minerva.classify(err)
        log.info("minerva: %s %s %s -> %s (%s)", sid, item["kind"], item.get("state") or item.get("part") or "",
                 kind, err.code)
        if kind == "retry" and err.coded and err.code == "transcript_incomplete":
            item["incomplete"] = int(item.get("incomplete") or 0) + 1
            if item["incomplete"] > INCOMPLETE_MAX:
                kind = "drop"                   # Minerva never assembled the transcript: give the moments up
        if kind == "retry":
            # a Minerva answer such as transcript_incomplete: only this session waits (False); not reachable: the round
            # stops (None)
            reachable = self._backoff(item, err, now)
            self._save(sid)
            return False if reachable else None
        if kind == "pause":
            self._pause(err)
            return None
        if kind == "gone":
            st["delete"] = {"reason": "gone", "at": minerva.now_iso()}
            self._save(sid)
            return False
        if kind in ("unknown", "blocked"):
            st["blocked"] = {"code": err.code, "text": minerva.text_of(err), "at": minerva.now_iso()}
            self._save(sid)
            return False
        if kind == "done":                     # transcript_exists: Minerva already has a transcript of this meeting
            st["outbox"][:] = [x for x in st["outbox"] if x["kind"] != "transcript"]
            st["delivered"]["transcript"] = minerva.now_iso()
            self._save(sid)
            return True
        if kind == "too_large" and item["kind"] == "transcript" and st["part_bytes"] > minerva.PART_BYTES_MIN \
                and int(st["steps"].get("restarts") or 0) < UPLOAD_RESTARTS + 4:
            st["part_bytes"] = max(minerva.PART_BYTES_MIN, st["part_bytes"] // 2)
            await self._enqueue_transcript(sid, restart=True)
            return True
        # drop: Minerva will never accept this item as it is
        self._remove_item(st, item)
        if item["kind"] in ("transcript", "moments"):
            # without the transcript Minerva cannot take the moments either (they would wait for it forever); the files
            # stay on the RAG PC, so «🔄 Спробувати ще раз» in Minerva can send them again (failed push)
            if item["kind"] == "transcript":
                st["outbox"][:] = [x for x in st["outbox"] if x["kind"] not in ("transcript", "moments")]
            st["failed"]["push"] = minerva.text_of(err)[:300]
        if not (item["kind"] == "status" and item.get("state") == "error"):
            self._status(sid, "error", save=False, stage="push", message=minerva.text_of(err)[:minerva.DETAIL_MAX])
        self._save(sid)
        return True

    async def _acked(self, sid, st, item, answer):
        self._remove_item(st, item)
        self._ok()
        self._reset_backoff()
        if item["kind"] == "transcript" and item["part"] == item["parts"]:
            if answer.get("complete") is False:
                # the last part is in, but Minerva does not have them all (staging replaced): send the upload again
                if int(st["steps"].get("restarts") or 0) < UPLOAD_RESTARTS:
                    await self._enqueue_transcript(sid, restart=True)
                else:
                    st["outbox"][:] = [x for x in st["outbox"] if x["kind"] not in ("transcript", "moments")]
                    st["failed"]["push"] = "Minerva не склала розшифровку з частин після кількох спроб."
                    self._status(sid, "error", save=False, stage="push", message=st["failed"]["push"])
            else:
                st["delivered"]["transcript"] = minerva.now_iso()
        elif item["kind"] == "moments":
            st["delivered"]["moments"] = minerva.now_iso()
        self._save(sid)
        await self._cleanup_texts(sid)

    async def _send_notices(self, client, now):
        changed, reached = False, True
        for n in list(self.notices):
            if float(n.get("next_at") or 0) > now or not self.store.gone(n.get("session") or ""):
                continue
            body = {"uid": n["uid"], "session": n["session"], "seq": n["seq"], "at": n["at"],
                    "started_at": n.get("started_at"), "state": n["state"], "bench_version": VERSION, **(n.get("data") or {})}
            try:
                answer = await self._run(lambda n=n, body=body: client.push(n["meeting"], "status", n["push_token"], body,
                                                                             f"{n['uid']}:status:{n['seq']}"))
                if not minerva.ack_ok("status", answer):
                    raise minerva.RemoteError(200, "bad_answer", retryable=True)
            except minerva.RemoteError as e:
                self._err(e)
                kind = minerva.classify(e)
                if kind == "retry":
                    self._backoff(n, e, now)
                    changed, reached = True, False
                    break
                if kind == "pause":
                    self._pause(e)
                    reached = False
                    break
            else:
                self._ok()
            self.notices.remove(n)               # delivered, or Minerva will never take it
            changed = True
        if changed:
            self._save_notices()
        return reached

    def _notice(self, sid, st, state, **data):
        st["seq"] = int(st.get("seq") or 0) + 1
        self.notices.append({"meeting": st["meeting"], "uid": st["uid"], "session": sid, "push_token": st["push_token"],
                             "seq": st["seq"], "at": minerva.now_iso(), "started_at": st.get("started_at"),
                             "state": state, "data": data, "progress_sha": st.get("progress_sha"), "created": time.time(),
                             "tries": 0, "next_at": 0})
        self._save_notices()

    # ---- sync ---------------------------------------------------------------------------------------------------
    def stage(self, sid, st):
        if self.live_sid() == sid:
            return "recording"
        job = self.jobs.get(sid)
        if job and job["state"] == "queued":
            return "queued"
        if job and job["state"] == "running":
            return "transcribing" if job["kind"] == "transcribe" else "finding_moments"
        if any(st["failed"].get(k) for k in FAILED_KEYS):
            return "failed"
        if st["outbox"]:
            return "waiting_for_minerva"
        if st["steps"].get("done"):
            return "done"
        return "processing"

    def _entry(self, sid, st):
        job = self.jobs.get(sid) or {}
        progress = job.get("progress") if job.get("state") == "running" else None
        try:
            audio = self.store.audio_file(sid) is not None
        except (KeyError, OSError):
            audio = False
        return {"meeting": st["meeting"], "uid": st["uid"], "session": sid,
                "token_sha256": minerva.token_sha256(st["push_token"]), "stage": self.stage(sid, st),
                "progress": None if progress is None else int(progress * 100), "queue_pos": self.jobs.position(sid),
                "audio_on_bench": audio}

    async def sync(self, client=None, now=None):
        now = time.time() if now is None else now
        if client is None:
            client, _ = await self.client()
            if client is None:
                return False
        self.next_sync = now + int(self.cfg.get("minerva_poll_seconds") or 120)
        sessions = [self._entry(sid, st) for sid, st in self.states.items() if not st.get("claiming")]
        body = {"sessions": sessions, "bench": {
            "version": VERSION, "live_busy": bool(self.live_sid()), "queue": self.jobs.queue.qsize(),
            "outbox": sum(len(st["outbox"]) for st in self.states.values()) + len(self.notices),
            "failed": sum(1 for st in self.states.values() if any(st["failed"].get(k) for k in FAILED_KEYS)),
            "last_error": (self.last_error or {}).get("text", "")[:200],
            "retention_days": HARD_CAP_DAYS,        # every file of a Minerva session is gone after this many days
            "standalone": bool(self.cfg.get("standalone_enabled"))}}
        try:
            answer = await self._run(client.sync, body)
        except minerva.RemoteError as e:
            self._err(e)
            if minerva.classify(e) == "pause":
                self._pause(e)
            return False
        self._ok()
        self.last_sync = minerva.now_iso()
        for sid, action in self.verdicts(answer).items():
            if sid in self.states:
                await self._verdict(sid, action)
        await self._finish_deletions()
        return True

    @staticmethod
    def verdicts(answer):
        """{session: action} from {sessions | results | verdicts: [{meeting, uid, session, action}]} (or a bare list);
        the grouped form {delete: [...], retry: [...], drop_audio: [...]} is read too."""
        out = {}
        actions = ("keep", "delete", "unknown", "retry", "drop_audio")
        answer = {"sessions": answer} if isinstance(answer, list) else (answer if isinstance(answer, dict) else {})
        for key in ("sessions", "results", "verdicts"):
            for e in answer.get(key) or []:
                if isinstance(e, dict) and isinstance(e.get("session"), str) and e.get("action") in actions:
                    out[e["session"]] = e["action"]
        for action in actions:
            for e in answer.get(action) or []:
                if isinstance(e, dict) and isinstance(e.get("session"), str):
                    out[e["session"]] = action
        return out

    async def _verdict(self, sid, action):
        st = self.states[sid]
        if action != "delete" and (st.get("delete") or {}).get("reason") == "sync":
            st["delete"] = None                        # a delete verdict is never cached
        if action == "keep":
            if st.get("blocked"):
                st["blocked"] = None
                for item in st["outbox"]:
                    item["next_at"] = 0
        elif action == "unknown":
            st["blocked"] = {"code": "meeting_unknown", "text": minerva.TEXTS["meeting_unknown"], "at": minerva.now_iso()}
        elif action == "delete":
            if not st.get("delete"):
                st["delete"] = {"reason": "sync", "at": minerva.now_iso()}
        elif action == "retry":
            await self._retry(sid)
        elif action == "drop_audio":
            await self._drop_audio(sid, "Аудіо видалено на RAG-ПК (на прохання Minerva).")
        self._save(sid)

    async def _retry(self, sid):
        """«🔄 Спробувати ще раз» in Minerva. Never ignored silently: the failed step runs again (attempts from zero),
        a transcript / moments Minerva did not take are sent again, waiting items go at once — or an `error` says
        why nothing can be repeated."""
        st = self.states[sid]
        folder = self.folder(sid)
        try:
            has_audio = self.store.audio_file(sid) is not None
        except KeyError:
            return
        has_tr, has_mo = (folder / "transcript.json").exists(), (folder / "moments.json").exists()
        steps = st["steps"]
        job = self.jobs.get(sid) if self.jobs.busy(sid) else None
        if job or self.live_sid() == sid:
            state = "finding_moments" if job and job["kind"] == "moments" else "transcribing"
            self._status(sid, state, save=False, detail="RAG-ПК ще обробляє цей запис — повтор не потрібен.")
            self._save(sid)
            return
        failed = [k for k in FAILED_KEYS if st["failed"].get(k)]
        if failed:
            stage = failed[0]
            possible = {"transcribe": has_audio, "moments": has_tr or has_audio, "push": has_tr}[stage]
            if not possible:
                self._status(sid, "error", save=False, stage=stage,
                             message="Повторити неможливо: аудіо й розшифровки цього запису на RAG-ПК вже немає.")
                self._save(sid)
                return
            for k in FAILED_KEYS:
                st["failed"][k] = ""
            for k in STAGES:
                st["attempts"][k] = 0
                st["retry_at"][k] = 0
                steps.pop(f"starts_{k}", None)
            if stage == "transcribe":
                steps.pop("done", None)
            if stage == "push":
                for k in ("transcript", "moments", "done", "restarts"):   # the whole upload again
                    steps.pop(k, None)
            steps.pop("texts_deleted", None)
            state = {"transcribe": "transcribing", "moments": "finding_moments", "push": "transcribed"}[stage]
            self._status(sid, state, save=False, detail="Повторюю на прохання з Minerva.")
            self._save(sid)
            await self.advance(sid)
            return
        if st["outbox"]:
            for item in st["outbox"]:                    # Minerva is waiting for them: send now
                item["next_at"], item["wait"] = 0, None
            st["blocked"] = None
            self._save(sid)
            return
        if has_tr and not st["delivered"].get("transcript"):
            for k in ("transcript", "moments", "done", "restarts"):
                steps.pop(k, None)
            await self.advance(sid)
            return
        if has_mo and not st["delivered"].get("moments"):
            for k in ("moments", "done"):
                steps.pop(k, None)
            await self.advance(sid)
            return
        if not (has_tr or has_audio):
            why = "розшифровку вже передано й видалено на RAG-ПК" if steps.get("done") else \
                "аудіо й розшифровки цього запису на RAG-ПК вже немає"
            self._status(sid, "error", save=False, stage="push", message=f"Повторити нічого: {why}.")
            self._save(sid)
            return
        steps.pop("done", None)
        steps.pop("texts_deleted", None)
        await self.advance(sid)

    # ---- pipeline -----------------------------------------------------------------------------------------------
    def on_job_finish(self, sid, kind, state, text):
        """Jobs callback (event loop): push the outcome and start the next step."""
        if sid in self.states:
            self._spawn(self._job_finished(sid, kind, state, text))

    async def _job_finished(self, sid, kind, state, text):
        st = self.states.get(sid)
        if not st or st.get("delete"):
            return
        st["steps"].pop(f"starts_{kind}", None)        # the job ended: it was not cut by a restart
        if state == "done":
            st["retry_at"][kind] = 0
            st["last_error"] = ""
        elif state == "error":
            st["last_error"] = (text or "")[:300]
            # an attempt is counted only here, when a job really ended with an error (a restart of the RAG PC while a
            # job waits or runs is no failure and must never cost the recording)
            n = int(st["attempts"].get(kind) or 0) + 1
            st["attempts"][kind] = n
            if n >= MAX_ATTEMPTS:
                await self._final_error(sid, kind, text or "Не вдалося.", worker_failed=True)
            else:
                wait = RETRY_DELAYS[min(n - 1, len(RETRY_DELAYS) - 1)] if n > 0 else RETRY_DELAYS[0]
                st["retry_at"][kind] = time.time() + wait
                self._status(sid, "transcribing" if kind == "transcribe" else "finding_moments", save=False,
                             detail=f"Спроба {n} з {MAX_ATTEMPTS} не вдалася ({(text or '')[:150]}). "
                                    f"Повторю о {_hm(st['retry_at'][kind])}.")
        self._save(sid)
        await self.advance(sid)

    async def _final_error(self, sid, stage, message, worker_failed=False):
        """worker_failed: the transcription itself failed MAX_ATTEMPTS times — only then does «лише текст» delete the
        audio (it promised to keep it no longer than needed). Any other final failure keeps it for «Спробувати ще раз»."""
        st = self.states[sid]
        st["failed"][stage] = message[:300]
        self._status(sid, "error", save=False, stage=stage, message=message[:minerva.DETAIL_MAX])
        if worker_failed and stage == "transcribe" and st.get("retention") == "text_only":
            await self._drop_audio(sid, "Аудіо видалено на RAG-ПК.")
        self._save(sid)

    async def _drop_audio(self, sid, detail):
        st = self.states.get(sid)
        if not st or self.live_sid() == sid or self.jobs.busy(sid):
            return False
        try:
            if self.store.audio_file(sid) is None:
                return True
            ok = await self._run(self.store.delete_audio, sid)
        except KeyError:
            return False
        if ok and sid in self.states:
            st["audio_deleted_at"] = minerva.now_iso()
            self._status(sid, "audio_deleted", save=False, detail=detail)
            self._save(sid)
            log.info("minerva: %s audio deleted", sid)
        return ok

    def _enqueue_recorded(self, sid, meta, duration=None, interrupted=False):
        st = self.states[sid]
        folder = self.folder(sid)
        data = {"duration": duration if duration is not None else meta.get("duration"),
                "levels": minerva.levels_of(meta.get("levels")), "silent": [c for c in meta.get("silent") or [] if c in ("L", "R")],
                "marks": minerva.marks_of(read_json(folder / "marks.json", []))}
        if interrupted:
            data.update(interrupted=True, detail="Запис перервано: RAG-ПК перезапустився.")
        self._status(sid, "recorded", save=False, **data)
        st["steps"]["recorded"] = True
        self._save(sid)

    async def on_live_end(self, sid, meta):
        """The live recording ended (Stop, a closed tab, a lost connection): push `recorded` and transcribe."""
        st = self.states.get(sid)
        if not st:
            return
        if st.get("delete"):
            await self._finish_deletions()
            return
        if not st["steps"].get("recorded"):
            self._enqueue_recorded(sid, meta or {})
        await self.advance(sid)

    def _submit(self, sid, kind, params):
        """Queue a step. Not an attempt (those count only when a job ends with an error); `starts_<kind>` only stops
        a step that never ends because the RAG PC keeps restarting during it."""
        st = self.states[sid]
        st["retry_at"][kind] = 0
        try:
            self.jobs.submit(sid, kind, params)
        except Exception as exc:  # noqa: BLE001 - JobError: already queued
            log.info("minerva: %s %s not submitted: %s", sid, kind, exc)
            return False
        st["steps"][f"starts_{kind}"] = int(st["steps"].get(f"starts_{kind}") or 0) + 1
        if kind == "moments":
            self._status(sid, "finding_moments", save=False, detail="Шукаю ключові моменти (локальна модель на RAG-ПК).")
        self._save(sid)
        return True

    async def advance(self, sid, now=None):
        """Do the next step of the pipeline, judged from the step flags and the files (idempotent; also resumes).
        One run per session at a time: a call that comes while one runs (a job ended meanwhile) runs right after."""
        if sid in self.advancing:
            self.again.add(sid)
            return
        self.advancing.add(sid)
        try:
            await self._advance(sid, now)
        finally:
            self.advancing.discard(sid)
        if sid in self.again:
            self.again.discard(sid)
            await self.advance(sid)

    async def _advance(self, sid, now=None):
        now = time.time() if now is None else now
        st = self.states.get(sid)
        if not st or st.get("delete") or st.get("claiming") or self.live_sid() == sid or self.jobs.busy(sid):
            return
        folder = self.folder(sid)
        if not folder.is_dir():
            return
        steps, attempts = st["steps"], st["attempts"]
        if not steps.get("recorded"):
            # the recording was cut by a restart of the RAG PC: Minerva gets `recorded` with the length of the WAV
            duration = await self._run(wav_seconds, folder / "audio.wav")
            self._enqueue_recorded(sid, read_json(folder / "meta.json", {}) or {}, duration=duration, interrupted=True)
        if steps.get("done") or st["failed"].get("transcribe"):
            await self._cleanup_texts(sid)
            return
        try:
            has_audio = self.store.audio_file(sid) is not None
        except KeyError:
            return
        if not (folder / "transcript.json").exists():
            if not has_audio:
                await self._final_error(sid, "transcribe", "Аудіо запису на RAG-ПК немає — розшифрувати нічого.")
                return
            if int(attempts.get("transcribe") or 0) >= MAX_ATTEMPTS:
                await self._final_error(sid, "transcribe", st.get("last_error") or "Розшифровка не вдалася.",
                                        worker_failed=True)
                return
            if int(steps.get("starts_transcribe") or 0) >= START_MAX:
                await self._final_error(sid, "transcribe", f"Розшифровка не закінчилась: RAG-ПК перезапускався "
                                                           f"{START_MAX} разів під час неї. Аудіо лишається на RAG-ПК.")
                return
            if float(st["retry_at"].get("transcribe") or 0) > now:
                return
            self._submit(sid, "transcribe", {"model": "accurate", "device": "auto", "allow_unload": True,
                                             "lang_l": st.get("lang_l") or "auto", "lang_r": st.get("lang_r") or "auto"})
            return
        if st.get("retention") == "text_only" and has_audio:
            await self._drop_audio(sid, "Аудіо видалено на RAG-ПК одразу після розшифровки.")
        if not steps.get("transcript"):
            tr = await self._run(read_json, folder / "transcript.json")
            if isinstance(tr, dict) and (tr.get("segments") or []):
                if not await self._enqueue_transcript(sid):
                    return
            else:
                steps.update(transcript=True, segments=0)
                self._status(sid, "transcribed", save=False, segments=0)
        segments = int(steps.get("segments") or 0)
        if not segments:
            steps["done"] = True
            self._status(sid, "done", save=False, segments=0, items=0)
            self._save(sid)
            await self._cleanup_texts(sid)
            return
        if not (folder / "moments.json").exists():
            if st["failed"].get("moments"):
                await self._cleanup_texts(sid)
                return
            if int(attempts.get("moments") or 0) >= MAX_ATTEMPTS or int(steps.get("starts_moments") or 0) >= START_MAX:
                await self._final_error(sid, "moments", st.get("last_error") or "Ключові моменти знайти не вдалося.")
                await self._cleanup_texts(sid)
                return
            if float(st["retry_at"].get("moments") or 0) > now:
                return
            self._submit(sid, "moments", {"provider": "ollama", "model": "", "lang": "uk", "minerva": True})
            return
        if st["failed"].get("push"):
            # Minerva refused the transcript: the moments would wait for it forever; a retry from Minerva sends both
            await self._cleanup_texts(sid)
            return
        if not steps.get("moments"):
            steps["moments"] = True
            steps["items"] = await self._run(self._count_items, sid)
            st["outbox"].append({"id": self._next_id(st), "kind": "moments", "tries": 0, "next_at": 0})
        if not steps.get("done"):
            steps["done"] = True
            self._status(sid, "done", save=False, segments=segments, items=int(steps.get("items") or 0))
        self._save(sid)
        await self._cleanup_texts(sid)

    def _count_items(self, sid):
        """Executor: how many key moments the moments request will carry (the `items` of `done`)."""
        try:
            return len(self._payload(sid, {"uid": ""}, {"kind": "moments"})["items"])
        except FileNotFoundError:
            return 0

    async def _cleanup_texts(self, sid):
        """Everything delivered and final: the texts leave the RAG PC (meta.json and minerva.json stay for sync, the
        audio only while «аудіо + текст» keeps it). After a failed LLM step transcript.json stays for «Спробувати
        ще раз» (at most until the 30-day cap)."""
        st = self.states.get(sid)
        if not st or st["outbox"] or st["steps"].get("texts_deleted") or self.jobs.busy(sid):
            return
        failed = st["failed"]
        if not (st["steps"].get("done") or any(failed.get(k) for k in FAILED_KEYS)):
            return
        # what a «🔄 Спробувати ще раз» would still need stays (marks.json carries the 📌 and the consent marks)
        keep = set()
        try:
            if failed.get("transcribe") and self.store.audio_file(sid) is not None:
                keep |= {"marks.json"}
        except KeyError:
            return
        if failed.get("moments"):
            keep |= {"transcript.json", "marks.json"}
        if failed.get("push"):
            keep |= {"transcript.json", "moments.json", "marks.json"}
        try:
            ok = await self._run(self.store.delete_texts, sid, tuple(sorted(keep)))
        except KeyError:
            return
        if ok and sid in self.states and not keep:
            st["steps"]["texts_deleted"] = True
            self._save(sid)

    def _progress(self):
        """«transcribing N %» while the worker runs (merged, at most every PROGRESS_EVERY seconds)."""
        now = time.time()
        for sid, st in self.states.items():
            job = self.jobs.get(sid)
            if not job or job["state"] != "running" or job["kind"] != "transcribe":
                continue
            pct = int((job.get("progress") or 0) * 100)
            last = st.get("last_progress") or {}
            if last.get("job") != job.get("started") or (pct != last.get("value") and now - float(last.get("at") or 0) >= PROGRESS_EVERY):
                st["last_progress"] = {"job": job.get("started"), "value": pct, "at": now}
                self._status(sid, "transcribing", progress=pct)

    # ---- deletion -----------------------------------------------------------------------------------------------
    DISCARD_DETAIL = "Запис скасовано на RAG-ПК: учасник не погодився."

    def mark_discard(self, sid, detail=DISCARD_DETAIL):
        """Persist the decision «delete this recording» at once (synchronously, before the recording is stopped): a
        restart or a crash right after the click must never transcribe what someone refused. resume() and every step
        skip a session marked for deletion; the folder is removed as soon as it is free."""
        st = self.states.get(sid)
        if not st:
            return False
        st["delete"] = {"reason": "discard", "at": minerva.now_iso(), "notice": ["discarded", {"detail": detail}]}
        self._save(sid)
        return True

    async def discard(self, sid, detail=DISCARD_DETAIL):
        """«⛔ Хтось не згоден» (or a recording that never really started): deleted at once, no transcription;
        Minerva hears `discarded`."""
        st = self.states.get(sid)
        if not st:
            return
        if (st.get("delete") or {}).get("reason") != "discard":
            self.mark_discard(sid, detail)
        await self._remove(sid)

    def wants_stop(self, sid):
        st = self.states.get(sid)
        return bool(st and st.get("delete"))

    async def _finish_deletions(self):
        gone = await self._run(self.store.retry_deleting)
        if gone:
            log.info("minerva: finished deleting %s", ", ".join(gone))
        for sid, st in list(self.states.items()):
            if not st.get("delete") or sid in self.removing:
                continue
            if self.live_sid() == sid:
                # a discard is being stopped by its own window; anything else (deleted in Minerva) stops it now
                if self.stop_live and st["delete"].get("reason") != "discard":
                    self.stop_live(sid, "Нараду видалено в Minerva — запис зупинено й видалено.")
                continue
            if self.jobs.busy(sid):
                self.jobs.cancel(sid)
                continue
            await self._remove(sid)

    async def _remove(self, sid):
        """One removal per session at a time (discard / on_live_end and the loop's _finish_deletions may meet)."""
        st = self.states.get(sid)
        if st is None or sid in self.removing:
            return False
        if self.jobs.busy(sid):
            self.jobs.cancel(sid)
            return False
        self.removing.add(sid)
        try:
            result = await self._run(self.store.remove, sid)
        finally:
            self.removing.discard(sid)
        if result == "failed":
            log.warning("minerva: %s could not be deleted yet (files in use)", sid)
            return False
        if self.states.pop(sid, None) is None:
            return False
        notice = (st.get("delete") or {}).get("notice")
        if isinstance(notice, list) and len(notice) == 2:
            self._notice(sid, st, notice[0], **(notice[1] or {}))
        log.info("minerva: %s deleted (%s, %s)", sid, (st.get("delete") or {}).get("reason"), result)
        return True

    async def housekeeping(self, now=None):
        """30-day cap for every Minerva session; «аудіо + текст» audio after min(audio_days, retention_days)."""
        now = time.time() if now is None else now
        for sid, st in list(self.states.items()):
            if st.get("delete") or self.live_sid() == sid:
                continue
            age = (now - created_ts(sid)) / 86400
            if age > HARD_CAP_DAYS:
                st["delete"] = {"reason": "cap", "at": minerva.now_iso(),
                                "notice": ["purged", {"detail": "Запис видалено на RAG-ПК: минуло 30 днів."}]}
                self._save(sid)
                continue
            if st.get("retention") == "audio_text" and not st.get("audio_deleted_at"):
                days = min(int(st.get("audio_days") or 30), int(st.get("retention_days") or 30), HARD_CAP_DAYS)
                if age > days:
                    await self._drop_audio(sid, f"Аудіо видалено на RAG-ПК: минуло {days} днів.")
        await self._finish_deletions()
        limit = time.time() - NOTICE_DAYS * 86400
        if any(float(n.get("created") or 0) < limit for n in self.notices):
            self.notices = [n for n in self.notices if float(n.get("created") or 0) >= limit]
            self._save_notices()
        if any(v.get("expires", 0) <= now for v in self.bindings.values()):
            self._save_bindings()

    # ---- for the page and the RAG GUI ---------------------------------------------------------------------------
    def progress(self, token):
        """State of the recording for the window (never any text of the meeting); None when unknown."""
        if not isinstance(token, str) or not minerva.TOKEN_RE.match(token):
            return None
        sha = minerva.token_sha256(token)
        for sid, st in self.states.items():
            if st.get("progress_sha") == sha:
                return self._view(sid, st)
        for n in self.notices:
            if n.get("progress_sha") == sha:
                return {"state": n["state"], "progress": None, "done": True,
                        "text": "⛔ Запис зупинено й видалено." if n["state"] == "discarded" else "Запис видалено на RAG-ПК."}
        return None

    def _view(self, sid, st):
        stage = self.stage(sid, st)
        job = self.jobs.get(sid) or {}
        pct = int((job.get("progress") or 0) * 100) if stage == "transcribing" else None
        texts = {
            "recording": "🔴 Йде запис…",
            "queued": f"⏳ У черзі на розшифровку (попереду: {max(0, self.jobs.position(sid) - 1)})…",
            "transcribing": f"📝 Розшифровка… {pct or 0} %",
            "finding_moments": "🧠 Шукаю ключові моменти (локальна модель на RAG-ПК)…",
            "done": "✅ Готово — розшифровка вже на сторінці наради в Minerva.",
        }
        if st.get("blocked"):
            text = f"⚠️ {st['blocked']['text']}"
        elif stage == "failed":
            msg = next(st["failed"][k] for k in FAILED_KEYS if st["failed"].get(k))
            text = f"⚠️ {msg} Що далі — на сторінці наради в Minerva."
        elif stage == "waiting_for_minerva":
            text = ("📤 Передаю в Minerva… Minerva зараз не відповідає — RAG-ПК передасть усе, щойно вона буде на "
                    "зв’язку." if self.paused or self.last_error else "📤 Передаю в Minerva…")
        elif stage == "processing":
            due = [float(st["retry_at"].get(k) or 0) for k in STAGES if float(st["retry_at"].get(k) or 0) > time.time()]
            text = f"⏳ Повторю спробу о {_hm(min(due))}." if due else "⏳ Обробка…"
        else:
            text = texts[stage]
        return {"state": stage, "progress": pct, "text": text, "done": stage in ("done", "failed")}

    def status(self):
        """The `minerva` block of /api/status (the RAG GUI shows it): counts and times, no titles, no keys."""
        url = self.url()
        return {
            "configured": bool(url), "url": url or "", "url_error": bool(self.cfg.get("minerva_url") and not url),
            "key_saved": self.key_saved, "standalone": bool(self.cfg.get("standalone_enabled")),
            "sessions": len(self.states),
            "outbox": sum(len(st["outbox"]) for st in self.states.values()) + len(self.notices),
            "failed": sum(1 for st in self.states.values() if any(st["failed"].get(k) for k in FAILED_KEYS)),
            "blocked": sum(1 for st in self.states.values() if st.get("blocked")),
            "paused": self.paused, "last_ok": self.last_ok, "last_error": self.last_error, "last_sync": self.last_sync,
        }

    def is_minerva_job(self, sid):
        return sid in self.states or (valid_id(sid or "") and (self.folder(sid) / STATE).exists())
