"""
run_profile() — orchestrates a single AutoImportProfile run:
  1. Check if due (or force=True)
  2. Load files from folder or URL
  3. For each file: hash-check, parse, call importer, archive, log
  4. Update schedule
  5. Notify
"""
import fnmatch
import hashlib
import os
import shutil
import time
from io import BytesIO

import pandas as pd
import requests
from django.utils import timezone

from .models import AutoImportProfile, AutoImportLog


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_folder(profile: AutoImportProfile):
    """Yield (content: bytes, filepath: str) for each matching file in folder."""
    folder = profile.source_path
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Папка не знайдена: {folder}")

    masks = [m.strip() for m in profile.file_mask.split(';') if m.strip()]
    files = sorted(os.listdir(folder))

    for filename in files:
        filepath = os.path.join(folder, filename)
        if not os.path.isfile(filepath):
            continue
        if any(fnmatch.fnmatch(filename.lower(), m.lower()) for m in masks):
            with open(filepath, 'rb') as f:
                yield f.read(), filepath


def _load_url(profile: AutoImportProfile):
    """Yield (content: bytes, url: str) for URL source."""
    url = profile.source_path
    resp = requests.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    yield resp.content, url


def _get_excel_sheets(content: bytes) -> list:
    """Return list of sheet names for an Excel file, or [] for CSV."""
    try:
        xls = pd.ExcelFile(BytesIO(content))
        return xls.sheet_names
    except Exception:
        return []


def _parse_to_df(content: bytes, source_name: str, sheet_name: str = '') -> pd.DataFrame:
    """Parse bytes (Excel or CSV) into a DataFrame.

    sheet_name: explicit sheet to use. Empty = auto (first sheet or header-search).
    """
    name_lower = source_name.lower().split('?')[0]  # strip URL params

    if name_lower.endswith('.csv') or 'format=csv' in source_name.lower():
        return pd.read_csv(BytesIO(content), dtype=object)

    if name_lower.endswith(('.xlsx', '.xls')):
        xls = pd.ExcelFile(BytesIO(content))

        # Resolve which sheet to use
        if sheet_name and sheet_name in xls.sheet_names:
            sheet = sheet_name
        else:
            sheet = xls.sheet_names[0]

        # Auto-detect header row (first 10 rows)
        preview = pd.read_excel(BytesIO(content), sheet_name=sheet, header=None, nrows=10, dtype=object)
        header_row = 0
        for i, row in preview.iterrows():
            vals = [str(v).strip().lower() for v in row if v is not None]
            if any(v in vals for v in ('sku', 'order_number', 'order number', 'id full', 'артикул', 'замовлення')):
                header_row = i
                break
        return pd.read_excel(BytesIO(content), sheet_name=sheet, header=header_row, dtype=object)

    # Default: try CSV
    return pd.read_csv(BytesIO(content), dtype=object)


def _archive_file(filepath: str):
    """Move processed file to _done/ subfolder."""
    folder = os.path.dirname(filepath)
    done_dir = os.path.join(folder, '_done')
    os.makedirs(done_dir, exist_ok=True)
    dest = os.path.join(done_dir, os.path.basename(filepath))
    # If dest already exists, add timestamp suffix
    if os.path.exists(dest):
        ts = timezone.now().strftime('%Y%m%d_%H%M%S')
        base, ext = os.path.splitext(os.path.basename(filepath))
        dest = os.path.join(done_dir, f'{base}_{ts}{ext}')
    shutil.move(filepath, dest)


def _call_importer(profile: AutoImportProfile, df: pd.DataFrame, dry_run: bool) -> dict:
    from .importers import import_sales, import_products, import_receipt, import_adjust

    cm = profile.column_map or {}

    if profile.import_type == AutoImportProfile.TYPE_SALES:
        return import_sales(df, source=profile.name.lower().replace(' ', '_'),
                            conflict_strategy=profile.conflict_strategy,
                            dry_run=dry_run, column_map=cm)
    elif profile.import_type == AutoImportProfile.TYPE_PRODUCTS:
        return import_products(df, dry_run=dry_run, column_map=cm)
    elif profile.import_type == AutoImportProfile.TYPE_RECEIPT:
        return import_receipt(df, dry_run=dry_run, column_map=cm)
    elif profile.import_type == AutoImportProfile.TYPE_ADJUST:
        return import_adjust(df, dry_run=dry_run, column_map=cm)
    else:
        return {'created': 0, 'updated': 0, 'skipped': 0, 'errors': [f'Невідомий тип імпорту: {profile.import_type}']}


