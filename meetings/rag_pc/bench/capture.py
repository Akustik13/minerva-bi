"""Capture details the page sends with a live recording (microphone name, applied settings) -> meta.json.

Standard library only (no aiohttp), so it is testable anywhere. Nothing here is trusted for decisions; it is
kept to explain afterwards why a channel was silent.
"""
SETTINGS = ("sampleRate", "channelCount", "echoCancellation", "noiseSuppression", "autoGainControl",
            "voiceIsolation", "latency", "sampleSize")


def short(value):
    """A browser-reported value kept only if it is a short string, a number or a flag."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, str):
        return value[:120]
    return None


def clean(raw):
    """Only known keys with short plain values survive (device ids and anything else are dropped)."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for side in ("mic", "display"):
        d = raw.get(side)
        if isinstance(d, dict):
            s = d.get("settings") if isinstance(d.get("settings"), dict) else {}
            out[side] = {"label": short(d.get("label")) or "", "processing": short(d.get("processing")),
                         "muted": d.get("muted") is True,
                         "settings": {k: short(s[k]) for k in SETTINGS if k in s}}
    for key in ("ctx_rate", "browser", "surface", "ec_modes"):
        if key in raw:
            out[key] = short(raw[key])
    for key in ("inputs", "outputs"):  # the PC's audio devices: which one Windows / Chrome treat as default
        items = raw.get(key)
        if isinstance(items, list):
            out[key] = [{"label": short(d.get("label")) or "", "default": d.get("default") is True, "comm": d.get("comm") is True}
                        for d in items[:12] if isinstance(d, dict)]
    return out or None
