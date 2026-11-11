"""One live capture: stereo PCM from the browser -> audio.wav + VAD per channel -> captions.

Channel L = the person's microphone ("ви"), R = meeting audio ("співрозмовники").
The asyncio side calls feed() for every binary frame; recognition runs in one worker thread
(finals before partials) and results go back through `emit`, which must be thread-safe.
"""
import itertools
import json
import logging
import queue
import threading
import time
import wave
from datetime import datetime

import numpy as np

from .store import read_json, write_json

log = logging.getLogger("bench.live")

SR = 16000
FINAL, PARTIAL, STOP = 0, 1, 2
RING_SECONDS = 30
PREROLL = 0.6  # the VAD reports speech a little after it starts
LOUD = 328     # |sample| above -40 dBFS counts as "sound"
SILENT_PEAK_DBFS = -50.0  # a channel whose loudest moment stays below this was effectively silent
SILENT_SECONDS = 0.3      # ... and so was one with less than 0.3 s of sound in the whole recording (a click)
SILENT_SHARE = 0.005      # meta.json written before the review of 10.11 has no sound_s: then less than 0.5 % of the time


def dbfs(value):
    """Int16 amplitude -> dBFS (None for digital silence)."""
    return round(20 * np.log10(value / 32768.0), 1) if value > 0 else None


SKIP_SAMPLES = SR // 2  # first 0.5 s: a device's power-on thump, the start-up gap of system echo cancellation


class Levels:
    """Running loudness of both channels of the recording (cheap enough for every frame).

    The first 0.5 s is left out: on 10.11.2026 a dead USB microphone gave one -15 dBFS thump at 0.17 s and pure
    noise afterwards, which must not count as sound.
    """

    def __init__(self):
        self.sumsq = np.zeros(2, np.float64)
        self.peak = np.zeros(2, np.int32)
        self.loud = np.zeros(2, np.int64)
        self.n = 0
        self.skip = SKIP_SAMPLES

    def add(self, stereo_i16):
        if self.skip:
            cut = min(self.skip, stereo_i16.shape[0])
            stereo_i16 = stereo_i16[cut:]
            self.skip -= cut
        if not stereo_i16.size:
            return
        x = stereo_i16.astype(np.int32)
        self.sumsq += (x.astype(np.float64) ** 2).sum(axis=0)
        np.maximum(self.peak, np.abs(x).max(axis=0), out=self.peak)
        self.loud += (np.abs(x) > LOUD).sum(axis=0)
        self.n += x.shape[0]

    def report(self, names):
        """{'L': {'rms_dbfs', 'peak_dbfs', 'sound_share', 'sound_s'}, ...} for the channels that were recorded."""
        out = {}
        for name in names:
            i = 0 if name == "L" else 1
            if not self.n:
                out[name] = {"rms_dbfs": None, "peak_dbfs": None, "sound_share": 0.0, "sound_s": 0.0}
                continue
            out[name] = {"rms_dbfs": dbfs(float(np.sqrt(self.sumsq[i] / self.n))), "peak_dbfs": dbfs(int(self.peak[i])),
                         "sound_share": round(float(self.loud[i]) / self.n, 3), "sound_s": round(float(self.loud[i]) / SR, 1)}
        return out


def silent_channels(levels):
    """Names of channels that carried no usable sound: peak below SILENT_PEAK_DBFS or under SILENT_SECONDS of sound.

    Measured 10.11.2026: the user's silent microphone gave peaks of -70..-40 dBFS with 0 % of the time above -40 dBFS,
    one take had a single click to -15 dBFS (0.4 % of a short take). Seconds, not a share: in an hour-long meeting
    someone who says only a greeting and one question (10 s) is above -40 dBFS for a few seconds - that is sound.
    """
    out = []
    for name, lv in levels.items():
        peak = lv.get("peak_dbfs")
        if peak is None or peak < SILENT_PEAK_DBFS:
            out.append(name)
        elif "sound_s" in lv:
            if (lv.get("sound_s") or 0) < SILENT_SECONDS:
                out.append(name)
        elif (lv.get("sound_share") or 0) < SILENT_SHARE:
            out.append(name)
    return out


class Ring:
    """Last N samples with absolute indexing (index = sample number since start)."""

    def __init__(self, size):
        self.buf = np.zeros(size, np.float32)
        self.size = size
        self.end = 0

    def write(self, x):
        n = len(x)
        if n >= self.size:
            self.buf[:] = x[-self.size:]
        else:
            pos = self.end % self.size
            first = min(n, self.size - pos)
            self.buf[pos:pos + first] = x[:first]
            if first < n:
                self.buf[:n - first] = x[first:]
        self.end += n

    def read(self, start):
        start = max(start, self.end - self.size, 0)
        n = self.end - start
        if n <= 0:
            return np.zeros(0, np.float32)
        pos = start % self.size
        if pos + n <= self.size:
            return self.buf[pos:pos + n].copy()
        return np.concatenate([self.buf[pos:], self.buf[:n - (self.size - pos)]])


