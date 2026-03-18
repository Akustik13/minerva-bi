"""Backup utility functions — pg_dump + media zip + restore + git updates.

Strategy: Django runs in the 'web' Docker container; PostgreSQL runs in
the 'db' container on the same network. The Dockerfile installs
postgresql-client so pg_dump/psql connect directly to DB_HOST=db.
No docker exec required.
"""
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings

from .models import BackupLog, BackupSettings


# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def get_backup_dir() -> Path:
    cfg = BackupSettings.get_settings()
    raw = (cfg.backup_dir or "").strip()
    if not raw:
        raw = getattr(settings, "BACKUP_DIR", str(settings.BASE_DIR / "backups"))
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_cfg():
    return settings.DATABASES["default"]



def _apply_retention():
    cfg = BackupSettings.get_settings()
    retention = max(1, cfg.retention or 10)
    for btype in [BackupLog.TYPE_DB, BackupLog.TYPE_MEDIA, BackupLog.TYPE_FULL]:
        old_logs = list(
            BackupLog.objects.filter(backup_type=btype, status=BackupLog.STATUS_OK)
            .order_by("-created_at")[retention:]
        )
        for log in old_logs:
            try:
                if log.file_path and "*" not in log.file_path:
                    Path(log.file_path).unlink(missing_ok=True)
            except Exception:
                pass
            log.delete()


def _do_pg_dump(filepath: Path) -> tuple[bool, str]:
    """
    Dump the database to *filepath* using pg_dump.

    Django runs inside a Docker container that has postgresql-client installed
    (see Dockerfile). pg_dump connects directly to the db service via Docker
    network (DB_HOST=db). No docker exec needed.

    Returns (success: bool, error_message: str).
    """
    db = _db_cfg()
    env = os.environ.copy()
    env["PGPASSWORD"] = db.get("PASSWORD", "")

    try:
        cmd = [
            "pg_dump",
            "-h", db.get("HOST", "localhost"),
            "-p", str(db.get("PORT", "5432")),
            "-U", db.get("USER", "tabele"),
            "-f", str(filepath),
            db.get("NAME", "tabele"),
        ]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        if res.returncode == 0:
            return True, ""
        return False, res.stderr.strip() or "pg_dump завершився з помилкою"
    except FileNotFoundError:
        return False, (
            "pg_dump не знайдено. "
            "Перебудуйте Docker образ: docker-compose build web && docker-compose up -d"
        )
    except Exception as exc:
        return False, str(exc)


# ── Backup runners ────────────────────────────────────────────────────────────

def run_db_backup() -> BackupLog:
    log = BackupLog.objects.create(backup_type=BackupLog.TYPE_DB, status=BackupLog.STATUS_RUNNING)
    start = time.time()
    try:
        backup_dir = get_backup_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = backup_dir / f"db_{timestamp}.sql"

        ok, err = _do_pg_dump(filepath)
        if not ok:
            raise RuntimeError(err)

        log.file_path = str(filepath)
        log.file_size = filepath.stat().st_size
        log.status = BackupLog.STATUS_OK
        log.duration = round(time.time() - start, 1)
        log.save()
        _apply_retention()

    except Exception as exc:
        log.status = BackupLog.STATUS_ERROR
        log.error_msg = str(exc)
        log.duration = round(time.time() - start, 1)
        log.save()
    return log


def run_media_backup() -> BackupLog:
    log = BackupLog.objects.create(backup_type=BackupLog.TYPE_MEDIA, status=BackupLog.STATUS_RUNNING)
    start = time.time()
    try:
        backup_dir = get_backup_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        media_root = Path(settings.MEDIA_ROOT)
        archive_base = str(backup_dir / f"media_{timestamp}")
        shutil.make_archive(archive_base, "zip", str(media_root))
        filepath = archive_base + ".zip"

        log.file_path = filepath
        log.file_size = Path(filepath).stat().st_size
        log.status = BackupLog.STATUS_OK
        log.duration = round(time.time() - start, 1)
        log.save()
        _apply_retention()

    except Exception as exc:
        log.status = BackupLog.STATUS_ERROR
        log.error_msg = str(exc)
        log.duration = round(time.time() - start, 1)
        log.save()
    return log


