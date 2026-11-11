"""aiohttp app: the page, JSON API, live capture over WebSocket, uploads, downloads.

Stage 1: a window opened from Minerva (#minerva=<ticket>) redeems the ticket here (/api/minerva/redeem), records with
the consent confirmed right before ▶ and hands the session to bench/link.py. Minerva sessions are private: no {sid}
route serves them. Without Minerva the stage-0 test bench (upload, list, metrics) works only with standalone_enabled.
"""
import asyncio
import csv
import gc
import html
import io
import json
import logging
import re
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import VERSION, access, capture, cloud, config, gpu, keystore, minerva
from .jobs import JobError, Jobs
from .link import EXPIRED, OFFLINE, Link, LinkError
from .live import SR, LiveSession
from .moments import SPEAKERS, clock
from .store import Store, read_json

log = logging.getLogger("bench.server")

STATIC = Path(__file__).resolve().parent / "static"
HELP = Path(__file__).resolve().parent.parent / "docs" / "help_uk.md"  # the same text as the RAG GUI tab «🎙 Наради»
UPLOAD_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".webm", ".mp4", ".mkv", ".mov",
              ".flac", ".wma", ".amr", ".3gp", ".mpga", ".mpeg"}
LANGS = {"", "auto", "uk", "de", "en", "ru", "pl", "fr", "it", "es"}
APP_KEY = web.AppKey("bench", dict)
STANDALONE_OFF = ("Тестові записи без Minerva вимкнено. Нараду записують із Minerva: сторінка наради → "
                  "«🎙 Відкрити вікно запису».")
