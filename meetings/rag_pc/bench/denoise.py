"""🧹 Noise removal with DeepFilterNet (deep-filter.exe, Rust, CPU) — the comparison tab «🧹 Чистка звуку».

A test recording (stage-0 bench only) gets a cleaned copy as its own session (kind "denoise", meta.denoised_from):
decode to 48 kHz WAV (DeepFilterNet works only on 48 kHz) -> deep-filter -D -a <dB> per channel (L and R are
different people: never one shared mask) -> source.wav (48 kHz, same channels) -> the usual Whisper job for both, so
the page can play and read them side by side. Nothing leaves the RAG PC; the original stays as it was.

The program is not part of the venv: D:\\Meeting\\tools\\deep-filter\\deep-filter-<version>-...exe (official GitHub
release of Rikorose/DeepFilterNet, MIT / Apache-2.0) or the path in config "denoise_exe".
"""
import wave
from pathlib import Path

import numpy as np

SR = 48000
# attenuation limit: how much noise may be removed (dB); 100 = as much as the model can (its default)
LEVELS = {
    "soft": (12, "м'яко (−12 дБ)"),
    "medium": (24, "середньо (−24 дБ)"),
    "full": (100, "повністю"),
}
DEFAULT_LEVEL = "medium"
CHANNELS = {"all": "усі канали", "mic": "лише мій мікрофон"}
TOOL_DIR = ("tools", "deep-filter")
TIMEOUT_PER_MINUTE = 60          # seconds of processing allowed per minute of audio (measured: ~7 s on the i7-6700)


def exe_path(cfg):
    """The deep-filter program: config "denoise_exe", else the newest deep-filter*.exe in <root>/tools/deep-filter."""
    wanted = str(cfg.get("denoise_exe") or "").strip()
    if wanted:
        p = Path(wanted)
        return p if p.is_file() else None
    folder = Path(cfg.root, *TOOL_DIR)
    if not folder.is_dir():
        return None
    # Windows: deep-filter-<version>-x86_64-pc-windows-msvc.exe; Linux later: the same name without an extension
    found = sorted(p for p in folder.glob("deep-filter*") if p.is_file() and p.suffix.lower() in (".exe", ""))
    return found[-1] if found else None


def info(cfg):
    """For /api/status: is the program there, which file (name only), the levels for the page."""
    exe = exe_path(cfg)
    return {"ready": exe is not None, "exe": exe.name if exe else "",
            "folder": str(Path(cfg.root, *TOOL_DIR)),
            "levels": [{"key": k, "db": db, "label": label} for k, (db, label) in LEVELS.items()],
            "default": DEFAULT_LEVEL}


def is_stereo(meta) -> bool:
    """Live recordings are L = my microphone, R = the meeting; an upload marked «two speakers» is split too."""
    return meta.get("kind") == "live" or meta.get("source_kind") == "live" or bool(meta.get("split"))


def decode48(path, stereo):
    """Any audio/video file -> int16 at 48 kHz: shape (n,) mono or (n, 2) stereo."""
    import av

    raw = bytearray()
    with av.open(str(path), mode="r") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio stream")
        resampler = av.AudioResampler(format="s16", layout="stereo" if stereo else "mono", rate=SR)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                raw += out.to_ndarray().tobytes()
        for out in resampler.resample(None):
            raw += out.to_ndarray().tobytes()
    audio = np.frombuffer(bytes(raw), dtype=np.int16)
    return audio.reshape(-1, 2) if stereo else audio


def write_wav(path, samples):
    samples = np.asarray(samples, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if samples.ndim == 1 else samples.shape[1])
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.ascontiguousarray(samples).tobytes())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        ch = w.getnchannels()
    return data.reshape(-1, ch) if ch > 1 else data


def prepare(src, folder, stereo, channels="all"):
    """48 kHz mono WAVs to clean (dn_in_<ch>.wav) and the ones kept as they are. -> plan dict."""
    folder = Path(folder)
    audio = decode48(src, stereo)
    tracks = {"L": audio[:, 0], "R": audio[:, 1]} if stereo else {"M": audio}
    clean = ["L"] if stereo and channels == "mic" else list(tracks)
    files = {}
    for ch, data in tracks.items():
        files[ch] = folder / f"dn_in_{ch}.wav"
        write_wav(files[ch], data)
    return {"files": {ch: str(p) for ch, p in files.items()}, "clean": clean, "frames": int(len(audio)),
            "seconds": round(len(audio) / SR, 1), "stereo": stereo}


def command(exe, plan, out_dir, level):
    db = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])[0]
    return [str(exe), "-D", "-a", str(db), "-o", str(out_dir), *[plan["files"][ch] for ch in plan["clean"]]]


def merge(plan, out_dir, folder):
    """Cleaned channels (+ the kept ones) -> source.wav at 48 kHz; the same length as the original (deep-filter -D
    ends a few ms early: padded with silence). Temporary files go."""
    folder, out_dir = Path(folder), Path(out_dir)
    n = plan["frames"]
    tracks = []
    for ch in (("L", "R") if plan["stereo"] else ("M",)):
        src = Path(plan["files"][ch])
        path = out_dir / src.name if ch in plan["clean"] else src
        if not path.is_file():
            raise FileNotFoundError(f"deep-filter did not write {src.name}")
        data = read_wav(path)
        if data.ndim > 1:
            data = data[:, 0]
        data = data[:n] if len(data) >= n else np.concatenate([data, np.zeros(n - len(data), dtype=np.int16)])
        tracks.append(data)
    target = folder / "source.wav"
    write_wav(target, tracks[0] if len(tracks) == 1 else np.stack(tracks, axis=1))
    cleanup(plan, out_dir)
    return target


def cleanup(plan, out_dir):
    for p in list(plan.get("files", {}).values()):
        Path(p).unlink(missing_ok=True)
    out_dir = Path(out_dir)
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            p.unlink(missing_ok=True)
        out_dir.rmdir()


def timeout(seconds):
    return max(120, int(seconds / 60 * TIMEOUT_PER_MINUTE) + 60)
