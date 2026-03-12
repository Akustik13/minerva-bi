"""
views.py для системи етикеток DYMO

Додати в urls.py проекту:
    path('labels/', include('labels.urls')),
"""
import os
import re
import copy
from pathlib import Path
from django.conf import settings
from django.http import HttpResponse, JsonResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
import xml.etree.ElementTree as ET


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
    
    return re.sub(r'<Text>(.*?QTY.*?)</Text>', replace_qty, content, flags=re.DOTALL)


@staff_member_required
def serve_label(request, sku):
    """Повертає dymo файл для скачування/відкриття."""
    qty = int(request.GET.get('qty', 1))
    
    path = get_label_path(sku)
    if not path:
        raise Http404(f"Етикетка для SKU '{sku}' не знайдена")
    
    with open(path, 'rb') as f:
        content = f.read().decode('utf-8-sig')
    
    # Оновлюємо QTY
    if qty > 0:
        content = patch_dymo_qty(content, qty)
    
    response = HttpResponse(
        content.encode('utf-8'),
        content_type='application/octet-stream'
    )
    response['Content-Disposition'] = f'attachment; filename="{sku}.dymo"'
    return response


@staff_member_required  
def label_status(request):
    """JSON: які SKU мають етикетки, які ні."""
    skus = request.GET.get('skus', '').split(',')
    skus = [s.strip() for s in skus if s.strip()]
    
    result = {}
    for sku in skus:
        path = get_label_path(sku)
        result[sku] = {
            'found': path is not None,
            'filename': path.name if path else None,
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
        existed = dest.exists()
        
        with open(dest, 'wb') as out:
            for chunk in f.chunks():
                out.write(chunk)
        
        results.append({
            'name': name,
            'sku': Path(name).stem,
            'status': 'updated' if existed else 'created',
            'size': f.size,
        })
    
    return JsonResponse({'results': results})


@staff_member_required
def list_labels(request):
    """Список всіх доступних етикеток."""
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    
    labels = []
    for f in sorted(LABELS_DIR.glob('*.dymo')):
        labels.append({
            'sku': f.stem,
            'filename': f.name,
            'size_kb': round(f.stat().st_size / 1024, 1),
            'modified': f.stat().st_mtime,
        })
    
    return JsonResponse({'labels': labels, 'count': len(labels)})
