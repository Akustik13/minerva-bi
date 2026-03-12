from pathlib import Path as P

from django.conf import settings as django_settings
from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import path

from .models import BackupLog, BackupPlaceholder, BackupSettings
from . import utils


def _fmt(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


@admin.register(BackupPlaceholder)
class BackupAdmin(admin.ModelAdmin):
    """Custom backup management page — no DB table for placeholder."""

    def get_urls(self):
        return [
            path(
                "",
                self.admin_site.admin_view(self.info_view),
                name="backup_backupplaceholder_changelist",
            ),
            path(
                "run/",
                self.admin_site.admin_view(self.run_view),
                name="backup_run",
            ),
            path(
                "restore/",
                self.admin_site.admin_view(self.restore_view),
                name="backup_restore",
            ),
            path(
                "settings/",
                self.admin_site.admin_view(self.settings_view),
                name="backup_settings",
            ),
            path(
                "delete/<int:pk>/",
                self.admin_site.admin_view(self.delete_log_view),
                name="backup_delete_log",
            ),
            path(
                "browse-dir/",
                self.admin_site.admin_view(self.browse_dir_view),
                name="backup_browse_dir",
            ),
            path(
                "download/<int:pk>/",
                self.admin_site.admin_view(self.download_view),
                name="backup_download_file",
            ),
            path(
                "settings-backup/",
                self.admin_site.admin_view(self.settings_backup_view),
                name="backup_settings_backup",
            ),
            path(
                "settings-restore/",
                self.admin_site.admin_view(self.settings_restore_view),
                name="backup_settings_restore",
            ),
            path(
                "git-status/",
                self.admin_site.admin_view(self.git_status_view),
                name="backup_git_status",
            ),
            path(
                "git-update/",
                self.admin_site.admin_view(self.git_update_view),
                name="backup_git_update",
            ),
        ]

    def _ctx(self, request, **extra):
        return dict(self.admin_site.each_context(request), opts=self.model._meta, **extra)

    def info_view(self, request):
        cfg = BackupSettings.get_settings()
        logs = BackupLog.objects.all()[:50]
        for log in logs:
            log.size_display = _fmt(log.file_size)
        default_dir = getattr(django_settings, "BACKUP_DIR",
                               str(django_settings.BASE_DIR / "backups"))
        # Available backup files for restore section
        available_backups = []
        try:
            raw = utils.list_backups()
            for b in raw:
                b["size_display"] = _fmt(b["size"])
                b["mtime_str"] = b["mtime"].strftime("%d.%m.%Y %H:%M")
                available_backups.append(b)
        except Exception:
            pass

        ctx = self._ctx(
            request,
            title="💾 Резервне копіювання",
            cfg=cfg,
            logs=logs,
            settings_default_dir=default_dir,
            available_backups=available_backups,
        )
        return render(request, "admin/backup/info.html", ctx)

    def run_view(self, request):
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)
        btype = request.POST.get("type", "db")
        if btype == "db":
            log = utils.run_db_backup()
        elif btype == "media":
            log = utils.run_media_backup()
        else:
            log = utils.run_full_backup()
        return JsonResponse({
            "pk": log.pk,
            "status": log.status,
            "file": log.file_path,
            "size": _fmt(log.file_size),
            "duration": log.duration,
            "error": log.error_msg,
            "created_at": log.created_at.strftime("%d.%m.%Y %H:%M:%S"),
            "type_display": log.get_backup_type_display(),
        })

    def restore_view(self, request):
        """AJAX endpoint — restores DB and/or media from a backup file.

        Accepts either a NAS file path (filepath POST param) OR a file upload
        (backup_file field in multipart form). Uploaded files are saved to the
        backup directory before restore.
        """
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)

        restore_type = request.POST.get("restore_type", "db")
        media_path   = request.POST.get("media_path", "").strip()
        uploaded     = request.FILES.get("backup_file")

        filepath = request.POST.get("filepath", "").strip()

        # If file was uploaded, save it to the backup dir
        if uploaded:
            suffix = P(uploaded.name).suffix.lower()
            if suffix not in (".sql", ".zip"):
                return JsonResponse({"error": "Тільки .sql або .zip файли"}, status=403)
            dest = utils.get_backup_dir() / uploaded.name
            with open(dest, "wb") as f:
                for chunk in uploaded.chunks():
                    f.write(chunk)
            filepath = str(dest)

        if not filepath:
            return JsonResponse({"error": "Файл не вказано"}, status=400)

        # Security: block path traversal; allow .sql and .zip only
        resolved = P(filepath).resolve()
        if ".." in filepath or resolved.suffix.lower() not in (".sql", ".zip"):
            return JsonResponse({"error": "Недозволений шлях або тип файлу (тільки .sql/.zip)"}, status=403)

        if restore_type == "db":
            result = utils.restore_db(filepath)
            return JsonResponse(result)

        if restore_type == "media":
            result = utils.restore_media(filepath)
            return JsonResponse(result)

        if restore_type == "full":
            r_db    = utils.restore_db(filepath)
            r_media = utils.restore_media(media_path) if media_path else {"ok": True, "note": "медіа не відновлювались"}
            return JsonResponse({
                "ok":     r_db.get("ok") and r_media.get("ok"),
                "db":     r_db,
                "media":  r_media,
                "error":  (r_db.get("error", "") + " " + r_media.get("error", "")).strip(),
                "duration": round(r_db.get("duration", 0) + r_media.get("duration", 0), 1),
            })

        return JsonResponse({"error": f"Невідомий тип: {restore_type}"}, status=400)

    def settings_view(self, request):
        if request.method != "POST":
            return redirect("admin:backup_backupplaceholder_changelist")
        cfg = BackupSettings.get_settings()
        cfg.backup_dir = request.POST.get("backup_dir", "").strip()
        cfg.include_media = request.POST.get("include_media") == "on"
        cfg.auto_enabled = request.POST.get("auto_enabled") == "on"
        cfg.schedule = request.POST.get("schedule", "daily")
        try:
            cfg.retention = max(1, int(request.POST.get("retention", 10)))
        except (ValueError, TypeError):
            cfg.retention = 10
        cfg.save()
        return redirect("admin:backup_backupplaceholder_changelist")

    def delete_log_view(self, request, pk):
        if request.method == "POST":
            try:
                log = BackupLog.objects.get(pk=pk)
                if log.file_path and "*" not in log.file_path:
                    P(log.file_path).unlink(missing_ok=True)
                log.delete()
            except BackupLog.DoesNotExist:
                pass
        return redirect("admin:backup_backupplaceholder_changelist")

    def browse_dir_view(self, request):
        """AJAX — список підпапок (і опційно .sql/.zip файлів) для браузера директорій."""
        import os
        show_files = request.GET.get("files", "0") == "1"
        path_str = request.GET.get("path", "").strip()
        if not path_str:
            path_str = str(utils.get_backup_dir())
        path_str = os.path.normpath(path_str)
        if not os.path.isdir(path_str):
            parent = os.path.dirname(path_str)
            path_str = parent if os.path.isdir(parent) else str(utils.get_backup_dir())
        parent = os.path.dirname(path_str)
        if parent == path_str:
            parent = None
        dirs, files = [], []
        try:
            with os.scandir(path_str) as it:
                for entry in sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())):
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append({"name": entry.name, "path": entry.path})
                        elif show_files and entry.name.lower().endswith((".sql", ".zip", ".mbackup")):
                            files.append({"name": entry.name, "path": entry.path})
                    except OSError:
                        pass
        except OSError:
            pass
        return JsonResponse({"current": path_str, "parent": parent, "dirs": dirs, "files": files})

    def download_view(self, request, pk):
        """Стрімить файл бекапу для локального збереження."""
        from django.http import FileResponse, HttpResponseNotFound
        try:
            log = BackupLog.objects.get(pk=pk, status=BackupLog.STATUS_OK)
            fp = P(log.file_path)
            if not fp.exists() or "*" in str(fp):
                return HttpResponseNotFound("File not found")
            return FileResponse(open(fp, "rb"), as_attachment=True, filename=fp.name)
        except BackupLog.DoesNotExist:
            return HttpResponseNotFound("Backup log not found")

    def settings_backup_view(self, request):
        """AJAX — create encrypted settings backup with password."""
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)
        password = request.POST.get("password", "").strip()
        if not password:
            return JsonResponse({"error": "Пароль не може бути порожнім"}, status=400)
        result = utils.backup_settings(password)
        return JsonResponse(result)

    def settings_restore_view(self, request):
        """AJAX — decrypt and restore settings from .mbackup (file upload or NAS path)."""
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)
        password = request.POST.get("password", "").strip()
        if not password:
            return JsonResponse({"error": "Пароль обов'язковий для відновлення"}, status=400)

        uploaded = request.FILES.get("backup_file")
        filepath = request.POST.get("filepath", "").strip()

        if uploaded:
            suffix = P(uploaded.name).suffix.lower()
            if suffix != ".mbackup":
                return JsonResponse({"error": "Очікується файл .mbackup"}, status=403)
            source = uploaded.read()
        elif filepath:
            if ".." in filepath or not filepath.endswith(".mbackup"):
                return JsonResponse({"error": "Недозволений шлях або тип файлу (тільки .mbackup)"}, status=403)
            source = filepath
        else:
            return JsonResponse({"error": "Файл не вказано"}, status=400)

        result = utils.restore_settings(source, password)
        return JsonResponse(result)

    def git_status_view(self, request):
        """AJAX — fetch origin and return update status."""
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)
        return JsonResponse(utils.git_status())

    def git_update_view(self, request):
        """AJAX — git pull + gunicorn reload."""
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)
        return JsonResponse(utils.git_pull())

    def has_add_permission(self, request):              return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
    def has_view_permission(self, request, obj=None):   return True
