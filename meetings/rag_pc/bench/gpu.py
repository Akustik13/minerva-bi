"""Video memory and the Ollama models that share the GTX 1070 with the bench."""
import json
import subprocess
import sys
import threading
import time
import urllib.request

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def memory():
    """{'total','used','free'} in MB and 'util' %, or None without nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
        ).stdout.strip().splitlines()[0]
        name, total, used, free, util = [x.strip() for x in out.split(",")]
        return {"name": name, "total": int(total), "used": int(used), "free": int(free), "util": int(util)}
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return None


def _ollama(url, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ollama_loaded(url):
    """Models Ollama holds in memory now: [{'name', 'vram_mb'}]; [] when Ollama is down."""
    try:
        models = _ollama(url, "/api/ps").get("models", [])
    except Exception:  # noqa: BLE001 - Ollama down or busy: treat as nothing loaded
        return []
    return [{"name": m.get("name"), "vram_mb": round((m.get("size_vram") or 0) / 2**20)} for m in models]


def ollama_installed(url):
    try:
        return [m.get("name") for m in _ollama(url, "/api/tags").get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def is_cloud_tag(entry):
    """An Ollama «cloud» model: listed in /api/tags like a local one, but it runs on ollama.com (the text would
    leave the RAG PC). Recognised by the name (…cloud, …-cloud, …:cloud) or by remote_host / remote_model."""
    if isinstance(entry, str):
        return entry.strip().lower().endswith("cloud")
    if not isinstance(entry, dict):
        return True
    return bool(entry.get("remote_host") or entry.get("remote_model")) or is_cloud_tag(entry.get("name") or "")


def ollama_local(url):
    """Names of the models that really run on this PC (cloud tags left out); [] when Ollama is down."""
    try:
        models = _ollama(url, "/api/tags").get("models", [])
    except Exception:  # noqa: BLE001
        return []
    return [m.get("name") for m in models if isinstance(m, dict) and m.get("name") and not is_cloud_tag(m)]


def ollama_unload(url, wait=30):
    """Ask Ollama to drop every loaded model; return the names that were loaded."""
    names = [m["name"] for m in ollama_loaded(url)]
    for name in names:
        try:
            _ollama(url, "/api/generate", {"model": name, "keep_alive": 0, "stream": False}, timeout=60)
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + wait
    while names and time.time() < deadline and ollama_loaded(url):
        time.sleep(1)
    return names


class Sampler(threading.Thread):
    """Samples used VRAM every second while a job runs; .peak is the highest value in MB."""

    def __init__(self, every=1.0):
        super().__init__(daemon=True)
        self.every = every
        self.peak = 0
        self.start_used = None
        self._halt = threading.Event()  # not `_stop`: threading.Thread uses that name internally

    def run(self):
        while not self._halt.is_set():
            m = memory()
            if m:
                if self.start_used is None:
                    self.start_used = m["used"]
                self.peak = max(self.peak, m["used"])
            self._halt.wait(self.every)

    def stop(self):
        self._halt.set()
        self.join(timeout=5)
