"""
views.py для системи етикеток DYMO

Додати в urls.py проекту:
    path('labels/', include('labels.urls')),
"""
import re
from pathlib import Path
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, JsonResponse, Http404
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required


# Папка з етикетками
LABELS_DIR = Path(getattr(settings, 'LABELS_DIR', 
    Path(settings.BASE_DIR) / 'labels'))


def _stem_matches_sku(stem: str, sku: str) -> bool:
    """
    True якщо стем файлу відповідає SKU:
      - точний збіг (case-insensitive)
      - або стем починається з SKU + суфікс через пробіл/підкреслення
        напр. "AN100201-01H Alt" → SKU "AN100201-01H" ✓
              "AN110502-02C_noDGkey" → SKU "AN110502-02C" ✓
    """
    s, k = stem.upper(), sku.upper()
    if s == k:
        return True
    if s.startswith(k) and len(s) > len(k) and s[len(k)] in (' ', '_'):
        return True
    return False


def get_label_path(sku: str) -> Path | None:
    """Знаходить файл етикетки по SKU (точно або з допустимим суфіксом)."""
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    # Спроба 1: точне ім'я (найшвидше)
    exact = LABELS_DIR / f"{sku}.dymo"
    if exact.exists():
        return exact

    # Спроба 2: case-insensitive + fuzzy суфікс
    fuzzy = None
    for f in LABELS_DIR.iterdir():
        if f.suffix.lower() != '.dymo':
            continue
        if f.stem.upper() == sku.upper():
            return f          # точний збіг — одразу повертаємо
        if fuzzy is None and _stem_matches_sku(f.stem, sku):
            fuzzy = f         # fuzzy — запам'ятовуємо першу знахідку
    return fuzzy


def patch_dymo_qty(content: str, qty: int) -> str:
    """Замінює QTY в dymo файлі."""
    # Шукаємо TextSpan з QTY і замінюємо кількість
    def replace_qty(m):
        text = m.group(1)
        # Замінюємо число після "QTY: " або "QTY:"
        patched = re.sub(
            r'(QTY:\s*)(\d+)(\s*PCS?\.?)',
            lambda x: f"{x.group(1)}{qty}{x.group(3)}",
            text,
            flags=re.IGNORECASE
        )
        return f'<Text>{patched}</Text>'
    
    return re.sub(r'<Text>([^<]*QTY[^<]*)</Text>', replace_qty, content, flags=re.IGNORECASE)