BUSY = "Зараз на RAG-ПК уже йде інший запис. Одночасно — лише один. Спробуйте, коли він закінчиться."
NOT_STARTED = "Запис не почався: "
REFUSED_PAGE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вікно запису наради</title></head>
<body>
<h1>⛔ Цей ПК ще не може відкривати вікно запису</h1>
<p>Адреса цього ПК: <b>{ip}</b>.</p>
<p>Попросіть адміністратора на RAG-ПК:</p>
<ol>
<li>відкрити «RAG — керування» → вкладку «🎙 Наради»;</li>
<li>у полі «Дозволені адреси» дописати {ip} і натиснути «💾 Зберегти налаштування»;</li>
<li>там же натиснути «📋 Копіювати команду брандмауера» й виконати її в PowerShell від адміністратора.</li>
</ol>
<p>Потім оновіть цю сторінку.</p>
</body></html>
"""


def _bench(request):
    return request.app[APP_KEY]


def _scheme(request, cfg):
    if access.allowed(request.remote, cfg["trusted_proxies"]):
        proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
        if proto in ("http", "https"):
            return proto
    return request.scheme


def _csp(request):
    host = request.headers.get("Host", "")
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"media-src 'self' blob:; connect-src 'self' wss://{host}; worker-src 'self' blob:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _secure(request, resp):
    resp.headers["Content-Security-Policy"] = _csp(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


@web.middleware
async def guard(request, handler):
    ctx = _bench(request)
    cfg = ctx["cfg"]
    if cfg.refresh():
        log.info("config.json changed: settings reloaded")
    ip = access.client_ip(request.remote, request.headers, cfg["trusted_proxies"])
    request["client_ip"] = ip
    if not access.allowed(ip, cfg["allowed_clients"]):
        log.warning("refused %s %s", ip, request.path)
        if request.method in ("GET", "HEAD") and not request.path.startswith(("/api/", "/static/", "/ws")):
            # a person opened the window from Minerva on a PC that is not allowed yet: say what to do
            resp = web.Response(status=403, text=REFUSED_PAGE.format(ip=html.escape(ip)), content_type="text/html",
                                charset="utf-8")
            resp.headers["Cache-Control"] = "no-store"
            return _secure(request, resp)
        return web.Response(status=403, text=f"Доступ лише з дозволених адрес. Ваша адреса: {ip}\n",
                            content_type="text/plain", charset="utf-8")
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    if request.method not in ("GET", "HEAD") or is_ws:
        via_proxy = access.allowed(request.remote, cfg["trusted_proxies"])
        if not access.same_origin(request.headers, _scheme(request, cfg), via_proxy):
            return web.json_response({"error": "Запит не з цієї сторінки."}, status=403)
        if not is_ws and request.headers.get("X-Bench") != "1":
            return web.json_response({"error": "Запит не з цієї сторінки."}, status=403)
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc
    if not is_ws and isinstance(resp, web.StreamResponse) and not resp.prepared:
        resp.headers["Content-Security-Policy"] = _csp(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Permissions-Policy"] = "microphone=(self), display-capture=(self), camera=()"
        if request.path.startswith("/api/") or request.path == "/":
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            # revalidate every time (ETag -> 304): an updated worklet must not run from the browser cache for hours
            resp.headers["Cache-Control"] = "no-cache"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


# ---- pages and status ---------------------------------------------------------------------------
async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def help_text(request):
    try:
        text = HELP.read_text(encoding="utf-8-sig")
    except ValueError:  # saved by another editor in another encoding: show what can be read
        text = HELP.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = "# Довідки немає\n\nФайл docs/help_uk.md не знайдено."
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


def _whisper_ready(cfg, name):
    if "/" in name:
        repo = name.replace("/", "--")
    elif name in ("large-v3-turbo", "turbo"):
        repo = "mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    else:
        repo = f"Systran--faster-whisper-{name}"
    snaps = cfg.whisper_dir / f"models--{repo}" / "snapshots"
    return snaps.is_dir() and any(snaps.glob("*/model.bin"))


async def status(request):
    ctx = _bench(request)
    cfg = ctx["cfg"]
    loop = asyncio.get_running_loop()
    mem, loaded, installed = await asyncio.gather(
        loop.run_in_executor(None, gpu.memory),
        loop.run_in_executor(None, gpu.ollama_loaded, cfg["ollama_url"]),
        loop.run_in_executor(None, gpu.ollama_installed, cfg["ollama_url"]),
    )
    jobs = ctx["jobs"]
    offered = await loop.run_in_executor(None, cloud.choices, cfg, installed)
    return web.json_response({
        "version": VERSION, "client_ip": request["client_ip"], "gpu": mem, "ollama_loaded": loaded,
        "llm_models": [m for m in cfg["llm_models"] if m in installed], "llm_default": cfg["llm_default"],
        "llm": {"choices": offered, "default": cloud.default_choice(cfg, offered)},
        "config_error": cfg.error,
        "whisper": {k: {"model": v, "ready": _whisper_ready(cfg, v)} for k, v in cfg["whisper_models"].items()},
        "parakeet": {"ready": (cfg.parakeet_dir / "tokens.txt").exists(), "loaded": ctx.get("parakeet") is not None},
        "vad_ready": cfg.vad_model.exists(),
        "live_busy": ctx["live_lock"].locked(), "live_engine": ctx.get("live_engine"),
        "job": _current_job(ctx), "queue": jobs.queue.qsize(),
        "retention_days": cfg["retention_days"], "max_upload_mb": cfg["max_upload_mb"],
        "standalone": bool(cfg.get("standalone_enabled")), "minerva": ctx["link"].status(),
    })


def _current_job(ctx):
    """The running job for the chips and the RAG GUI: never its params (hotwords); for a Minerva meeting only the
    kind, state and progress."""
    jobs = ctx["jobs"]
    st = jobs.status.get(jobs.current) if jobs.current else None
    if not st:
        return None
    if ctx["link"].is_minerva_job(jobs.current):
        return {"kind": st.get("kind"), "state": st.get("state"), "progress": st.get("progress"), "minerva": True}
    return _job_view(st)


# ---- sessions (the stage-0 test bench) -----------------------------------------------------------
def _test_bench(request):
    if not _bench(request)["cfg"].get("standalone_enabled"):
        raise web.HTTPForbidden(text=json.dumps({"error": STANDALONE_OFF}, ensure_ascii=False),
                                content_type="application/json")


def _sid(request):
    """A test recording of this request; a Minerva session (or a folder without readable meta) is never served."""
    _test_bench(request)
    sid = request.match_info["sid"]
    try:
        if _bench(request)["store"].private(sid):
            raise KeyError(sid)
    except KeyError:
        raise web.HTTPNotFound(text="Запис не знайдено")
    return sid


async def sessions(request):
    _test_bench(request)
    ctx = _bench(request)
    items = ctx["store"].list()
    for it in items:
        it["job"] = _job_view(ctx["jobs"].get(it["id"]))
    return web.json_response({"sessions": items})


def _job_view(st):
    if not st:
        return None
    return {k: st.get(k) for k in ("kind", "state", "text", "progress", "started", "finished")}


async def session(request):
    ctx = _bench(request)
    sid = _sid(request)
    store = ctx["store"]
    folder = store.path(sid)
    return web.json_response({
        "meta": store.meta(sid),
        "live": store.live_lines(sid),
        "marks": read_json(folder / "marks.json", []),
        "transcript": read_json(folder / "transcript.json"),
        "moments": read_json(folder / "moments.json"),
        "moments_runs": read_json(folder / "moments_runs.json", []),
        "error": read_json(folder / "error.json"),
        "job": _job_view(ctx["jobs"].get(sid)),
        "has_audio": store.audio_file(sid) is not None,
    })


async def audio(request):
    ctx = _bench(request)
    f = ctx["store"].audio_file(_sid(request))
    if f is None:
        raise web.HTTPNotFound(text="Аудіо немає")
    return web.FileResponse(f, headers={"Cache-Control": "no-store"})


async def export_txt(request):
    ctx = _bench(request)
    sid = _sid(request)
    folder = ctx["store"].path(sid)
    meta = ctx["store"].meta(sid)
    tr = read_json(folder / "transcript.json")
    out = io.StringIO()
    out.write(f"{meta.get('title') or sid}\n{meta.get('created', '')}\n\n")
    if tr and tr.get("segments"):
        for seg in tr["segments"]:
            who = SPEAKERS.get(seg.get("ch"), "")
            out.write(f"[{clock(seg['start'])}] {who + ': ' if who and seg.get('ch') != 'M' else ''}{seg['text']}\n")
    else:
        for line in ctx["store"].live_lines(sid):
            out.write(f"[{clock(line['t0'])}] {SPEAKERS.get(line['ch'], '')}: {line['text']}\n")
    name = re.sub(r"[^\w\-]+", "_", meta.get("title") or sid, flags=re.U).strip("_")[:60] or sid
    return web.Response(text=out.getvalue(), content_type="text/plain", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{sid}.txt"; filename*=UTF-8\'\'{_q(name)}.txt'})


def _q(text):
    from urllib.parse import quote

    return quote(text, safe="")


async def rename(request):
    ctx = _bench(request)
    sid = _sid(request)
    data = await request.json()
    meta = ctx["store"].update_meta(sid, title=str(data.get("title") or "").strip()[:200])
    return web.json_response({"ok": True, "title": meta["title"]})


async def delete(request):
    ctx = _bench(request)
    sid = _sid(request)
    st = ctx["jobs"].get(sid)
    if st and st["state"] in ("queued", "running"):
        return web.json_response({"error": "Спершу дочекайтесь завершення завдання."}, status=409)
    if ctx.get("live_sid") == sid:
        return web.json_response({"error": "Цей запис ще йде."}, status=409)
    result = await asyncio.get_running_loop().run_in_executor(None, ctx["store"].delete, sid)
    if result == "failed":
        return web.json_response({"error": "Файли запису зараз зайняті — спробуйте за хвилину."}, status=409)
    log.info("deleted %s by %s", sid, request["client_ip"])
    return web.json_response({"ok": True})


async def transcribe(request):
    ctx = _bench(request)
    sid = _sid(request)
    data = await request.json()
    params = {
        "model": data.get("model") if data.get("model") in ("accurate", "fast") else "accurate",
        "device": data.get("device") if data.get("device") in ("auto", "gpu", "cpu") else "auto",
        "allow_unload": bool(data.get("allow_unload", True)),
        "hotwords": str(data.get("hotwords") or "")[:300],
    }
    for key in ("lang", "lang_l", "lang_r"):
        if key in data and data[key] in LANGS:
            params[key] = data[key]
    try:
        st = ctx["jobs"].submit(sid, "transcribe", params)
    except JobError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({"ok": True, "job": _job_view(st)})


async def find_moments(request):
    ctx = _bench(request)
    cfg = ctx["cfg"]
    sid = _sid(request)
    data = await request.json()
    provider, _, model = str(data.get("choice") or "").partition(":")
    if not model:  # a page opened before 0.2.0 sends only {"model"}
        provider, model = "ollama", str(data.get("model") or cfg["llm_default"])
    model = model[:80]
    if provider not in config.PROVIDERS:
        return web.json_response({"error": "Невідомий постачальник моделі."}, status=400)
    if provider != "ollama":
        if model not in ((cfg.get("llm_cloud_models") or {}).get(provider) or []) or model not in cloud.MODELS:
            return web.json_response({"error": "Цієї платної моделі немає в дозволених (налаштування на вкладці «🎙 Наради»)."}, status=400)
        # consent is given for THIS recording: the page sends the recording id, a bare "true" is not enough
        if data.get("cloud_consent") != sid:
            return web.json_response({"error": "Поставте галочку: учасники погодились на обробку тексту в хмарі."}, status=400)
        key = await asyncio.get_running_loop().run_in_executor(None, keystore.get, provider, cfg.get("secrets_file"))
        if not key:
            return web.json_response({"error": "Ключ Anthropic не збережено — додайте його в «RAG — керування» → «🎙 Наради»."}, status=400)
    params = {"provider": provider, "model": model,
              "lang": data.get("lang") if data.get("lang") in ("uk", "de", "en", "ru") else "uk"}
    try:
        st = ctx["jobs"].submit(sid, "moments", params)
    except JobError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    if provider != "ollama":
        # written right after the run is queued (no await in between, so the job sees it), never for a refused run
        ctx["store"].update_meta(sid, cloud_consent={"provider": provider, "model": model,
                                                     "at": time.strftime("%Y-%m-%d %H:%M:%S"), "client": request["client_ip"]})
        log.info("cloud consent %s %s:%s by %s", sid, provider, model, request["client_ip"])
    return web.json_response({"ok": True, "job": _job_view(st)})


async def upload(request):
    _test_bench(request)
    ctx = _bench(request)
    cfg, store = ctx["cfg"], ctx["store"]
    limit = int(cfg["max_upload_mb"]) * 2**20
    reader = await request.multipart()
    fields, sid, folder, size = {}, None, None, 0
    async for part in reader:
        if part.name == "file" and part.filename:
            ext = Path(part.filename).suffix.lower()
            if ext not in UPLOAD_EXT:
                return web.json_response({"error": f"Файли {ext or 'без розширення'} не підтримуються."}, status=400)
            if fields.get("consent") != "1":
                return web.json_response({"error": "Потрібна згода всіх, кого чути в записі."}, status=400)
            lang = fields.get("lang") if fields.get("lang") in LANGS else "auto"
            sid, folder, _ = store.create("upload", fields.get("title") or Path(part.filename).stem,
                                          source_name=Path(part.filename).name[:200], lang=lang,
                                          split=fields.get("split") == "1", consent=True, client=request["client_ip"])
            target = folder / f"source{ext}"
            with open(target, "wb") as f:
                while True:
                    chunk = await part.read_chunk(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        f.close()
                        store.delete(sid)
                        return web.json_response({"error": f"Файл більший за {cfg['max_upload_mb']} МБ."}, status=413)
                    f.write(chunk)
        else:
            fields[part.name] = (await part.text())[:500]
    if not sid:
        return web.json_response({"error": "Файл не отримано."}, status=400)
    duration = await asyncio.get_running_loop().run_in_executor(None, _probe_duration, store.audio_file(sid))
    store.update_meta(sid, duration=duration, size_mb=round(size / 2**20, 1))
    log.info("upload %s %.1f MB by %s", sid, size / 2**20, request["client_ip"])
    job = None
    if fields.get("transcribe", "1") == "1":
        params = {"model": fields.get("model") if fields.get("model") in ("accurate", "fast") else "accurate",
                  "device": "auto", "allow_unload": fields.get("allow_unload", "1") == "1",
                  "hotwords": fields.get("hotwords", "")[:300]}
        job = _job_view(ctx["jobs"].submit(sid, "transcribe", params))
    return web.json_response({"ok": True, "id": sid, "duration": duration, "job": job})


def _probe_duration(path):
    try:
        import av

        with av.open(str(path)) as c:
            if c.duration:
                return round(c.duration / 1_000_000, 1)
            stream = next((s for s in c.streams if s.type == "audio"), None)
            if stream is not None and stream.duration and stream.time_base:
                return round(float(stream.duration * stream.time_base), 1)
    except Exception:  # noqa: BLE001 - the worker reports unreadable files properly
        return None
    return None


# ---- measurements -------------------------------------------------------------------------------
def _metric_rows(store):
    rows = []
    for item in store.list():
        folder = store.path(item["id"])
        tr = (read_json(folder / "transcript.json") or {}).get("metrics") or {}
        mo = (read_json(folder / "moments.json") or {}).get("metrics") or {}
        live = item.get("live") or {}
        rows.append({
            "id": item["id"], "created": item.get("created"), "kind": item.get("kind"), "title": item.get("title"),
            "duration_s": item.get("duration"),
            "live_engine": live.get("engine"), "live_rtf": live.get("rtf"), "live_wait_ms": live.get("avg_wait_ms"),
            "live_cpu_avg": live.get("cpu_avg"), "live_cpu_max": live.get("cpu_max"),
            "whisper_model": tr.get("model"), "device": tr.get("device"), "compute_type": tr.get("compute_type"),
            "batch": tr.get("batch_size"), "load_s": tr.get("load_s"), "transcribe_s": tr.get("transcribe_s"),
            "speed_x": tr.get("speed_x"), "vram_peak_mb": tr.get("vram_peak_mb"),
            "ollama_unloaded": ", ".join(tr.get("ollama_unloaded") or []), "fallback": tr.get("fallback"),
            "llm": mo.get("model"), "llm_cost_usd": mo.get("cost_usd"), "llm_s": mo.get("seconds"), "llm_tok_s": mo.get("eval_tok_s"),
            "llm_gpu_share": mo.get("gpu_share"), "moments": mo.get("items"), "moments_rejected": mo.get("rejected"),
        })
    return rows


async def metrics(request):
    _test_bench(request)
    return web.json_response({"rows": _metric_rows(_bench(request)["store"])})


async def metrics_csv(request):
    _test_bench(request)
    rows = _metric_rows(_bench(request)["store"])
    out = io.StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})
    return web.Response(body=("﻿" + out.getvalue()).encode("utf-8"), content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition": f'attachment; filename="meeting-bench-{time.strftime("%Y%m%d")}.csv"'})


# ---- live ---------------------------------------------------------------------------------------
async def _engine(ctx, name):
    loop = asyncio.get_running_loop()
    cfg = ctx["cfg"]
    if name == "parakeet":
        if ctx.get("parakeet") is None:
            from .engines import ParakeetEngine

            ctx["parakeet"] = await loop.run_in_executor(None, ParakeetEngine, cfg)
        return ctx["parakeet"]
    if name == "whisper":
        need = int(cfg["vram_needed_mb"].get(cfg["whisper_live_model"], 2600))
        if ctx["jobs"].current and (ctx["jobs"].get(ctx["jobs"].current) or {}).get("kind") == "transcribe":
            raise JobError("Відеокарта зайнята розшифровкою — оберіть Parakeet або зачекайте.")
        mem = await loop.run_in_executor(None, gpu.memory)
        if not mem or mem["free"] < need:
            raise JobError(f"Для Whisper наживо треба ≈ {need} МБ відеопам'яті, вільно {(mem or {}).get('free', 0)} МБ. Оберіть Parakeet.")
        from .engines import WhisperEngine

        return await loop.run_in_executor(None, WhisperEngine, cfg)
    return None


def _consent(raw):
    """The two ticks of the card «Перед записом» (Minerva mode); None unless both are really ticked."""
    if not isinstance(raw, dict) or raw.get("announced") is not True or raw.get("all_agreed") is not True:
        return None
    lang = raw.get("lang") if raw.get("lang") in minerva.CONSENT_LANGS else "de"
    return {"announced": True, "all_agreed": True, "lang": lang, "at": minerva.now_iso()}


def _mark_kind(data, minerva_mode):
    """user (📌) | auto (microphone lost / back, pause, auto-stop) | consent_ok / consent_join (Minerva only)."""
    note = str(data.get("note") or "").strip()[:200]
    if minerva_mode and note in ("consent_ok", "consent_join"):
        return "", note
    return note, "auto" if data.get("auto") is True else "user"


def _lang(value):
    return value if value in LANGS else "auto"


async def live_ws(request):
    ctx = _bench(request)
    cfg, store, link = ctx["cfg"], ctx["store"], ctx["link"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 2**20)
    await ws.prepare(request)
    prev = ctx.get("live_finish")
    if prev is not None and not prev.done():
        # the previous recording is still being stopped in the background (its window went away): its end also
        # resets live_sid / live_engine, so a new recording must not start before it
        try:
            await asyncio.wait_for(asyncio.shield(prev), 30)
        except asyncio.TimeoutError:
            pass
    if ctx["live_lock"].locked() or (prev is not None and not prev.done()):
        await ws.send_json({"type": "error", "message": BUSY, "busy": True})
        await ws.close()
        return ws
    loop = asyncio.get_running_loop()
    out = asyncio.Queue()

    def emit(msg):
        loop.call_soon_threadsafe(out.put_nowait, msg)

    async def sender():
        while True:
            msg = await out.get()
            if msg is None:
                return
            if not ws.closed:
                try:
                    await ws.send_json(msg)
                except (ConnectionError, RuntimeError):
                    pass

    async def ticker(sess):
        while True:
            await asyncio.sleep(2)
            emit(sess.snapshot())

    async with ctx["live_lock"]:
        send_task = asyncio.create_task(sender())
        sess, tick, engine, sid = None, None, None, None
        minerva_mode, discard, deleted = False, False, False
        cap = None

        def stop_live(target, text):
            """bench/link.py: the meeting was deleted in Minerva -> stop now, nothing is transcribed or kept."""
            nonlocal deleted
            if target == sid and not deleted:
                deleted = True
                emit({"type": "error", "message": text, "deleted": True})
                asyncio.ensure_future(ws.close())

        async def _finish():
            """Stop the recording and hand it over. Runs as its own task: when the window goes away aiohttp
            cancels the handler (even again inside its `finally`), and a recording must never be left
            unstopped and unprocessed until a restart."""
            nonlocal engine
            # every step below must run even if one fails: otherwise live_engine stays "whisper" and every later
            # transcription waits for a live session that no longer exists (review 10.11)
            try:
                if tick:
                    tick.cancel()
                # a Minerva recording that got no audio at all (closed right after ▶) is not a recording
                empty = bool(minerva_mode and sess is not None and not (discard or deleted) and sess.samples == 0)
                if sess is not None:
                    try:
                        duration = await loop.run_in_executor(None, sess.stop)
                    except Exception as exc:  # noqa: BLE001 - the page must leave «Зберігаю…»
                        log.exception("live %s stop failed", sid)
                        emit({"type": "error", "message": f"Запис зупинено, але зберегти все не вдалося: {exc}"[:300]})
                    else:
                        log.info("live %s stop %.1f s", sid, duration)
                        if not (discard or deleted or empty):
                            emit({"type": "saved", "id": None if minerva_mode else sid, "duration": duration,
                                  "levels": sess.meta.get("levels"), "silent": sess.meta.get("silent") or [],
                                  "minerva": minerva_mode})
                ctx["live_sid"] = None          # before the link below: it never touches the live session
                if minerva_mode and sid and sess is None and sid in link.states:
                    # the handler was cancelled between the claim and the start (the window went away): nothing was
                    # recorded — the folder goes and Minerva hears `discarded`
                    link.stop_live = None
                    try:
                        await link.discard(sid, NOT_STARTED + "вікно запису закрили.")
                    except Exception:  # noqa: BLE001
                        log.exception("live %s: discarding an unstarted recording failed", sid)
                if minerva_mode and sid and sess is not None:
                    link.stop_live = None
                    try:
                        if discard:
                            await link.discard(sid)
                            emit({"type": "discarded"})
                        elif empty:
                            text = NOT_STARTED + "звуку не надійшло."
                            await link.discard(sid, text)
                            emit({"type": "discarded", "message": f"{text} Відкрийте вікно запису знову з Minerva."})
                        else:
                            await link.on_live_end(sid, None if deleted else store.meta(sid))
                    except Exception:  # noqa: BLE001
                        log.exception("live %s: handing over to Minerva failed", sid)
            finally:
                # the end marker goes the same way as the messages before it; put_nowait() here used to
                # overtake the scheduled "saved", so the page never left «Зберігаю…» (fixed 2026-11-10)
                emit(None)
                try:
                    await asyncio.wait_for(send_task, timeout=10)
                except asyncio.TimeoutError:
                    send_task.cancel()
                try:
                    if engine is not None and getattr(engine, "name", "") == "whisper":
                        engine.close()
                except Exception:  # noqa: BLE001
                    log.exception("live %s: closing the engine failed", sid)
                finally:
                    engine = None
                    gc.collect()
                    ctx["live_sid"] = None
                    ctx["live_engine"] = None

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    kind = data.get("type")
                    if kind == "start" and sess is None and sid is None:
                        minerva_mode = data.get("minerva") is not None
                        bid, binding, consent = None, None, None
                        if minerva_mode:
                            bid = str(data.get("minerva") or "")
                            consent = _consent(data.get("consent"))
                            if consent is None:
                                emit({"type": "error", "message": "Поставте обидві галочки згоди — тоді можна почати запис."})
                                break
                            try:
                                binding = link.reserve(bid)
                            except LinkError as exc:
                                emit({"type": "error", "message": exc.text, "stale": True})
                                break
                        elif not cfg.get("standalone_enabled"):
                            emit({"type": "error", "message": STANDALONE_OFF, "stale": True})
                            break
                        elif data.get("consent") is not True:
                            emit({"type": "error", "message": "Потрібна згода всіх, кого чути."})
                            break
                        try:
                            mode = data.get("mode") if data.get("mode") in ("both", "mic", "meeting") else "both"
                            name = data.get("engine") if data.get("engine") in ("parakeet", "whisper", "none") else "parakeet"
                            if minerva_mode and name == "whisper":
                                name = "parakeet"           # live Whisper would take the GPU the transcription needs
                            emit({"type": "status", "text": "Завантажую модель розпізнавання…" if name != "none" else "Готую запис…"})
                            try:
                                engine = await _engine(ctx, name)
                            except JobError as exc:
                                emit({"type": "error", "message": str(exc)})
                                break
                            ctx["live_engine"] = name
                            lang_l, lang_r = _lang(data.get("lang_l")), _lang(data.get("lang_r"))
                            capture_info = capture.clean(data.get("capture"))
                            extra = {}
                            if minerva_mode:
                                extra["minerva"] = {"meeting": binding["meeting"], "uid": binding["uid"],
                                                    "retention": binding["retention"]}
                            sid, folder, meta = store.create(
                                "live", (binding or {}).get("title") or data.get("title") or
                                ("Нарада" if minerva_mode else "Запис наживо"), mode=mode, engine=name,
                                lang_l=None if lang_l in ("", "auto") else lang_l, lang_r=None if lang_r in ("", "auto") else lang_r,
                                consent=True, client=request["client_ip"], capture=capture_info, **extra)
                            offline, progress = False, None
                            if minerva_mode:
                                processing = ((capture_info or {}).get("mic") or {}).get("processing")
                                try:
                                    offline, progress = await link.claim(
                                        bid, sid, consent=consent, mode=mode, processing=processing,
                                        lang_l="" if lang_l == "auto" else lang_l, lang_r="" if lang_r == "auto" else lang_r)
                                except LinkError as exc:
                                    await loop.run_in_executor(None, store.remove, sid)
                                    sid = None
                                    emit({"type": "error", "message": exc.text, "stale": exc.status == 409})
                                    break
                                if ws.closed:
                                    # the window was closed during the claim: nothing is recorded or transcribed;
                                    # Minerva hears `discarded` (it already got `recording`)
                                    await link.discard(sid, NOT_STARTED + "вікно запису закрили.")
                                    sid = None
                                    break
                                link.stop_live = stop_live
                                cap = (int(cfg.get("minerva_max_minutes") or 240) * 60 + 30) * SR
                        finally:
                            if bid:
                                link.release(bid)
                        try:
                            sess = await loop.run_in_executor(None, LiveSession, cfg, folder, meta, engine, emit)
                        except Exception as exc:  # noqa: BLE001 - e.g. audio.wav cannot be created
                            log.exception("live %s: the recording could not start", sid)
                            emit({"type": "error", "message": f"Запис не почався на RAG-ПК: {exc}"[:300]})
                            if minerva_mode:
                                link.stop_live = None
                                # Minerva got `recording`: it must not stay there
                                await link.discard(sid, NOT_STARTED + "помилка на RAG-ПК.")
                            sid = None
                            break
                        ctx["live_sid"] = sid
                        tick = asyncio.create_task(ticker(sess))
                        log.info("live %s start engine=%s mode=%s%s by %s", sid, name, mode,
                                 " (Minerva)" if minerva_mode else "", request["client_ip"])
                        emit({"type": "ready", "id": None if minerva_mode else sid, "engine": name,
                              "load_s": getattr(engine, "load_s", None), "minerva": minerva_mode, "progress": progress,
                              "offline": offline})
                        if offline:
                            emit({"type": "warn", "text": OFFLINE})
                    elif kind == "mark" and sess is not None:
                        note, mark_kind = _mark_kind(data, minerva_mode)
                        emit({"type": "marked", **sess.mark(note, mark_kind)})
                    elif kind == "discard" and sess is not None and minerva_mode:
                        # persisted BEFORE the recording is stopped: a restart in between must never transcribe it
                        link.mark_discard(sid)
                        discard = True
                        break
                    elif kind == "stop":
                        break
                elif msg.type == WSMsgType.BINARY and sess is not None:
                    sess.feed(msg.data)
                    if cap and sess.samples > cap:
                        # the page stops at minerva_max_minutes; this catches a page whose script no longer runs
                        sess.mark("Зупинено автоматично: досягнуто найбільшої тривалості запису", "auto")
                        emit({"type": "capped"})
                        break
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            finish = asyncio.ensure_future(_finish())
            ctx["live_finish"] = finish
            try:
                await asyncio.shield(finish)
            except asyncio.CancelledError:
                log.info("live %s: the window went away; the recording is finished in the background", sid)
                raise
    if not ws.closed:
        await ws.close()
    return ws


# ---- Minerva (stage 1) ----------------------------------------------------------------------------
async def minerva_redeem(request):
    """#minerva=<ticket> from the page -> Minerva (server to server) -> a binding for this window."""
    ctx = _bench(request)
    try:
        data = await request.json()
    except ValueError:
        data = None
    ticket = str(data.get("ticket") or "") if isinstance(data, dict) else ""
    try:
        _, view = await ctx["link"].redeem(ticket)
    except LinkError as exc:
        return web.json_response({"error": exc.text}, status=exc.status)
    view["live_busy"] = ctx["live_lock"].locked()
    return web.json_response(view)