def make_vad(cfg):
    import sherpa_onnx

    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(cfg.vad_model)
    config.silero_vad.threshold = float(cfg["vad_threshold"])
    config.silero_vad.min_silence_duration = float(cfg["vad_min_silence"])
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.max_speech_duration = float(cfg["vad_max_speech"])
    config.sample_rate = SR
    config.num_threads = 1
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=120)
    return vad, config.silero_vad.window_size


class Channel:
    def __init__(self, name, index, language, vad, window):
        self.name = name
        self.index = index
        self.language = language
        self.vad = vad
        self.window = window
        self.pending = np.zeros(0, np.float32)
        self.ring = Ring(RING_SECONDS * SR)
        self.in_speech = False
        self.speech_since = 0
        self.last_partial_at = 0
        self.gen = 0
        self.partial_busy = False
        self.peak = 0.0


class LiveSession:
    def __init__(self, cfg, folder, meta, engine, emit):
        self.cfg = cfg
        self.folder = folder
        self.meta = meta
        self.engine = engine
        self.emit = emit
        self.samples = 0
        self.marks = []
        self.closed = False
        self.q = queue.PriorityQueue()
        self.seq = itertools.count()
        self.stats = {"finals": 0, "partials": 0, "decode_s": 0.0, "audio_s": 0.0, "wait_s": 0.0, "errors": 0}
        self.cpu0 = (time.process_time(), time.time())
        self.cpu_samples = []
        self.levels = Levels()
        self.wav = wave.open(str(folder / "audio.wav"), "wb")
        self.wav.setnchannels(2)
        self.wav.setsampwidth(2)
        self.wav.setframerate(SR)
        self.live_file = open(folder / "live.jsonl", "a", encoding="utf-8", newline="\n")
        mode = meta.get("mode", "both")
        wanted = {"both": ("L", "R"), "mic": ("L",), "meeting": ("R",)}.get(mode, ("L", "R"))
        self.channels = []
        if engine is not None:
            for name in wanted:
                vad, window = make_vad(cfg)
                lang = meta.get("lang_l" if name == "L" else "lang_r") or None
                self.channels.append(Channel(name, 0 if name == "L" else 1, lang, vad, window))
            self.thread = threading.Thread(target=self._work, name="live-recognizer", daemon=True)
            self.thread.start()
        else:
            self.thread = None

    # ---- audio in (event loop thread) ------------------------------------------------------
    def feed(self, data):
        if self.closed or not data:
            return
        frames = np.frombuffer(data, dtype="<i2")
        if frames.size % 2:
            frames = frames[:-1]
        self.wav.writeframes(frames.tobytes())
        start = self.samples
        self.samples += frames.size // 2
        pairs = frames.reshape(-1, 2)
        self.levels.add(pairs)
        if not self.channels:
            return
        stereo = pairs.astype(np.float32) / 32768.0
        for ch in self.channels:
            self._feed_channel(ch, stereo[:, ch.index], start)

    def _feed_channel(self, ch, x, start):
        ch.ring.write(x)
        ch.peak = max(ch.peak, float(np.abs(x).max()) if x.size else 0.0)
        buf = np.concatenate([ch.pending, x]) if ch.pending.size else x
        k = (len(buf) // ch.window) * ch.window
        for i in range(0, k, ch.window):
            ch.vad.accept_waveform(buf[i:i + ch.window])
        ch.pending = buf[k:].copy()
        self._collect_finals(ch)
        speaking = ch.vad.is_speech_detected()
        now = ch.ring.end
        if speaking and not ch.in_speech:
            ch.in_speech = True
            ch.speech_since = max(ch.speech_since, now - int(PREROLL * SR), 0)
            ch.last_partial_at = now
        elif not speaking:
            ch.in_speech = False
        every = float(self.cfg["partial_every"]) * SR
        if ch.in_speech and not ch.partial_busy and now - ch.last_partial_at >= every:
            audio = ch.ring.read(ch.speech_since)
            if len(audio) >= SR // 2:
                ch.last_partial_at = now
                ch.partial_busy = True
                self._put(PARTIAL, ch, audio, ch.speech_since / SR)

    def _collect_finals(self, ch):
        while not ch.vad.empty():
            seg = ch.vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            start = int(seg.start)
            ch.vad.pop()
            ch.gen += 1
            ch.speech_since = start + len(samples)
            self._put(FINAL, ch, samples, start / SR)

    def _put(self, kind, ch, samples, t0):
        self.q.put((kind, next(self.seq), (kind, ch, samples, t0, ch.gen, time.time())))

    def mark(self, note, kind=None):
        """kind: user (📌), auto (microphone lost / back, pause, auto-stop), consent_ok / consent_join (Minerva)."""
        t = round(self.samples / SR, 2)    # recorded audio only: a ⏸ pause is not counted
        item = {"t": t, "note": (note or "").strip()[:200],
                "at": datetime.now().astimezone().isoformat(timespec="seconds")}   # wall clock (consent protocol)
        if kind in ("user", "auto", "consent_ok", "consent_join"):
            item["kind"] = kind
        self.marks.append(item)
        write_json(self.folder / "marks.json", self.marks)
        return item

    # ---- recognition (worker thread) -------------------------------------------------------
    def _work(self):
        while True:
            _, _, task = self.q.get()
            if task is None:
                break
            kind, ch, samples, t0, gen, queued = task
            if kind == PARTIAL and gen != ch.gen:
                ch.partial_busy = False
                continue
            started = time.time()
            try:
                text, lang = self.engine.transcribe(samples, ch.language)
            except Exception:  # noqa: BLE001 - one bad segment must not stop the capture
                log.exception("live recognition failed")
                self.stats["errors"] += 1
                text, lang = "", None
            took = time.time() - started
            dur = len(samples) / SR
            self.stats["decode_s"] += took
            self.stats["audio_s"] += dur
            if kind == PARTIAL:
                ch.partial_busy = False
                self.stats["partials"] += 1
                if gen == ch.gen and text:
                    self.emit({"type": "partial", "ch": ch.name, "text": text, "t0": round(t0, 2)})
                continue
            self.stats["finals"] += 1
            self.stats["wait_s"] += started - queued
            if not text:
                self.emit({"type": "drop", "ch": ch.name})
                continue
            line = {"type": "final", "ch": ch.name, "text": text, "t0": round(t0, 2), "t1": round(t0 + dur, 2),
                    "lang": lang, "decode_ms": int(took * 1000), "wait_ms": int((started - queued) * 1000)}
            self.live_file.write(json.dumps(line, ensure_ascii=False) + "\n")
            self.live_file.flush()
            self.emit(line)

    # ---- numbers for the page --------------------------------------------------------------
    def snapshot(self):
        cpu_t, wall_t = time.process_time(), time.time()
        cpu = (cpu_t - self.cpu0[0]) / max(wall_t - self.cpu0[1], 1e-6) / _cores() * 100
        self.cpu0 = (cpu_t, wall_t)
        self.cpu_samples.append(cpu)
        s = self.stats
        peaks = {ch.name: round(ch.peak, 3) for ch in self.channels}
        for ch in self.channels:
            ch.peak = 0.0
        return {
            "type": "stats", "seconds": round(self.samples / SR, 1), "queue": self.q.qsize(),
            "rtf": round(s["decode_s"] / s["audio_s"], 3) if s["audio_s"] else None,
            "avg_wait_ms": int(s["wait_s"] / s["finals"] * 1000) if s["finals"] else None,
            "cpu": round(cpu), "finals": s["finals"], "errors": s["errors"], "peaks": peaks,
        }

    # ---- stop ------------------------------------------------------------------------------
    def stop(self, reason="stop"):
        if self.closed:
            return self.samples / SR
        self.closed = True
        for ch in self.channels:
            if ch.pending.size:
                pad = np.zeros(ch.window - ch.pending.size, np.float32)
                ch.vad.accept_waveform(np.concatenate([ch.pending, pad]))
                ch.pending = np.zeros(0, np.float32)
            ch.vad.flush()
            self._collect_finals(ch)
        if self.thread is not None:
            self.q.put((STOP, next(self.seq), None))
            self.thread.join(timeout=180)
        self.wav.close()
        self.live_file.close()
        duration = round(self.samples / SR, 2)
        s = self.stats
        live = {
            "engine": getattr(self.engine, "name", None), "device": getattr(self.engine, "device", None),
            "finals": s["finals"], "errors": s["errors"],
            "rtf": round(s["decode_s"] / s["audio_s"], 3) if s["audio_s"] else None,
            "avg_decode_ms": int(s["decode_s"] / max(s["finals"] + s["partials"], 1) * 1000),
            "avg_wait_ms": int(s["wait_s"] / s["finals"] * 1000) if s["finals"] else None,
            "cpu_avg": round(sum(self.cpu_samples) / len(self.cpu_samples)) if self.cpu_samples else None,
            "cpu_max": round(max(self.cpu_samples)) if self.cpu_samples else None,
            "unfinished": bool(self.thread is not None and self.thread.is_alive()),
        }
        recorded = {"both": ("L", "R"), "mic": ("L",), "meeting": ("R",)}.get(self.meta.get("mode", "both"), ("L", "R"))
        levels = self.levels.report(recorded)
        result = {"duration": duration, "live": live, "stopped": reason, "marks": len(self.marks), "levels": levels,
                  "silent": silent_channels(levels)}
        # merge into the file as it is now: a rename made during the recording must survive
        current = read_json(self.folder / "meta.json", None) or dict(self.meta)
        current.update(result)
        write_json(self.folder / "meta.json", current)
        self.meta = current
        return duration


def _cores():
    import os

    return os.cpu_count() or 1
