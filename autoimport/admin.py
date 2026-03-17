"""
AutoImport admin — 3-step wizard (same pattern as inventory/sales import-excel).
URLs:
  /admin/autoimport/autoimportprofile/run/       — new one-time import or create profile
  /admin/autoimport/autoimportprofile/<pk>/run/  — edit/re-run existing profile
  /admin/autoimport/autoimportprofile/<pk>/run-now/ — quick run (no wizard)
"""
import base64
import json
import os
import time
import types

import requests
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .importers import IMPORT_FIELDS
from .models import AutoImportLog, AutoImportProfile

# ── AUTO_HINTS: column name → DB field guessing (mirrors importers.py candidates) ──
AUTO_HINTS = {
    # sales
    'order_number':     ['order number', 'bestellnummer', 'замовлення', 'order_number', 'sales order'],
    'sku_raw':          ['artikelnummer', 'part number', 'product number', 'sku', 'pn'],
    'qty':              ['quantity', 'menge', 'кількість', 'qty'],
    'order_date':       ['order date', 'bestelldatum', 'дата замовлення', 'order_date'],
    'status':           ['status', 'статус'],
    'client':           ['customer', 'kunde', 'клієнт', 'client', 'buyer'],
    'email':            ['e-mail', 'email', 'mail'],
    'phone':            ['telefon', 'телефон', 'phone'],
    'source':           ['channel', 'джерело', 'source'],
    'shipped_at':       ['versandt', 'відправлено', 'shipped'],
    'shipping_courier': ['carrier', 'перевізник', 'courier', 'kurier'],
    'tracking_number':  ['trackingnummer', 'трекінг', 'tracking'],
    'shipping_deadline':['дедлайн', 'deadline', 'ship by'],
    'addr_street':      ['straße', 'вулиця', 'street'],
    'addr_city':        ['stadt', 'місто', 'city'],
    'addr_zip':         ['plz', 'postal', 'індекс', 'zip'],
    'addr_country':     ['land', 'країна', 'country'],
    'unit_price':       ['einzelpreis', 'ціна', 'price', 'unit price', 'preis'],
    'currency':         ['währung', 'валюта', 'currency'],
    # products
    'sku':              ['id full', 'artikelnummer', 'part number', 'артикул', 'код', 'sku'],
    'name':             ['bezeichnung', 'назва', 'description', 'artikel', 'name'],
    'category':         ['kategorie', 'категорія', 'category'],
    'manufacturer':     ['hersteller', 'виробник', 'brand', 'manufacturer'],
    'purchase_price':   ['einkauf', 'закупівля', 'purchase', 'cost', 'buy price'],
    'sale_price':       ['verkauf', 'продаж', 'sell price', 'sale'],
    'reorder_point':    ['mindest', 'мін.залишок', 'min stock', 'reorder'],
    'lead_time_days':   ['lieferzeit', 'термін постачання', 'lead time', 'days'],
    'initial_stock':    ['bestand', 'залишок', 'on hand', 'quantity', 'stock', 'кількість'],
    'is_active':        ['aktiv', 'активний', 'active', 'enabled'],
    'hs_code':          ['hscode', 'hs code', 'zolltarifnummer', 'hs'],
    'net_weight_g':     ['gewicht', 'вага', 'weight'],
    'country_of_origin':['herkunft', 'походження', 'origin'],
    # receipt / adjust
    'ref':              ['beleg', 'документ', 'reference', 'document', 'ref'],
    'date':             ['datum', 'дата', 'date'],
    'new_qty':          ['neuer bestand', 'новий залишок', 'new qty', 'target'],
    'delta_qty':        ['зміна', 'change', 'delta', 'diff'],
}


