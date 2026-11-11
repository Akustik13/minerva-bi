"""Sessions on disk: <data>/sessions/<id>/ with meta.json, audio, live captions, transcript, moments.

Ids are generated here and validated on every request, so a path from the browser never
leaves the sessions folder. A session recorded for Minerva (meta.minerva, minerva.json) is private: it is never
listed or served to the page, and it is deleted with `remove` (content first, checked, then hidden as .deleting-<id>
when Windows keeps a file open), never with a silent rmtree.
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
AUDIO_EXT_RE = re.compile(r"^\.[a-z0-9]{1,5}$")
DELETING = ".deleting-"                     # a folder whose deletion is not finished (ID_RE never matches it)
KEEP_LAST = ("minerva.json", "meta.json")   # removed last: until then the folder is still known as private


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def valid_id(sid):
    return bool(ID_RE.match(sid or ""))


REPLACE_TRIES = 40  # x 50 ms


def write_json(path, data):
    """Atomic write: a temp file per thread, then os.replace.

    On Windows os.replace fails with PermissionError while another handle has the target open - the server reads
    progress.json every second, the pages read meta.json and transcript.json, each for a few milliseconds. The review
    of 10.11.2026 measured 0.5-1.4 % of writes meeting such a read, enough to fail a long transcription, so the
    replace is retried for up to 2 s.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.05)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _retry(action, tries=REPLACE_TRIES):
    """Run a file operation, retrying while Windows reports the file as in use (another handle, an antivirus scan)."""
    for attempt in range(tries):
        try:
            action()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt == tries - 1:
                return False
            time.sleep(0.05)
        except OSError:
            return False
    return False


