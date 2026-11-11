"""Final transcript in a separate process: python -m bench.worker <session folder>.

Reads job.json, writes progress.json while working and transcript.json (or error.json) at the
end, then exits, so the video memory is free again for Ollama.
"""
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from . import cuda, gpu
from .engines import HALLUCINATIONS
from .store import read_json, write_json

SR = 16000
SILENT_PEAK = 0.003


class Progress:
    def __init__(self, folder):
        self.path = folder / "progress.json"
        self.last = 0.0
        self.state = {"stage": "start", "text": "Готуюсь…", "done_s": 0, "total_s": 0}

    def set(self, stage, text, force=True, **extra):
        self.state.update(stage=stage, text=text, **extra)
        now = time.time()
        if force or now - self.last > 1.5:
            self.last = now
            self.state["updated"] = round(now, 1)
            try:
                write_json(self.path, self.state)
            except OSError:  # progress is only for the page: a busy file must never stop the transcription
                pass


def friendly(exc):
    msg = str(exc)
    low = msg.lower()
    if "out of memory" in low:
        return "Не вистачило пам'яті відеокарти. Спробуйте «процесор» або звільніть відеокарту."
    if "cublas" in low or ("cuda" in low and "dll" in low):
        return "Не знайдено бібліотеку CUDA (cuBLAS 12) — перевірте встановлення пакета nvidia-cublas-cu12."
    if "local_files_only" in low or "cannot find the requested files" in low:
        return "Модель Whisper ще не завантажена на RAG-ПК (setup.ps1 завантажує її в D:\\Meeting\\models)."
    if "invalid data" in low or "could not find" in low or "error opening" in low:
        return "Не вдалося прочитати аудіо з цього файлу."
    return msg[:300] or exc.__class__.__name__


def decode(path, stereo):
    """Any audio/video file -> float32 16 kHz (mono, or (left, right)).

    Same steps as faster_whisper.decode_audio, without its av.open(metadata_errors=...) that
    PyAV 19 no longer accepts.
    """
    import av

    raw = bytearray()
    with av.open(str(path), mode="r") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("invalid data: no audio stream")
        resampler = av.AudioResampler(format="s16", layout="stereo" if stereo else "mono", rate=SR)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                raw += out.to_ndarray().tobytes()
        for out in resampler.resample(None):
            raw += out.to_ndarray().tobytes()
    audio = np.frombuffer(bytes(raw), dtype=np.int16).astype(np.float32) / 32768.0
    if stereo:
        return audio[0::2], audio[1::2]
    return audio


def load_tracks(job):
    if job["split"]:
        left, right = decode(job["audio"], stereo=True)
        tracks = {"L": left, "R": right}
    else:
        tracks = {"M": decode(job["audio"], stereo=False)}
    return {ch: audio for ch, audio in tracks.items() if ch in job["channels"]}


def transcribe_track(model, pipe, audio, language, job, on_segment):
    # without_timestamps=False: the batched pipeline defaults to True, which gives ~30-60 s lines
    kwargs = dict(language=language or None, beam_size=int(job["beam_size"]), vad_filter=True,
                  condition_on_previous_text=False, multilingual=not language, without_timestamps=False,
                  hotwords=job.get("hotwords") or None)
    if pipe is not None:
        segments, info = pipe.transcribe(audio, batch_size=int(job["batch_size"]), **kwargs)
    else:
        segments, info = model.transcribe(audio, **kwargs)
    out = []
    for seg in segments:
        text = seg.text.strip()
        on_segment(seg.end)
        if not text or text.lower() in HALLUCINATIONS:
            continue
        out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text,
                    "avg_logprob": round(seg.avg_logprob, 3), "no_speech_prob": round(seg.no_speech_prob, 3)})
    return out, {"language": info.language, "probability": round(info.language_probability or 0, 3)}


def run(folder):
    job = read_json(folder / "job.json")
    progress = Progress(folder)
    started = time.time()
    progress.set("decode", "Читаю аудіо…")
    tracks = load_tracks(job)
    audio_s = max((len(a) for a in tracks.values()), default=0) / SR
    active = {ch: a for ch, a in tracks.items() if a.size and float(np.abs(a).max()) > SILENT_PEAK}
    skipped = sorted(set(tracks) - set(active))
    decode_s = time.time() - started
    total = sum(len(a) for a in active.values()) / SR

    cuda.setup()
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    sampler = gpu.Sampler() if job["device"] == "cuda" else None
    if sampler:
        sampler.start()
    progress.set("load", f"Завантажую модель {job['model']}…", total_s=round(total, 1))
    t0 = time.time()
    model = WhisperModel(job["model"], device=job["device"], compute_type=job["compute_type"],
                         cpu_threads=int(job["cpu_threads"]), download_root=job["whisper_dir"], local_files_only=True)
    load_s = time.time() - t0
    batch = int(job["batch_size"]) if job["device"] == "cuda" else 1
    pipe = BatchedInferencePipeline(model) if batch > 1 else None

    segments, languages, done = [], {}, 0.0
    t1 = time.time()
    for ch, audio in active.items():
        offset = done

        def on_segment(end, offset=offset):
            progress.set("transcribe", "Розшифровую…", force=False, done_s=round(offset + end, 1), total_s=round(total, 1))

        try:
            found, lang = transcribe_track(model, pipe, audio, job["channels"].get(ch), {**job, "batch_size": batch}, on_segment)
        except RuntimeError as exc:
            if pipe is None or "out of memory" not in str(exc).lower():
                raise
            pipe, batch = None, 1  # retry this channel without batching
            found, lang = transcribe_track(model, None, audio, job["channels"].get(ch), {**job, "batch_size": 1}, on_segment)
        for seg in found:
            seg["ch"] = ch
        segments.extend(found)
        languages[ch] = lang
        done += len(audio) / SR
    transcribe_s = time.time() - t1
    if sampler:
        sampler.stop()
    compute = getattr(model.model, "compute_type", job["compute_type"])
    segments.sort(key=lambda s: (s["start"], s["ch"]))
    metrics = {
        "model": job["model"], "device": job["device"], "compute_type": compute,
        "beam_size": int(job["beam_size"]), "batch_size": batch,
        "audio_s": round(audio_s, 1), "channels_s": round(total, 1), "skipped_silent": skipped,
        "decode_s": round(decode_s, 1), "load_s": round(load_s, 1), "transcribe_s": round(transcribe_s, 1),
        "total_s": round(time.time() - started, 1),
        "speed_x": round(total / transcribe_s, 2) if transcribe_s > 0 else None,
        "vram_before_mb": sampler.start_used if sampler else None,
        "vram_peak_mb": sampler.peak if sampler else None,
        "ollama_unloaded": job.get("unloaded") or [],
        "fallback": job.get("fallback"),
    }
    return {"model": job["model"], "languages": languages, "segments": segments, "metrics": metrics,
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}


def main():
    folder = Path(sys.argv[1])
    (folder / "error.json").unlink(missing_ok=True)
    try:
        result = run(folder)
        write_json(folder / "transcript.json", result)
    except Exception as exc:  # noqa: BLE001 - the page shows a readable error
        traceback.print_exc()
        try:
            write_json(folder / "error.json", {"message": friendly(exc), "detail": repr(exc)[:500],
                                              "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        except OSError:
            pass  # the exit code alone still tells the server that it failed
        sys.exit(1)
    try:
        write_json(folder / "progress.json", {"stage": "done", "text": "Готово", "updated": round(time.time(), 1)})
    except OSError:
        pass  # transcript.json is written; the exit code 0 is what counts


if __name__ == "__main__":
    main()
