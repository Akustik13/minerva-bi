"""🎤 Голосовий набір Minerva через RAG-ПК: коротке аудіо з будь-якого поля Minerva -> текст (static/admin/js/mv_voice.js).

POST /api/dictate?lang=auto|uk|de|en|ru|pl&clean=0|1, тіло — аудіо (webm / ogg / mp4 / wav від MediaRecorder,
≤ MAX_BYTES); GET /api/dictate/ping — чи доступно. Лише дозволені ПК (allowed_clients) і лише зі сторінок Minerva
(Origin = cfg minerva_url, CORS у server.guard) або самого вікна; заголовок X-Bench: 1. Нічого не зберігається:
тимчасовий файл видаляється одразу, у журналі — лише тривалість, мова й IP, без тексту.

Модель — whisper_live_model (large-v3-turbo) у процесі вікна: на відеокарті, коли вільної пам'яті ≥ dictate_vram_mb і
не йде запис наживо з Whisper, інакше на процесорі; тримається dictate_idle_seconds після останнього диктування, потім
вивантажується (відеопам'ять — Ollama й розшифровці нарад; перед розшифровкою наради — одразу, release()).
clean=1 — спершу DeepFilterNet (bench/denoise.py, −24 дБ), як на вкладці «🧹 Чистка звуку».
"""
import gc
import logging
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import denoise, gpu

log = logging.getLogger("bench.dictate")

MAX_BYTES = 12 * 2**20            # ≈ 5 хв opus; більше — не диктування
MAX_SECONDS = 300
LANGS = ("uk", "de", "en", "ru", "pl", "fr", "it", "es")
SUFFIX = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
          "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav"}
NO_WINDOW = 0x08000000
CLEAN_LEVEL = "medium"


class DictationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def suffix_for(content_type) -> str:
    return SUFFIX.get((content_type or "").split(";")[0].strip().lower(), ".webm")


def lang_of(value):
    """'auto' / '' / невідома -> None (Whisper визначає сам)."""
    value = (value or "").strip().lower()[:5].split("-")[0]
    return value if value in LANGS else None