def run_full_backup() -> BackupLog:
    log = BackupLog.objects.create(backup_type=BackupLog.TYPE_FULL, status=BackupLog.STATUS_RUNNING)
    start = time.time()
    errors = []
    total_size = 0
    try:
        backup_dir = get_backup_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # ── DB ───────────────────────────────────────────────────────────────
        db_filepath = backup_dir / f"full_{timestamp}_db.sql"
        ok, err = _do_pg_dump(db_filepath)
        if ok:
            total_size += db_filepath.stat().st_size
        else:
            errors.append(err)

        # ── Media ────────────────────────────────────────────────────────────
        cfg = BackupSettings.get_settings()
        if cfg.include_media:
            media_root = Path(settings.MEDIA_ROOT)
            archive_base = str(backup_dir / f"full_{timestamp}_media")
            shutil.make_archive(archive_base, "zip", str(media_root))
            total_size += Path(archive_base + ".zip").stat().st_size

        log.file_path = str(backup_dir / f"full_{timestamp}_*")
        log.file_size = total_size
        log.status = BackupLog.STATUS_ERROR if errors else BackupLog.STATUS_OK
        log.error_msg = "\n".join(errors)
        log.duration = round(time.time() - start, 1)
        log.save()
        _apply_retention()

    except Exception as exc:
        log.status = BackupLog.STATUS_ERROR
        log.error_msg = str(exc)
        log.duration = round(time.time() - start, 1)
        log.save()
    return log


# ── Restore helpers ───────────────────────────────────────────────────────────

def list_backups() -> list:
    """Scan backup directory and return available backup files sorted by date."""
    backup_dir = get_backup_dir()
    result = []

    for f in backup_dir.glob("db_*.sql"):
        try:
            stat = f.stat()
            result.append({
                "type": "db", "label": "База даних",
                "path": str(f), "name": f.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime),
            })
        except OSError:
            pass

    for f in backup_dir.glob("media_*.zip"):
        try:
            stat = f.stat()
            result.append({
                "type": "media", "label": "Медіа файли",
                "path": str(f), "name": f.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime),
            })
        except OSError:
            pass

    seen = set()
    for f in backup_dir.glob("full_*_db.sql"):
        try:
            # extract timestamp: full_YYYYMMDD_HHMMSS_db.sql → YYYYMMDD_HHMMSS
            ts = f.name[5:-7]  # strip "full_" prefix and "_db.sql" suffix
            if ts in seen:
                continue
            seen.add(ts)
            media_part = backup_dir / f"full_{ts}_media.zip"
            total_size = f.stat().st_size + (media_part.stat().st_size if media_part.exists() else 0)
            result.append({
                "type": "full", "label": "Повний (БД + медіа)",
                "path": str(f),
                "media_path": str(media_part) if media_part.exists() else "",
                "name": f"full_{ts}",
                "size": total_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime),
            })
        except OSError:
            pass

    for f in backup_dir.glob("settings_*.mbackup"):
        try:
            stat = f.stat()
            result.append({
                "type": "settings", "label": "Налаштування",
                "path": str(f), "name": f.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime),
            })
        except OSError:
            pass

    return sorted(result, key=lambda x: x["mtime"], reverse=True)