@staff_member_required
def serve_label(request, sku):
    """Повертає dymo файл для скачування/відкриття."""
    # qty=0 (default when not provided) → serve raw file without modification
    qty = int(request.GET.get('qty', 0))

    path = get_label_path(sku)
    if not path:
        raise Http404(f"Етикетка для SKU '{sku}' не знайдена")

    with open(path, 'rb') as f:
        raw = f.read()

    if qty > 0:
        content = patch_dymo_qty(raw.decode('utf-8-sig'), qty)
        raw = content.encode('utf-8')

    response = HttpResponse(raw, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{sku}.dymo"'
    return response


def label_kinds(skus) -> dict:
    """{sku: 'antenna' | 'cable' | ''} — чи можна сформувати етикетку з шаблону (інакше лише готовий файл)."""
    from shipping.services.dymo_label_service import parse_part_number
    from inventory.models import Product
    from labels_app import antenna
    by_category = {s.upper() for s in Product.objects.filter(category__icontains='antenn', sku__in=list(skus))
                   .values_list('sku', flat=True)}
    kinds = {}
    for sku in skus:
        kind = ''
        if sku.upper().startswith('CA-'):
            try:
                parse_part_number(sku)
                kind = 'cable'
            except ValueError:
                pass
        elif antenna.is_antenna(sku) or sku.upper() in by_category:
            kind = 'antenna'
        kinds[sku] = kind
    return kinds


def label_source() -> str:
    """Налаштування «Звідки брати етикетку DYMO»: generate — з шаблону, file — готовий файл."""
    try:
        from sales.models import SalesSettings
        return SalesSettings.get().dymo_label_source
    except Exception:
        return 'generate'


@staff_member_required
def label_status(request):
    """JSON: які SKU мають етикетки (файл або шаблон антени / кабелю), які ні."""
    skus = request.GET.get('skus', '').split(',')
    skus = [s.strip() for s in skus if s.strip()]
    kinds = label_kinds(skus)

    result = {}
    for sku in skus:
        path = get_label_path(sku)
        result[sku] = {
            'found': path is not None or bool(kinds[sku]),
            'filename': path.name if path else None,
            'file': path is not None,
            'generate': kinds[sku],
        }
    return JsonResponse(result)


@staff_member_required
@csrf_exempt
def upload_label(request):
    """Завантаження нової або оновленої етикетки на сервер."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    
    files = request.FILES.getlist('labels')
    if not files:
        return JsonResponse({'error': 'Немає файлів'}, status=400)
    
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    
    results = []
    for f in files:
        name = f.name
        if not name.endswith('.dymo'):
            results.append({'name': name, 'status': 'error', 'msg': 'Тільки .dymo файли'})
            continue

        dest = LABELS_DIR / name
        sku  = Path(name).stem

        # Delete any existing label for this SKU that has a different filename
        # (covers fuzzy matches like "AN100-01A Alt.dymo" being replaced by "AN100-01A.dymo")
        old_path = get_label_path(sku)
        replaced_old = None
        if old_path and old_path.resolve() != dest.resolve():
            replaced_old = old_path.name
            old_path.unlink()

        existed = dest.exists()
        with open(dest, 'wb') as out:
            for chunk in f.chunks():
                out.write(chunk)

        results.append({
            'name':         name,
            'sku':          sku,
            'status':       'updated' if (existed or replaced_old) else 'created',
            'size':         f.size,
            'replaced_old': replaced_old,
        })
    
    return JsonResponse({'results': results})


@staff_member_required
@csrf_exempt
def delete_label(request, sku):
    """DELETE (або POST) — видаляє .dymo файл для SKU."""
    if request.method not in ('POST', 'DELETE'):
        return JsonResponse({'error': 'POST/DELETE only'}, status=405)
    path = get_label_path(sku)
    if not path:
        return JsonResponse({'ok': False, 'error': f'Етикетку для {sku} не знайдено'}, status=404)
    filename = path.name
    path.unlink()
    return JsonResponse({'ok': True, 'deleted': filename})


@staff_member_required
def list_labels(request):
    """Список всіх доступних етикеток — HTML-сторінка або JSON (?json=1)."""
    from labels_app import antenna
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    labels = []
    for f in sorted(LABELS_DIR.glob('*.dymo')):
        mtime = f.stat().st_mtime
        sku = f.stem
        base_sku = re.split(r'[ _]', sku.strip(), maxsplit=1)[0]   # «AN100201-01H Alt» → AN100201-01H
        labels.append({
            'sku': sku,
            'filename': f.name,
            'size_kb': round(f.stat().st_size / 1024, 1),
            'modified_fmt': datetime.fromtimestamp(mtime).strftime('%d.%m.%Y %H:%M'),
            'is_cable': sku.upper().startswith('CA-'),
            'antenna_sku': base_sku if antenna.is_antenna(base_sku) else '',
        })
    if request.GET.get('json'):
        return JsonResponse({'labels': labels})
    return render(request, 'labels/list.html', {'labels': labels, 'label_source': label_source()})


# ── Етикетки антен з шаблону (labels_app/antenna.py) ──────────────────────────

def _antenna_args(params):
    """sku, qty, замовлення, Digi-Key P/N (None — за джерелом замовлення), виправлення з вікна."""
    from labels_app import antenna
    from sales.models import SalesOrder
    order = None
    order_id = str(params.get('order') or '').strip()
    if order_id.isdigit():
        order = SalesOrder.objects.filter(pk=int(order_id)).first()
    dk = params.get('dk')
    show_dk = None if dk in (None, '') else str(dk).lower() in ('1', 'true', 'on')
    edits = {f: params.get(f) for f in antenna.EDIT_FIELDS if params.get(f) is not None}
    return (params.get('sku') or '').strip(), params.get('qty') or 1, order, show_dk, edits


def _antenna_json(data, order):
    from urllib.parse import quote, urlencode
    query = {'qty': data['qty'], 'dk': int(data['show_dk'])}
    if order is not None:
        query['order'] = order.pk
    data = dict(data, lines=[[prefix, value] for prefix, value in data['lines']])
    data['url'] = f"/labels/antenna/{quote(data['pn'])}/?{urlencode(query)}"
    if data['file']:
        data['file_url'] = f"/labels/serve/{quote(data['sku'] or data['pn'])}/?qty={data['qty']}"
    return JsonResponse({'ok': True, **data})


@staff_member_required
def antenna_check(request):
    """GET ?sku=&qty=&order=[&dk=&description=&frequency=&connector=] → дані для вікна перевірки."""
    from labels_app import antenna
    sku, qty, order, show_dk, edits = _antenna_args(request.GET)
    if not sku:
        return JsonResponse({'ok': False, 'error': 'sku required'}, status=400)
    return _antenna_json(antenna.build(sku, qty, order=order, show_dk=show_dk, edits=edits), order)


@staff_member_required
@require_POST
def antenna_save(request):
    """POST JSON {sku, qty, order, dk, description, frequency, connector} — людина перевірила етикетку."""
    import json as _json
    from labels_app import antenna
    try:
        payload = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'JSON required'}, status=400)
    sku, qty, order, show_dk, edits = _antenna_args(payload)
    if not sku:
        return JsonResponse({'ok': False, 'error': 'sku required'}, status=400)
    data = antenna.build(sku, qty, order=order, show_dk=show_dk, edits=edits)
    if not data['can_print']:
        return JsonResponse({'ok': False, 'error': next(i['text'] for i in data['issues'] if i['level'] == 'error')},
                            status=400)
    antenna.save(sku, edits, user=request.user)
    return _antenna_json(antenna.build(sku, qty, order=order, show_dk=show_dk), order)


@staff_member_required
def antenna_label(request, sku):
    """Готовий .dymo антени з шаблону з актуальними даними (?qty=&order=&dk=)."""
    from labels_app import antenna
    _sku, qty, order, show_dk, _edits = _antenna_args({**request.GET.dict(), 'sku': sku})
    data = antenna.build(sku, qty, order=order, show_dk=show_dk)
    if not data['can_print']:
        problems = '; '.join(i['text'] for i in data['issues'] if i['level'] == 'error')
        return HttpResponse(f'Етикетку {sku} не сформовано: {problems}', status=400,
                            content_type='text/plain; charset=utf-8')
    response = HttpResponse(antenna.render(data).encode('utf-8'), content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{data["pn"]}.dymo"'
    return response


@staff_member_required
def order_label(request, pk):
    """Етикетка замовлення: номер (текст і штрихкод) і всі артикули з кількістю (labels_app/order_label.py)."""
    from django.shortcuts import get_object_or_404
    from labels_app import order_label as builder
    from sales.models import SalesOrder
    order = get_object_or_404(SalesOrder, pk=pk)
    data = builder.build(order)
    if not data['items']:
        return HttpResponse(f'У замовленні {order.order_number} немає позицій з артикулом.', status=400,
                            content_type='text/plain; charset=utf-8')
    name = re.sub(r'[^\w.-]+', '_', order.order_number) or str(order.pk)
    response = HttpResponse(builder.render(data).encode('utf-8'), content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="Order-{name}.dymo"'
    return response


@staff_member_required
def preview_cable_label(request):
    """GET ?part_no=CA-… → JSON with parsed label fields (for modal preview)."""
    from shipping.services.dymo_label_service import parse_part_number, label_lines
    part_no = (request.GET.get('part_no') or '').strip()
    if not part_no:
        return JsonResponse({'ok': False, 'error': 'part_no required'}, status=400)
    try:
        parsed = parse_part_number(part_no)
        qty = int(request.GET.get('qty') or 1)
        lines = label_lines(parsed, qty)
        return JsonResponse({'ok': True, 'parsed': parsed, 'lines': lines})
    except ValueError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)


@staff_member_required
@csrf_exempt
def generate_cable_label(request):
    """POST {part_no, qty} → generate .dymo, return JSON with download_url."""
    from shipping.services.dymo_label_service import DymoLabelService
    import json as _json

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    try:
        data = _json.loads(request.body or b'{}')
    except Exception:
        data = {}

    part_no = (data.get('part_no') or request.POST.get('part_no') or '').strip()
    try:
        qty = int(data.get('qty') or request.POST.get('qty') or 1)
    except (ValueError, TypeError):
        qty = 1

    if not part_no:
        return JsonResponse({'ok': False, 'error': 'part_no required'}, status=400)

    try:
        path = DymoLabelService.generate(part_no, qty)
        sku  = path.stem
        return JsonResponse({
            'ok':           True,
            'sku':          sku,
            'filename':     path.name,
            'xml':          path.read_text(encoding='utf-8'),
            'download_url': f'/labels/serve/{sku}/?qty={qty}',
        })
    except ValueError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({'ok': False, 'error': f'Generation failed: {exc}'}, status=500)