def run_profile(profile_id: int, dry_run: bool = False, force: bool = False) -> list:
    """
    Run a single AutoImportProfile. Returns list of AutoImportLog instances created.
    """
    profile = AutoImportProfile.objects.get(pk=profile_id)
    logs = []

    if not force and not profile.is_due():
        return logs

    effective_dry = dry_run or profile.dry_run_mode

    # Load files
    try:
        if profile.source_type == AutoImportProfile.SOURCE_FOLDER:
            file_iter = list(_load_folder(profile))
        else:
            file_iter = list(_load_url(profile))
    except Exception as e:
        log = AutoImportLog.objects.create(
            profile=profile,
            source_name=profile.source_path,
            file_hash='',
            status=AutoImportLog.STATUS_ERROR,
            errors_count=1,
            error_detail=str(e),
        )
        logs.append(log)
        profile.update_schedule()
        return logs

    for content, source_name in file_iter:
        t_start = time.time()
        file_hash = _sha256(content)

        # Dedup check
        if not force and AutoImportLog.objects.filter(
            profile=profile, file_hash=file_hash, status=AutoImportLog.STATUS_OK
        ).exists():
            log = AutoImportLog.objects.create(
                profile=profile,
                source_name=os.path.basename(source_name),
                file_hash=file_hash,
                status=AutoImportLog.STATUS_SKIPPED,
            )
            logs.append(log)
            continue

        # Parse
        try:
            df = _parse_to_df(content, source_name, sheet_name=profile.sheet_name or '')
        except Exception as e:
            log = AutoImportLog.objects.create(
                profile=profile,
                source_name=os.path.basename(source_name),
                file_hash=file_hash,
                status=AutoImportLog.STATUS_ERROR,
                errors_count=1,
                error_detail=f'Помилка парсингу: {e}',
                duration_ms=int((time.time() - t_start) * 1000),
            )
            logs.append(log)
            continue

        # Import
        try:
            stats = _call_importer(profile, df, effective_dry)
        except Exception as e:
            log = AutoImportLog.objects.create(
                profile=profile,
                source_name=os.path.basename(source_name),
                file_hash=file_hash,
                status=AutoImportLog.STATUS_ERROR,
                errors_count=1,
                error_detail=f'Помилка імпорту: {e}',
                duration_ms=int((time.time() - t_start) * 1000),
            )
            logs.append(log)
            continue

        errors = stats.get('errors', [])
        status = AutoImportLog.STATUS_DRY_RUN if effective_dry else (
            AutoImportLog.STATUS_ERROR if errors and stats.get('created', 0) == 0
            else AutoImportLog.STATUS_OK
        )

        log = AutoImportLog.objects.create(
            profile=profile,
            source_name=os.path.basename(source_name),
            file_hash=file_hash,
            status=status,
            records_created=stats.get('created', 0),
            records_updated=stats.get('updated', 0),
            records_skipped=stats.get('skipped', 0),
            errors_count=len(errors),
            error_detail='\n'.join(errors[:10]),
            duration_ms=int((time.time() - t_start) * 1000),
        )
        logs.append(log)

        # Archive processed file
        if (
            not effective_dry
            and status == AutoImportLog.STATUS_OK
            and profile.archive_processed
            and profile.source_type == AutoImportProfile.SOURCE_FOLDER
        ):
            try:
                _archive_file(source_name)
            except Exception:
                pass  # archiving failure doesn't block the run

    # Update schedule after all files
    profile.update_schedule()

    # Notify
    if profile.notify and logs:
        _send_notify(profile, logs)

    return logs


def _send_notify(profile: AutoImportProfile, logs: list):
    """Send notify_sync_result summary for this profile run."""
    try:
        from dashboard.notifications import notify_sync_result
        total_created = sum(l.records_created for l in logs)
        total_updated = sum(l.records_updated for l in logs)
        errors = []
        for l in logs:
            if l.error_detail:
                errors.extend(l.error_detail.splitlines()[:3])

        stats = {
            'created': total_created,
            'updated': total_updated,
            'errors': errors,
            'changes': [],
        }
        notify_sync_result(
            source=f'Авто-імпорт: {profile.name}',
            stats=stats,
            force_notify=False,
        )
    except Exception:
        pass