def restore_db(filepath: str) -> dict:
    """Restore PostgreSQL database from a SQL dump file.

    Drops and recreates the public schema, then pipes the SQL file to psql.
    psql connects directly to the db service (postgresql-client in Dockerfile).
    Returns dict: {"ok": bool, "duration": float, "error": str (if failed)}
    """
    start = time.time()
    fp = Path(filepath)
    if not fp.exists():
        return {"ok": False, "error": f"Файл не знайдено: {filepath}"}

    db = _db_cfg()
    env = os.environ.copy()
    env["PGPASSWORD"] = db.get("PASSWORD", "")
    db_host = db.get("HOST", "localhost")
    db_port = str(db.get("PORT", "5432"))
    db_user = db.get("USER", "tabele")
    db_name = db.get("NAME", "tabele")

    clean_sql = (
        f"DROP SCHEMA public CASCADE; "
        f"CREATE SCHEMA public; "
        f"GRANT ALL ON SCHEMA public TO {db_user}; "
        f"GRANT ALL ON SCHEMA public TO public;"
    )

    try:
        # Step 1 — drop and recreate schema
        subprocess.run(
            ["psql", "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name, "-c", clean_sql],
            env=env, capture_output=True, text=True, timeout=60, check=True,
        )
        # Step 2 — restore from SQL dump via stdin
        with open(fp, "rb") as f:
            res = subprocess.run(
                ["psql", "-h", db_host, "-p", db_port, "-U", db_user, "-d", db_name],
                env=env, stdin=f, capture_output=True, timeout=600,
            )
        if res.returncode == 0:
            return {"ok": True, "duration": round(time.time() - start, 1)}
        return {
            "ok": False,
            "duration": round(time.time() - start, 1),
            "error": res.stderr.decode("utf-8", errors="replace").strip() or "psql завершився з помилкою",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "duration": round(time.time() - start, 1),
            "error": (
                "psql не знайдено. "
                "Перебудуйте Docker образ: docker-compose build web && docker-compose up -d"
            ),
        }
    except Exception as exc:
        return {"ok": False, "duration": round(time.time() - start, 1), "error": str(exc)}


def restore_media(filepath: str) -> dict:
    """Restore media files from a ZIP archive (extracted into MEDIA_ROOT)."""
    start = time.time()
    fp = Path(filepath)
    if not fp.exists():
        return {"ok": False, "error": f"Файл не знайдено: {filepath}"}
    try:
        media_root = Path(settings.MEDIA_ROOT)
        media_root.mkdir(parents=True, exist_ok=True)
        shutil.unpack_archive(str(fp), str(media_root), "zip")
        return {"ok": True, "duration": round(time.time() - start, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "duration": round(time.time() - start, 1)}


# ── Settings backup ───────────────────────────────────────────────────────────

# Models to include in settings backup (app_label.model_name, all lowercase)
_SETTINGS_MODELS = [
    "auth.user",
    "auth.group",
    "api.apikey",
    "bots.digikeyconfig",
    "config.notificationsettings",
    "config.systemsettings",
    "accounting.companysettings",
    "shipping.shippingsettings",
    "shipping.carrier",
    "backup.backupsettings",
]



def backup_settings() -> dict:
    """Serialize selected config models to JSON and save as .mbackup file.

    Returns a plain dict (not BackupLog) so it works even if the DB is unavailable.
    """
    start = time.time()
    try:
        import json, threading as _threading
        from django.apps import apps as django_apps
        from django.core import serializers as dj_serializers

        # Серіалізацію моделей виконуємо у daemon-потоці з таймаутом
        # щоб не зависати при недоступній БД
        _result = [None]
        _done   = _threading.Event()

        def _serialize_models():
            entries = []
            for model_label in _SETTINGS_MODELS:
                try:
                    app_label, model_name = model_label.split(".")
                    model = django_apps.get_model(app_label, model_name)
                    objs = list(model.objects.all())
                    if objs:
                        serialized = dj_serializers.serialize("json", objs, indent=2)
                        entries.append({"model": model_label, "data": json.loads(serialized)})
                except LookupError:
                    pass
                except Exception:
                    pass
            _result[0] = entries
            _done.set()

        _t = _threading.Thread(target=_serialize_models, daemon=True)
        _t.start()

        if not _done.wait(timeout=5):
            return {"ok": False, "status": "error",
                    "error": "БД недоступна (таймаут 5с). Для бекапу налаштувань потрібна запущена PostgreSQL.",
                    "duration": round(time.time() - start, 1)}

        all_entries = _result[0] or []

        if not all_entries:
            return {"ok": False, "status": "error",
                    "error": "Нічого не знайдено для бекапу (БД порожня або недоступна).",
                    "duration": round(time.time() - start, 1)}

        # Тепер БД доступна — отримуємо директорію для бекапу
        backup_dir = get_backup_dir()
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath   = backup_dir / f"settings_{timestamp}.mbackup"

        payload = json.dumps(all_entries, ensure_ascii=False, indent=2).encode("utf-8")

        with open(filepath, "wb") as f:
            f.write(payload)

        size = filepath.stat().st_size
        duration = round(time.time() - start, 1)

        # Try to save log entry (non-fatal if DB unavailable)
        try:
            BackupLog.objects.create(
                backup_type=BackupLog.TYPE_SETTINGS,
                status=BackupLog.STATUS_OK,
                file_path=str(filepath),
                file_size=size,
                duration=duration,
            )
        except Exception:
            pass

        return {
            "ok": True,
            "status": "ok",
            "file": filepath.name,
            "size": fmt_size(size),
            "duration": duration,
        }

    except Exception as exc:
        duration = round(time.time() - start, 1)
        try:
            BackupLog.objects.create(
                backup_type=BackupLog.TYPE_SETTINGS,
                status=BackupLog.STATUS_ERROR,
                error_msg=str(exc),
                duration=duration,
            )
        except Exception:
            pass
        return {"ok": False, "status": "error", "error": str(exc), "duration": duration}


def restore_settings(source) -> dict:
    """Restore settings from a .mbackup file or raw bytes (plain JSON).

    *source* can be a file path (str/Path) or raw bytes (uploaded file content).
    """
    import json
    from django.core import serializers as dj_serializers

    start = time.time()
    try:
        if isinstance(source, (str, Path)):
            fp = Path(source)
            if not fp.exists():
                return {"ok": False, "error": f"Файл не знайдено: {source}"}
            raw = fp.read_bytes()
        else:
            raw = bytes(source)

        if len(raw) < 2:
            return {"ok": False, "error": "Файл пошкоджений або невірний формат."}

        all_entries = json.loads(raw.decode("utf-8"))
        restored = 0
        warnings = []
        for entry in all_entries:
            try:
                data_json = json.dumps(entry["data"])
                for obj in dj_serializers.deserialize("json", data_json):
                    obj.save()
                    restored += 1
            except Exception as exc:
                warnings.append(f"{entry.get('model', '?')}: {exc}")

        result = {"ok": True, "duration": round(time.time() - start, 1), "count": restored}
        if warnings:
            result["warnings"] = "; ".join(warnings[:5])
        return result

    except Exception as exc:
        return {"ok": False, "error": str(exc), "duration": round(time.time() - start, 1)}


# ── Git update helpers ────────────────────────────────────────────────────────

_GIT_DIR = Path("/app")  # working tree inside Docker container


def _git(*args, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={_GIT_DIR}", *args],
        cwd=str(_GIT_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def git_status() -> dict:
    """Fetch origin and return how many commits the local branch is behind.

    Returns:
      {ok, behind, commits[{hash, subject}], current_hash, current_subject,
       branch, remote_url, error (if not ok)}
    """
    try:
        # Fetch (non-fatal — might fail if no network)
        fetch = _git("fetch", "origin", timeout=20)
        fetch_ok = fetch.returncode == 0
        fetch_err = fetch.stderr.strip() if not fetch_ok else ""

        # Current branch
        branch_r = _git("rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_r.stdout.strip() or "main"

        # Remote URL
        remote_r = _git("remote", "get-url", "origin")
        remote_url = remote_r.stdout.strip()

        # Commits behind
        log_r = _git("log", f"HEAD..origin/{branch}", "--oneline")
        raw_commits = [l.strip() for l in log_r.stdout.strip().splitlines() if l.strip()]
        commits = [{"hash": c[:7], "subject": c[8:]} for c in raw_commits]

        # Current commit
        cur_r = _git("log", "-1", "--format=%h|||%s")
        cur_parts = cur_r.stdout.strip().split("|||", 1)
        current_hash = cur_parts[0] if cur_parts else ""
        current_subject = cur_parts[1] if len(cur_parts) > 1 else ""

        return {
            "ok": True,
            "fetch_ok": fetch_ok,
            "fetch_err": fetch_err,
            "branch": branch,
            "remote_url": remote_url,
            "behind": len(commits),
            "commits": commits,
            "current_hash": current_hash,
            "current_subject": current_subject,
        }

    except FileNotFoundError:
        return {"ok": False, "error": "git не знайдено в контейнері. Потрібен --rebuild образу."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git fetch timeout — перевірте мережу NAS."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_pull() -> dict:
    """Pull latest changes from origin and reload gunicorn workers (SIGHUP PID 1).

    Returns:
      {ok, output, duration, reloaded (bool), error (if not ok)}
    """
    start = time.time()
    try:
        branch_r = _git("rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_r.stdout.strip() or "main"

        fetch_r = _git("fetch", "origin", branch, timeout=60)
        if fetch_r.returncode != 0:
            return {
                "ok": False,
                "error": (fetch_r.stderr.strip() or fetch_r.stdout.strip() or "git fetch failed"),
                "duration": round(time.time() - start, 1),
            }

        reset_r = _git("reset", "--hard", f"origin/{branch}", timeout=30)
        if reset_r.returncode != 0:
            return {
                "ok": False,
                "error": (reset_r.stderr.strip() or reset_r.stdout.strip() or "git reset failed"),
                "duration": round(time.time() - start, 1),
            }

        output = reset_r.stdout.strip()

        # Graceful gunicorn reload — send SIGHUP to PID 1 (gunicorn master)
        reloaded = False
        try:
            os.kill(1, signal.SIGHUP)
            reloaded = True
        except (ProcessLookupError, PermissionError):
            pass  # not PID 1 or not gunicorn — skip

        return {
            "ok": True,
            "output": output,
            "reloaded": reloaded,
            "duration": round(time.time() - start, 1),
        }

    except FileNotFoundError:
        return {"ok": False, "error": "git не знайдено.", "duration": round(time.time() - start, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "duration": round(time.time() - start, 1)}


def run_migrate() -> dict:
    """Run manage.py migrate and return output.

    Returns:
      {ok, output, duration, error (if not ok)}
    """
    start = time.time()
    try:
        import sys
        result = subprocess.run(
            [sys.executable, "manage.py", "migrate", "--no-input"],
            capture_output=True, text=True, timeout=120,
            cwd=str(settings.BASE_DIR),
        )
        output = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0
        return {
            "ok": ok,
            "output": output,
            "duration": round(time.time() - start, 1),
            **({"error": output} if not ok else {}),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "migrate timeout (>120с)", "duration": round(time.time() - start, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "duration": round(time.time() - start, 1)}