def remove_folder(folder):
    """Delete a session folder safely: content first (retried), check that nothing else is left, then minerva.json and
    meta.json, then the folder. True only when the folder is really gone."""
    folder = Path(folder)
    if not folder.exists():
        return True
    try:
        inside = sorted(folder.rglob("*"), key=lambda x: len(x.parts), reverse=True)   # deepest first
    except OSError:
        return False
    for p in inside:
        if p.parent == folder and p.name in KEEP_LAST:
            continue
        _retry(p.rmdir if p.is_dir() else p.unlink)
    try:
        if any(p.name not in KEEP_LAST for p in folder.iterdir()):
            return False
    except OSError:
        return False
    for name in KEEP_LAST:
        if not _retry((folder / name).unlink):
            return False
    _retry(folder.rmdir)
    return not folder.exists()


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.sessions_dir

    def path(self, sid):
        if not valid_id(sid):
            raise KeyError(sid)
        p = self.root / sid
        if not p.is_dir():
            raise KeyError(sid)
        return p

    def create(self, kind, title, **extra):
        sid = new_id()
        p = self.root / sid
        p.mkdir(parents=True)
        meta = {"id": sid, "kind": kind, "title": (title or "").strip()[:200],
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), **extra}
        write_json(p / "meta.json", meta)
        return sid, p, meta

    def meta(self, sid):
        return read_json(self.path(sid) / "meta.json", {})

    def update_meta(self, sid, **changes):
        p = self.path(sid)
        meta = read_json(p / "meta.json", {})
        meta.update(changes)
        write_json(p / "meta.json", meta)
        return meta

    def audio_file(self, sid):
        """Recorded audio.wav, or the uploaded source.<ext>."""
        p = self.path(sid)
        if (p / "audio.wav").exists():
            return p / "audio.wav"
        for f in p.iterdir():
            if f.name.startswith("source.") and AUDIO_EXT_RE.match(f.suffix.lower()):
                return f
        return None

    def live_lines(self, sid):
        f = self.path(sid) / "live.jsonl"
        lines = []
        if f.exists():
            with open(f, encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        lines.append(json.loads(raw))
                    except ValueError:
                        continue
        return lines

    def summary(self, sid):
        p = self.path(sid)
        meta = read_json(p / "meta.json", {})
        transcript = read_json(p / "transcript.json")
        moments = read_json(p / "moments.json")
        return {
            **meta,
            "has_audio": self.audio_file(sid) is not None,
            "transcript": bool(transcript and transcript.get("segments") is not None),
            "transcript_model": (transcript or {}).get("model"),
            "moments": len((moments or {}).get("items", [])) if moments else None,
            "error": read_json(p / "error.json"),
        }

    def private(self, sid):
        """A session the page must never see: recorded for Minerva, or a folder without readable meta.json (a half
        deleted Minerva session must not turn into an ordinary test recording)."""
        p = self.path(sid)
        meta = read_json(p / "meta.json")
        return not isinstance(meta, dict) or bool(meta.get("minerva")) or (p / "minerva.json").exists()

    def list(self):
        """Test recordings only (stage 0): Minerva sessions and unreadable folders are never listed."""
        items = []
        for p in sorted(self.root.iterdir(), reverse=True):
            if p.is_dir() and valid_id(p.name):
                try:
                    if self.private(p.name):
                        continue
                    items.append(self.summary(p.name))
                except KeyError:
                    continue
        return items

    def minerva_ids(self):
        """Sessions that belong to Minerva meetings (have minerva.json), oldest first."""
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and valid_id(p.name) and (p / "minerva.json").exists())

    def delete(self, sid):
        """A test recording deleted from the page (stage 0): the same safe deletion as for Minerva sessions."""
        return self.remove(sid)

    def remove(self, sid):
        """'gone' — deleted and checked; 'hidden' — something is still locked, the folder is now .deleting-<id> (never
        listed or served) and retry_deleting() finishes it; 'failed' — not even renamed (meta.json and minerva.json
        stay, so a Minerva session stays private)."""
        if not valid_id(sid):
            raise KeyError(sid)
        folder = self.root / sid
        if remove_folder(folder):
            return "gone"
        hidden = self.root / f"{DELETING}{sid}"
        if not _retry(lambda: os.replace(folder, hidden)):
            return "failed"
        return "gone" if remove_folder(hidden) else "hidden"

    def retry_deleting(self):
        """Finish deletions left as .deleting-<id>; returns the ids that are gone now."""
        done = []
        for p in self.root.iterdir():
            if p.is_dir() and p.name.startswith(DELETING) and remove_folder(p):
                done.append(p.name[len(DELETING):])
        return done

    def gone(self, sid):
        """Neither the session folder nor its .deleting-<id> rest exists any more."""
        return not (self.root / sid).exists() and not (self.root / f"{DELETING}{sid}").exists()

    def delete_audio(self, sid):
        """Remove audio.wav / source.* and check; True when no audio is left."""
        p = self.path(sid)
        for f in list(p.iterdir()):
            if f.is_file() and (f.name == "audio.wav" or f.name.startswith("source.")):
                _retry(f.unlink)
        return self.audio_file(sid) is None

    def delete_texts(self, sid, also_keep=()):
        """After Minerva has everything: delete all but meta.json, minerva.json and the audio (kept only while the
        mode «аудіо + текст» keeps it) and `also_keep`. True when nothing else is left."""
        p = self.path(sid)

        def kept(f):
            return (f.name in KEEP_LAST or f.name in also_keep or f.name == "audio.wav"
                    or f.name.startswith("source."))

        for f in list(p.iterdir()):
            if not kept(f):
                _retry(f.unlink if f.is_file() else (lambda f=f: shutil.rmtree(f)))
        return all(kept(f) for f in p.iterdir())

    def purge(self, days, keep=None):
        """Delete test sessions older than `days` days; returns how many were removed.

        Minerva sessions are left to bench/link.py (their own rules and a 30-day cap); keep(sid) -> True skips a
        session (the live one, a running job)."""
        if not days or days <= 0:
            return 0
        limit = time.time() - days * 86400
        removed = 0
        for p in self.root.iterdir():
            if p.is_dir() and valid_id(p.name):
                if (p / "minerva.json").exists() or (read_json(p / "meta.json") or {}).get("minerva"):
                    continue
                if keep and keep(p.name):
                    continue
                created = time.mktime(time.strptime(p.name[:15], "%Y%m%d-%H%M%S"))
                if created < limit and self.remove(p.name) != "failed":
                    removed += 1
        return removed
