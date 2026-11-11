"""One job at a time: final transcript (worker process on the GPU or CPU) or key moments (Ollama).

State lives in memory for the page (`status`) and on disk in the session folder. `on_finish(sid, kind, state, text)`
tells bench/link.py when a job of a Minerva session ends, so it can push the result and start the next step.
Minerva sessions get only the local model: no cloud provider and no Ollama «cloud» tag, whatever the request says.
"""
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

from . import cloud, gpu, moments
from .store import read_json, write_json

log = logging.getLogger("bench.jobs")

APP_DIR = Path(__file__).resolve().parent.parent
BELOW_NORMAL = 0x00004000
NO_WINDOW = 0x08000000
KINDS = ("transcribe", "moments")


class JobError(Exception):
    pass


def local_model(cfg, local):
    """The LLM for a Minerva meeting: the configured local default if Ollama has it, else the first installed of
    llm_models; '' when none (never a cloud model, never cloud.default_choice)."""
    wanted = cfg.get("llm_model") if cfg.get("llm_provider") == "ollama" and cfg.get("llm_model") else cfg["llm_default"]
    for name in [wanted, *cfg.get("llm_models", [])]:
        if name and name in local and not gpu.is_cloud_tag(name):
            return name
    return ""


class Jobs:
    def __init__(self, cfg, store, live_busy, on_finish=None):
        self.cfg = cfg
        self.store = store
        self.live_busy = live_busy  # callable: is a live session holding the GPU?
        self.on_finish = on_finish  # callable(sid, kind, state, text) on the event loop, after a job ends
        self.queue = asyncio.Queue()
        self.status = {}
        self.current = None
        self.procs = {}             # sid -> running worker process (cancel kills it)

    def get(self, sid):
        return self.status.get(sid)

    def busy(self, sid):
        st = self.status.get(sid)
        return bool(st and st["state"] in ("queued", "running"))

    def submit(self, sid, kind, params):
        if kind not in KINDS:
            raise JobError("Невідоме завдання.")
        if self.busy(sid):
            raise JobError("Для цього запису вже є завдання в черзі.")
        self.status[sid] = {"kind": kind, "state": "queued", "text": "У черзі…", "queued": time.time(), "params": params}
        self.queue.put_nowait((sid, kind, params))
        return self.status[sid]

    def position(self, sid):
        """1-based place in the queue (1 = next), 0 when running or not queued."""
        st = self.status.get(sid)
        if not st or st["state"] != "queued":
            return 0
        waiting = sorted((s["queued"], k) for k, s in self.status.items() if s["state"] == "queued")
        return 1 + [k for _, k in waiting].index(sid)

    def cancel(self, sid):
        """The session is being deleted: a queued job is skipped, a running worker is stopped."""
        st = self.status.get(sid)
        if not st:
            return
        if st["state"] == "queued":
            st.update(state="cancelled", text="Скасовано", finished=time.time())
        proc = self.procs.get(sid)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            st["cancelled"] = True

    async def run_forever(self):
        while True:
            sid, kind, params = await self.queue.get()
            st = self.status.get(sid)
            if st is None or st["state"] != "queued":   # cancelled meanwhile
                continue
            st.update(state="running", text="Починаю…", started=time.time())
            self.current = sid
            try:
                if kind == "transcribe":
                    await self._transcribe(sid, params, st)
                elif kind == "moments":
                    await self._moments(sid, params, st)
                else:
                    raise JobError("Невідоме завдання.")
                st.update(state="done", text="Готово", finished=time.time())
            except (JobError, KeyError) as exc:
                st.update(state="error", text=str(exc) or "Запис не знайдено", finished=time.time())
            except Exception as exc:  # noqa: BLE001
                log.exception("job failed")
                st.update(state="error", text=f"Помилка: {exc}"[:300], finished=time.time())
            finally:
                self.current = None
                self.procs.pop(sid, None)
            if self.on_finish is not None:
                try:
                    self.on_finish(sid, kind, st["state"], st.get("text") or "")
                except Exception:  # noqa: BLE001 - the queue must keep running
                    log.exception("on_finish failed for %s", sid)

    # ---- final transcript -------------------------------------------------------------------
    async def _transcribe(self, sid, params, st):
        cfg = self.cfg
        folder = self.store.path(sid)
        meta = self.store.meta(sid)
        audio = self.store.audio_file(sid)
        if audio is None:
            raise JobError("У записі немає аудіо.")
        model = cfg["whisper_models"].get(params.get("model", "accurate"), cfg["whisper_models"]["accurate"])
        need = int(cfg["vram_needed_mb"].get(model, 4200))
        wanted = params.get("device", "auto")
        device, unloaded, fallback = "cpu", [], None
        loop = asyncio.get_running_loop()
        if wanted in ("auto", "gpu"):
            while self.live_busy():
                st["text"] = "Чекаю: відеокарту зараз тримає запис наживо (Whisper)…"
                await asyncio.sleep(2)
            # nvidia-smi can take seconds: never on the event loop (a stall drops a running live recording)
            mem = await loop.run_in_executor(None, gpu.memory)
            if mem and mem["free"] < need and params.get("allow_unload", True):
                st["text"] = "Звільняю відеокарту: вивантажую моделі Ollama…"
                unloaded = await loop.run_in_executor(None, gpu.ollama_unload, cfg["ollama_url"])
                mem = await loop.run_in_executor(None, gpu.memory)
            if mem and mem["free"] >= need:
                device = "cuda"
            elif mem is None:
                if wanted == "gpu":
                    raise JobError("Відеокарти NVIDIA не видно (nvidia-smi не відповідає).")
                fallback = "відеокарти NVIDIA не видно — рахую на процесорі"
            elif wanted == "gpu":
                raise JobError(f"Не вистачає пам'яті відеокарти: вільно {mem['free']} МБ, потрібно ≈ {need} МБ.")
            else:
                fallback = f"на відеокарті вільно {mem['free']} МБ із потрібних ≈ {need} — рахую на процесорі"
        split = meta.get("kind") == "live" or bool(meta.get("split"))
        if meta.get("kind") == "live":
            mode = meta.get("mode", "both")
            names = {"both": ("L", "R"), "mic": ("L",), "meeting": ("R",)}.get(mode, ("L", "R"))
            langs = {"L": params.get("lang_l", meta.get("lang_l")), "R": params.get("lang_r", meta.get("lang_r"))}
            channels = {ch: (langs[ch] if langs[ch] not in ("", "auto") else None) for ch in names}
        elif split:
            lang = params.get("lang", meta.get("lang"))
            lang = None if lang in (None, "", "auto") else lang
            channels = {"L": lang, "R": lang}
        else:
            lang = params.get("lang", meta.get("lang"))
            channels = {"M": None if lang in (None, "", "auto") else lang}
        hotwords = params.get("hotwords") or ""
        if meta.get("minerva"):
            # a Minerva meeting's hotwords (customer, project, SKUs) go to the worker only through job.json: they are
            # never kept in the job status that /api/status shows
            hotwords = (read_json(folder / "minerva.json") or {}).get("hotwords") or ""
        job = {
            "audio": str(audio), "split": split, "channels": channels, "model": model, "device": device,
            "compute_type": cfg["whisper_compute_type"], "beam_size": int(cfg["whisper_beam_size"]),
            "batch_size": int(cfg["whisper_batch_size"]), "cpu_threads": int(cfg["cpu_threads"]),
            "hotwords": str(hotwords).strip()[:300], "whisper_dir": str(cfg.whisper_dir),
            "unloaded": unloaded, "fallback": fallback,
        }
        write_json(folder / "job.json", job)
        for name in ("progress.json", "error.json"):
            (folder / name).unlink(missing_ok=True)
        st["text"] = f"Запускаю {model} ({'відеокарта' if device == 'cuda' else 'процесор'})…"
        logf = open(folder / "worker.log", "w", encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "bench.worker", str(folder), cwd=str(APP_DIR),
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=(BELOW_NORMAL | NO_WINDOW) if sys.platform == "win32" else 0,
            )
            self.procs[sid] = proc
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                p = read_json(folder / "progress.json")
                if p:
                    st["text"] = p.get("text", st["text"])
                    if p.get("total_s"):
                        st["progress"] = min(1.0, (p.get("done_s") or 0) / p["total_s"])
        finally:
            logf.close()
        if st.get("cancelled"):
            raise JobError("Скасовано: запис видаляється.")
        err = read_json(folder / "error.json")
        if proc.returncode != 0 or err:
            raise JobError((err or {}).get("message") or f"Розшифровка зупинилась (код {proc.returncode}).")

    # ---- key moments ------------------------------------------------------------------------
    async def _moments(self, sid, params, st):
        folder = self.store.path(sid)
        transcript = read_json(folder / "transcript.json")
        if not transcript or not transcript.get("segments"):
            raise JobError("Спочатку потрібна розшифровка.")
        cfg = self.cfg
        loop = asyncio.get_running_loop()
        provider = params.get("provider") or "ollama"
        model = params.get("model") or cfg["llm_default"]
        meta = self.store.meta(sid)
        title, lang = meta.get("title", ""), params.get("lang", "uk")
        if meta.get("minerva") or params.get("minerva"):
            # the participants were told: nothing goes to the cloud. Checked here, right before the text is used.
            if provider != "ollama":
                raise JobError("Для наради Minerva — лише локальна модель на RAG-ПК.")
            local = await loop.run_in_executor(None, gpu.ollama_local, cfg["ollama_url"])
            model = params.get("model") or local_model(cfg, local)
            if not model or model not in local or gpu.is_cloud_tag(model):
                raise JobError("Локальної моделі для ключових моментів немає (Ollama не відповідає або модель не "
                               "встановлена).")
        if provider == "ollama":
            installed = await loop.run_in_executor(None, gpu.ollama_installed, cfg["ollama_url"])
            if model not in installed:
                raise JobError(f"Модель {model} не встановлена в Ollama.")
            st["text"] = f"{model}: шукаю рішення, завдання, строки…"
            url, num_ctx = cfg["ollama_url"], int(cfg["llm_num_ctx"])
            result = await loop.run_in_executor(
                None, lambda: moments.extract(url, transcript, model, title=title, out_lang=lang, num_ctx=num_ctx))
        else:
            # the page asked for consent; check it again here, right before the text leaves the RAG PC
            if (meta.get("cloud_consent") or {}).get("provider") != provider:
                raise JobError("Немає згоди на обробку тексту цього запису в хмарі.")
            st["text"] = f"{cloud.label(model)}: надсилаю текст розшифровки в Anthropic і шукаю моменти…"
            chunk = int(cfg["llm_cloud_chunk_chars"])
            effort = cloud.effort_of(model, cfg.get("llm_cloud_effort"))
            secrets_file = cfg.get("secrets_file")
            try:
                result = await loop.run_in_executor(
                    None, lambda: cloud.extract(provider, model, transcript, title=title, out_lang=lang, chunk_chars=chunk,
                                                effort=effort, secrets_file=secrets_file))
            except cloud.CloudError as exc:
                self._remember_run(folder, {"provider": provider, "model": model, "error": str(exc), **exc.usage})
                raise JobError(str(exc)) from exc
        result["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._remember_run(folder, {"created": result["created"], **result["metrics"]})
        write_json(folder / "moments.json", result)

    @staticmethod
    def _remember_run(folder, row):
        row.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
        history = read_json(folder / "moments_runs.json", []) or []
        history.append(row)
        write_json(folder / "moments_runs.json", history[-20:])