class Dictation:
    def __init__(self, cfg, live_gpu=lambda: False):
        self.cfg = cfg
        self.live_gpu = live_gpu          # іде запис наживо з Whisper на відеокарті — диктування на процесорі
        self.model = None
        self.device = ""
        self.load_s = 0.0
        self.last = 0.0
        self.lock = threading.Lock()      # одне диктування за раз (наступне чекає)
        self.busy = False

    # ---- стан і модель --------------------------------------------------------------------------
    def info(self):
        return {"model": self.cfg["whisper_live_model"], "loaded": self.model is not None, "device": self.device,
                "busy": self.busy, "denoise": denoise.exe_path(self.cfg) is not None,
                "max_seconds": MAX_SECONDS}

    def _device(self):
        if self.live_gpu():
            return "cpu"
        mem = gpu.memory()
        return "cuda" if mem and mem["free"] >= int(self.cfg["dictate_vram_mb"]) else "cpu"

    def _load(self):
        if self.model is not None:
            return
        from . import cuda
        cuda.setup()
        from faster_whisper import WhisperModel

        device = self._device()
        t0 = time.time()
        try:
            self.model = WhisperModel(self.cfg["whisper_live_model"], device=device,
                                      compute_type=self.cfg["whisper_compute_type"],
                                      cpu_threads=int(self.cfg["cpu_threads"]),
                                      download_root=str(self.cfg.whisper_dir), local_files_only=True)
        except Exception as exc:  # noqa: BLE001 - CUDA / пам'ять: ще раз на процесорі
            if device != "cuda":
                raise
            log.warning("dictation model on the GPU failed (%s): CPU", exc)
            device = "cpu"
            self.model = WhisperModel(self.cfg["whisper_live_model"], device="cpu",
                                      compute_type=self.cfg["whisper_compute_type"],
                                      cpu_threads=int(self.cfg["cpu_threads"]),
                                      download_root=str(self.cfg.whisper_dir), local_files_only=True)
        self.device = device
        self.load_s = round(time.time() - t0, 1)
        log.info("dictation model %s on %s in %.1f s", self.cfg["whisper_live_model"], device, self.load_s)

    def release(self, idle_only=True):
        """Вивантажити модель (відеопам'ять — іншим): якщо зараз не диктують; idle_only — лише після паузи."""
        if self.model is None or self.busy:
            return False
        if idle_only and time.time() - self.last < int(self.cfg["dictate_idle_seconds"]):
            return False
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self.model, self.device = None, ""
            gc.collect()
        finally:
            self.lock.release()
        log.info("dictation model unloaded")
        return True

    # ---- аудіо -> текст -------------------------------------------------------------------------
    def _clean(self, src, folder):
        """DeepFilterNet на копії (48 кГц моно) -> 16 кГц для Whisper."""
        from .worker import decode
        exe = denoise.exe_path(self.cfg)
        if exe is None:
            return None
        wav = folder / "dn.wav"
        denoise.write_wav(wav, denoise.decode48(src, stereo=False))
        out = folder / "out"
        proc = subprocess.run([str(exe), "-D", "-a", str(denoise.LEVELS[CLEAN_LEVEL][0]), "-o", str(out), str(wav)],
                              capture_output=True, timeout=120,
                              creationflags=NO_WINDOW if sys.platform == "win32" else 0)
        if proc.returncode != 0 or not (out / "dn.wav").is_file():
            log.warning("dictation denoise failed: code %s", proc.returncode)
            return None
        return decode(out / "dn.wav", stereo=False)

    def run(self, data, content_type="audio/webm", lang=None, clean=False):
        """-> {text, language, model, device, seconds, audio_s, cleaned, load_s}. Блокує: викликати в executor."""
        if not data:
            raise DictationError("Порожній запис.")
        if len(data) > MAX_BYTES:
            raise DictationError("Задовгий запис для голосового набору.", 413)
        from .engines import HALLUCINATIONS
        from .worker import SR, decode

        with self.lock:
            self.busy = True
            started = time.time()
            try:
                with tempfile.TemporaryDirectory(prefix="dictate-", dir=self._tmp()) as tmp:
                    folder = Path(tmp)
                    src = folder / f"{uuid.uuid4().hex}{suffix_for(content_type)}"
                    src.write_bytes(data)
                    try:
                        audio = decode(src, stereo=False)
                    except Exception as exc:  # noqa: BLE001
                        raise DictationError(f"Не вдалося прочитати звук: {exc}"[:200]) from exc
                    audio_s = len(audio) / SR
                    if audio_s > MAX_SECONDS + 5:
                        raise DictationError("Задовгий запис для голосового набору.", 413)
                    cleaned = False
                    if clean:
                        better = self._clean(src, folder)
                        if better is not None:
                            audio, cleaned = better, True
                    loaded = self.model is None
                    self._load()
                    segments, info = self.model.transcribe(
                        audio, language=lang_of(lang), beam_size=5, vad_filter=True,
                        condition_on_previous_text=False, without_timestamps=True)
                    parts = []
                    for seg in segments:
                        text = seg.text.strip()
                        if not text or text.lower() in HALLUCINATIONS:
                            continue
                        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                            continue
                        parts.append(text)
            finally:
                self.busy = False
                self.last = time.time()
        return {"text": " ".join(parts).strip(), "language": info.language, "model": self.cfg["whisper_live_model"],
                "device": self.device, "seconds": round(time.time() - started, 1), "audio_s": round(audio_s, 1),
                "cleaned": cleaned, "load_s": self.load_s if loaded else 0.0}

    def _tmp(self):
        folder = Path(self.cfg.data_dir, "tmp")
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)