@admin.register(AutoImportProfile)
class AutoImportProfileAdmin(admin.ModelAdmin):
    list_display  = ('name', 'import_type_badge', 'source_type_badge', 'enabled',
                     'interval_minutes', 'last_run_at', 'next_run_at',
                     'last_status_badge', 'action_buttons')
    list_filter   = ('import_type', 'source_type', 'enabled')
    list_editable = ('enabled',)
    search_fields = ('name', 'source_path', 'notes')

    def import_type_badge(self, obj):
        colors = {'sales': '#81c784', 'products': '#64b5f6', 'receipt': '#ffb74d', 'adjust': '#ce93d8'}
        labels = {'sales': '🛒 Sales', 'products': '📦 Products', 'receipt': '📥 Receipt', 'adjust': '🔧 Adjust'}
        c = colors.get(obj.import_type, '#9e9e9e')
        return format_html('<span style="color:{};font-weight:700">{}</span>',
                           c, labels.get(obj.import_type, obj.import_type))
    import_type_badge.short_description = 'Тип'

    def source_type_badge(self, obj):
        return '📁 Папка' if obj.source_type == 'folder' else '🌐 URL'
    source_type_badge.short_description = 'Джерело'

    def last_status_badge(self, obj):
        log = obj.logs.order_by('-ran_at').first()
        if not log:
            return format_html('<span style="color:#607d8b">—</span>')
        icons = {'ok': '✅', 'error': '❌', 'skipped': '⏭', 'dry_run': '🧪'}
        return format_html('<span title="{}">{}</span>',
                           log.ran_at.strftime('%d.%m.%Y %H:%M') if log.ran_at else '',
                           icons.get(log.status, log.status))
    last_status_badge.short_description = 'Статус'

    def action_buttons(self, obj):
        run_url  = reverse('admin:autoimport_profile_run_now', args=[obj.pk])
        dry_url  = reverse('admin:autoimport_profile_run_now', args=[obj.pk]) + '?dry=1'
        edit_url = reverse('admin:autoimport_profile_run', args=[obj.pk])
        s = 'padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;text-decoration:none;margin-right:3px'
        return format_html(
            '<a href="{}" style="background:#0b1f14;border:1px solid #1b4d2e;color:#81c784;' + s + '">▶</a>'
            '<a href="{}" style="background:#1a1200;border:1px solid #4a3600;color:#ffb74d;' + s + '">🧪</a>'
            '<a href="{}" style="background:#0d1520;border:1px solid #1e2d3d;color:#58a6ff;' + s + '">⚙</a>',
            run_url, dry_url, edit_url,
        )
    action_buttons.short_description = 'Дії'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('run/',
                 self.admin_site.admin_view(self._wizard_new),
                 name='autoimport_run'),
            path('<int:pk>/run/',
                 self.admin_site.admin_view(self._wizard_edit),
                 name='autoimport_profile_run'),
            path('<int:pk>/run-now/',
                 self.admin_site.admin_view(self._run_now_view),
                 name='autoimport_profile_run_now'),
        ]
        return custom + urls

    def _wizard_new(self, request):
        return self._wizard_view(request, pk=None)

    def _wizard_edit(self, request, pk):
        return self._wizard_view(request, pk=pk)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        return redirect(reverse('admin:autoimport_profile_run', args=[object_id]))

    def add_view(self, request, form_url='', extra_context=None):
        return redirect(reverse('admin:autoimport_run'))

    # ── Quick run (no wizard) ─────────────────────────────────────────────────

    def _run_now_view(self, request, pk):
        from .runner import run_profile
        profile = get_object_or_404(AutoImportProfile, pk=pk)
        dry = request.GET.get('dry') == '1'
        try:
            logs = run_profile(pk, force=True, dry_run=dry)
            created = sum(l.records_created for l in logs)
            updated = sum(l.records_updated for l in logs)
            errors  = sum(l.errors_count for l in logs)
            label   = '🧪 Dry-run' if dry else '✅ Запущено'
            messages.success(request,
                f'{label} «{profile.name}»: {len(logs)} файл(ів), '
                f'+{created} створено, {updated} оновлено, {errors} помилок')
        except Exception as e:
            messages.error(request, f'❌ Помилка запуску «{profile.name}»: {e}')
        return redirect(reverse('admin:autoimport_autoimportprofile_changelist'))

    # ── Wizard (3 steps) ──────────────────────────────────────────────────────

    def _wizard_view(self, request, pk=None):
        from .runner import _get_excel_sheets, _parse_to_df

        profile = get_object_or_404(AutoImportProfile, pk=pk) if pk else None
        step    = request.POST.get('step') if request.method == 'POST' else None

        ctx_base = dict(
            self.admin_site.each_context(request),
            title='📥 Авто-імпорт',
            import_types=AutoImportProfile.IMPORT_TYPES,
            conflict_choices=AutoImportProfile.CONFLICT_CHOICES,
            profile=profile,
        )

        # ── STEP 1 GET ────────────────────────────────────────────────────────
        if step not in ('1', '2'):
            return render(request, 'admin/autoimport/wizard.html',
                          {**ctx_base, 'step': 1})

        # ── STEP 1 POST: fetch & analyze source ───────────────────────────────
        if step == '1':
            import_type = request.POST.get('import_type', 'sales')
            source_type = request.POST.get('source_type', 'upload')
            folder_path = request.POST.get('folder_path', '').strip()
            file_mask   = request.POST.get('file_mask', '*.xlsx;*.csv').strip() or '*.xlsx;*.csv'
            url_source  = request.POST.get('url_source', '').strip()

            content     = None
            source_name = ''

            try:
                if source_type == 'upload':
                    uploaded = request.FILES.get('upload_file')
                    if not uploaded:
                        messages.error(request, 'Оберіть файл для завантаження.')
                        return render(request, 'admin/autoimport/wizard.html',
                                      {**ctx_base, 'step': 1})
                    content     = uploaded.read()
                    source_name = uploaded.name

                elif source_type == 'url':
                    if not url_source:
                        messages.error(request, 'Введіть URL.')
                        return render(request, 'admin/autoimport/wizard.html',
                                      {**ctx_base, 'step': 1})
                    resp = requests.get(url_source, timeout=30, allow_redirects=True)
                    resp.raise_for_status()
                    content     = resp.content
                    # Guess source_name for CSV vs Excel detection
                    raw_name = url_source.split('?')[0].rstrip('/').split('/')[-1] or 'import'
                    ctype    = resp.headers.get('content-type', '')
                    if 'format=csv' in url_source or 'text/csv' in ctype:
                        source_name = raw_name if raw_name.lower().endswith('.csv') else raw_name + '.csv'
                    elif 'spreadsheetml' in ctype or raw_name.lower().endswith(('.xlsx', '.xls')):
                        source_name = raw_name
                    else:
                        source_name = raw_name + '.csv'

                elif source_type == 'folder':
                    if not folder_path:
                        messages.error(request, 'Введіть шлях до папки.')
                        return render(request, 'admin/autoimport/wizard.html',
                                      {**ctx_base, 'step': 1})
                    import fnmatch
                    masks = [m.strip() for m in file_mask.split(';') if m.strip()]
                    found = None
                    for fname in sorted(os.listdir(folder_path)):
                        fpath = os.path.join(folder_path, fname)
                        if os.path.isfile(fpath) and any(
                            fnmatch.fnmatch(fname.lower(), m.lower()) for m in masks
                        ):
                            found = fpath
                            break
                    if not found:
                        messages.error(request,
                            f'Файлів за маскою «{file_mask}» у «{folder_path}» не знайдено.')
                        return render(request, 'admin/autoimport/wizard.html',
                                      {**ctx_base, 'step': 1})
                    with open(found, 'rb') as f:
                        content = f.read()
                    source_name = os.path.basename(found)

            except requests.RequestException as e:
                messages.error(request, f'Помилка завантаження URL: {e}')
                return render(request, 'admin/autoimport/wizard.html',
                              {**ctx_base, 'step': 1})
            except OSError as e:
                messages.error(request, f'Помилка читання папки: {e}')
                return render(request, 'admin/autoimport/wizard.html',
                              {**ctx_base, 'step': 1})

            # Detect sheets and columns for ALL sheets (for JS sheet-switcher)
            try:
                sheets = _get_excel_sheets(content)
                all_sheets_data = {}
                for s in (sheets if sheets else ['']):
                    try:
                        df_s   = _parse_to_df(content, source_name, sheet_name=s)
                        cols_s = [str(c) for c in df_s.columns]
                        smp_s  = {}
                        for col in cols_s:
                            for val in df_s.head(200)[col]:
                                sv = str(val).strip()
                                if sv.lower() not in ('', 'nan', 'none', 'nat'):
                                    smp_s[col] = sv[:60]
                                    break
                            else:
                                smp_s[col] = ''
                        all_sheets_data[s if s else source_name] = {'columns': cols_s, 'sample': smp_s}
                    except Exception:
                        pass

                req_sheet  = profile.sheet_name if profile else ''
                used_sheet = req_sheet if (req_sheet and req_sheet in sheets) else (sheets[0] if sheets else '')
                key        = used_sheet if used_sheet else source_name
                columns    = all_sheets_data.get(key, {}).get('columns', [])
                sample     = all_sheets_data.get(key, {}).get('sample', {})

            except Exception as e:
                messages.error(request, f'Не вдалося розібрати файл: {e}')
                return render(request, 'admin/autoimport/wizard.html',
                              {**ctx_base, 'step': 1})

            # Store file in session
            request.session['ai_content_b64']     = base64.b64encode(content).decode()
            request.session['ai_source_name']      = source_name
            request.session['ai_all_sheets_json']  = json.dumps(all_sheets_data, ensure_ascii=False)
            request.session['ai_import_type']      = import_type
            request.session['ai_source_type']      = source_type
            request.session['ai_folder_path']      = folder_path
            request.session['ai_file_mask']        = file_mask
            request.session['ai_url_source']       = url_source
            request.session['ai_profile_name']     = request.POST.get('profile_name', '').strip()
            request.session['ai_interval']         = request.POST.get('interval_minutes', '60')
            request.session['ai_conflict']         = request.POST.get('conflict_strategy', 'skip')
            request.session['ai_dry_run']          = '1' if request.POST.get('dry_run') else '0'
            request.session['ai_notify']           = '1' if request.POST.get('notify') else '0'
            request.session['ai_archive']          = '1' if request.POST.get('archive_processed') else '0'

            existing_map = profile.column_map or {} if profile else {}

            return render(request, 'admin/autoimport/wizard.html', {
                **ctx_base,
                'step':              2,
                'source_name':       source_name,
                'sheets':            sheets,
                'used_sheet':        used_sheet,
                'columns':           columns,
                'sample':            sample,
                'import_type':       import_type,
                'db_fields':         IMPORT_FIELDS[import_type],
                'existing_map':      existing_map,
                'dry_run_checked':   request.POST.get('dry_run'),
                'all_sheets_data_json': json.dumps(all_sheets_data, ensure_ascii=False),
                'auto_hints_json':   json.dumps(AUTO_HINTS, ensure_ascii=False),
                'preselected_json':  json.dumps(existing_map, ensure_ascii=False),
            })

        # ── STEP 2 POST: run import ───────────────────────────────────────────
        if step == '2':
            content_b64 = request.session.get('ai_content_b64')
            if not content_b64:
                messages.error(request, 'Сесія застаріла — починайте спочатку.')
                return redirect(reverse('admin:autoimport_run'))

            content     = base64.b64decode(content_b64)
            source_name = request.session.get('ai_source_name', 'import')
            import_type = request.session.get('ai_import_type', 'sales')
            source_type = request.session.get('ai_source_type', 'upload')
            used_sheet  = request.POST.get('sheet_name') or request.session.get('ai_used_sheet', '')
            dry_run     = (request.session.get('ai_dry_run') == '1'
                           or bool(request.POST.get('dry_run')))

            # Build column_map from POST: map_<field_name> → column_name
            column_map = {}
            for key, val in request.POST.items():
                if key.startswith('map_') and val and val != '--':
                    column_map[key[4:]] = val

            # Parse DataFrame
            from .runner import _call_importer, _parse_to_df
            try:
                df = _parse_to_df(content, source_name, sheet_name=used_sheet)
            except Exception as e:
                messages.error(request, f'Помилка парсингу: {e}')
                return redirect(reverse('admin:autoimport_run'))

            # Temporary profile-like namespace for _call_importer
            tmp = types.SimpleNamespace(
                import_type=import_type,
                name=request.session.get('ai_profile_name') or 'wizard',
                column_map=column_map,
                conflict_strategy=request.session.get('ai_conflict', 'skip'),
            )

            t_start = time.time()
            try:
                stats = _call_importer(tmp, df, dry_run)
            except Exception as e:
                messages.error(request, f'Помилка імпорту: {e}')
                return redirect(reverse('admin:autoimport_run'))
            duration_ms = int((time.time() - t_start) * 1000)

            # Save profile if name given and not dry_run
            profile_name = request.session.get('ai_profile_name', '').strip()
            saved_profile = None
            if profile_name:
                folder_path = request.session.get('ai_folder_path', '')
                url_src     = request.session.get('ai_url_source', '')
                src_path    = url_src if source_type == 'url' else folder_path
                interval    = int(request.session.get('ai_interval') or 60)
                notify      = request.session.get('ai_notify') == '1'
                archive     = request.session.get('ai_archive') == '1'
                conflict    = request.session.get('ai_conflict', 'skip')

                save_defaults = dict(
                    import_type=import_type,
                    source_type=source_type,
                    source_path=src_path,
                    file_mask=request.session.get('ai_file_mask', '*.xlsx;*.csv'),
                    sheet_name=used_sheet,
                    column_map=column_map,
                    interval_minutes=interval,
                    conflict_strategy=conflict,
                    notify=notify,
                    archive_processed=archive,
                )
                if pk:
                    saved_profile = get_object_or_404(AutoImportProfile, pk=pk)
                    for attr, val in save_defaults.items():
                        setattr(saved_profile, attr, val)
                    saved_profile.name = profile_name
                    saved_profile.save()
                else:
                    saved_profile, _ = AutoImportProfile.objects.update_or_create(
                        name=profile_name, defaults=save_defaults)

            # Log the run
            errors_list = stats.get('errors', [])
            AutoImportLog.objects.create(
                profile=saved_profile,
                source_name=os.path.basename(source_name),
                file_hash='',
                status=(AutoImportLog.STATUS_DRY_RUN if dry_run else (
                    AutoImportLog.STATUS_ERROR
                    if errors_list and stats.get('created', 0) == 0
                    else AutoImportLog.STATUS_OK)),
                records_created=stats.get('created', 0),
                records_updated=stats.get('updated', 0),
                records_skipped=stats.get('skipped', 0),
                errors_count=len(errors_list),
                error_detail='\n'.join(errors_list[:10]),
                duration_ms=duration_ms,
            )

            return render(request, 'admin/autoimport/wizard.html', {
                **ctx_base,
                'step':          3,
                'source_name':   source_name,
                'stats':         stats,
                'dry_run':       dry_run,
                'duration_ms':   duration_ms,
                'saved_profile': saved_profile,
                'import_type':   import_type,
            })


@admin.register(AutoImportLog)
class AutoImportLogAdmin(admin.ModelAdmin):
    list_display  = ('ran_at', 'profile', 'source_name', 'status_badge',
                     'records_created', 'records_updated', 'records_skipped',
                     'errors_count', 'duration_ms')
    list_filter   = ('status', 'profile')
    date_hierarchy = 'ran_at'
    readonly_fields = ('ran_at', 'profile', 'source_name', 'file_hash',
                       'status', 'records_created', 'records_updated',
                       'records_skipped', 'errors_count', 'error_detail', 'duration_ms')

    def status_badge(self, obj):
        icons = {'ok': '✅', 'error': '❌', 'skipped': '⏭', 'dry_run': '🧪'}
        return format_html('{}', icons.get(obj.status, obj.status))
    status_badge.short_description = 'Статус'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