async def minerva_binding(request):
    """The banner again after F5 (the binding id is kept in the tab's sessionStorage)."""
    ctx = _bench(request)
    view = ctx["link"].binding_view(request.match_info["bid"])
    if view is None:
        return web.json_response({"error": EXPIRED}, status=404)
    view["live_busy"] = ctx["live_lock"].locked()
    return web.json_response(view)


async def minerva_progress(request):
    """Only the state of the recording (no text of the meeting) for the random token from the WS `ready`."""
    view = _bench(request)["link"].progress(request.match_info["token"])
    if view is None:
        return web.json_response({"error": "Запис не знайдено — можливо, його вже видалено на RAG-ПК."}, status=404)
    return web.json_response(view)


# ---- app ----------------------------------------------------------------------------------------
async def _housekeeping(app):
    ctx = app[APP_KEY]
    while True:
        try:
            ctx["cfg"].refresh()
            removed = await asyncio.get_running_loop().run_in_executor(
                None, lambda: ctx["store"].purge(int(ctx["cfg"]["retention_days"]),
                                                 keep=lambda sid: sid == ctx.get("live_sid") or ctx["jobs"].busy(sid)))
            if removed:
                log.info("retention: removed %d old sessions", removed)
        except Exception:  # noqa: BLE001
            log.exception("retention failed")
        await asyncio.sleep(6 * 3600)


async def _start_background(app):
    ctx = app[APP_KEY]
    ctx["tasks"] = [asyncio.create_task(ctx["jobs"].run_forever()), asyncio.create_task(_housekeeping(app)),
                    asyncio.create_task(ctx["link"].run_forever())]


async def _stop_background(app):
    for t in app[APP_KEY].get("tasks", []):
        t.cancel()


def create_app(cfg, background=True):
    store = Store(cfg)
    ctx = {"cfg": cfg, "store": store, "live_lock": asyncio.Lock(), "parakeet": None, "live_sid": None, "live_engine": None}
    ctx["jobs"] = Jobs(cfg, store, live_busy=lambda: ctx.get("live_engine") == "whisper")
    ctx["link"] = Link(cfg, store, ctx["jobs"], live_sid=lambda: ctx.get("live_sid"))
    ctx["jobs"].on_finish = ctx["link"].on_job_finish
    app = web.Application(middlewares=[guard], client_max_size=4 * 2**20)
    app[APP_KEY] = ctx
    r = app.router
    r.add_get("/", index)
    r.add_static("/static/", STATIC, show_index=False)
    r.add_get("/api/status", status)
    r.add_get("/api/help", help_text)
    r.add_post("/api/minerva/redeem", minerva_redeem)
    r.add_get("/api/minerva/binding/{bid}", minerva_binding)
    r.add_get("/api/minerva/progress/{token}", minerva_progress)
    r.add_get("/api/sessions", sessions)
    r.add_get("/api/sessions/{sid}", session)
    r.add_get("/api/sessions/{sid}/audio", audio)
    r.add_get("/api/sessions/{sid}/export.txt", export_txt)
    r.add_post("/api/sessions/{sid}/rename", rename)
    r.add_post("/api/sessions/{sid}/delete", delete)
    r.add_post("/api/sessions/{sid}/transcribe", transcribe)
    r.add_post("/api/sessions/{sid}/moments", find_moments)
    r.add_post("/api/upload", upload)
    r.add_get("/api/metrics", metrics)
    r.add_get("/api/metrics.csv", metrics_csv)
    r.add_get("/ws", live_ws)
    if background:
        app.on_startup.append(_start_background)
        app.on_cleanup.append(_stop_background)
    return app
