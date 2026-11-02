"""core/views.py — Personal settings page + custom error views."""
import json
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe


@staff_member_required
@require_http_methods(['GET', 'POST'])
def my_settings_view(request):
    try:
        profile = request.user.profile
    except Exception:
        return render(request, 'core/my_settings.html', {
            'profile': None,
            'error': 'Профіль не знайдено',
        })

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except (ValueError, Exception):
            return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

        fields_updated = []
        ui_saved = []

        # «Email / Telegram сповіщення» і «Записів на сторінку» прибрано: їх ніщо не читало
        # (особисті повідомлення йдуть на Telegram ID, а що надсилати — у налаштуваннях кожної функції).
        if 'interface_language' in data:
            valid_langs = [c[0] for c in profile._meta.get_field('interface_language').choices]
            if data['interface_language'] in valid_langs:
                profile.interface_language = data['interface_language']
                fields_updated.append('interface_language')
        # тема й доступність — core/ui_prefs.py (лише відомі значення; тема пишеться й у старе поле theme).
        # ui_prefs.save пише сам, під блокуванням рядка, — тут ці поля вдруге не зберігаються
        ui = dict(data.get('ui') or {}) if isinstance(data.get('ui'), dict) else {}
        if 'theme' in data:
            ui['theme'] = data['theme']
        if ui:
            from core import ui_prefs
            ui_saved = ui_prefs.save(profile, ui)
        if 'telegram_id' in data:
            val = data['telegram_id']
            if val == '' or val is None:
                profile.telegram_id = None
            else:
                try:
                    profile.telegram_id = int(val)
                except (ValueError, TypeError):
                    pass
            fields_updated.append('telegram_id')

        if fields_updated:
            profile.save(update_fields=fields_updated)

        return JsonResponse({'ok': True, 'updated': fields_updated + ui_saved})

    # GET
    modules = profile.get_allowed_modules()
    if modules == '__all__':
        modules_display = 'Всі модулі'
    else:
        modules_display = ', '.join(modules) if modules else 'Немає доступу'

    from core import ui_prefs
    ui = ui_prefs.prefs_of(profile)
    context = {
        'profile': profile,
        'modules_display': modules_display,
        'modules_is_all': modules == '__all__',
        'title': 'Мої налаштування',
        'themes': [(k, v) for k, v in ui_prefs.THEMES.items() if k != 'custom'],
        'current_theme': ui['theme'] or profile.theme or 'dark',
        'ui_options': ui_prefs.options_for(ui),
    }
    return render(request, 'core/my_settings.html', context)


@staff_member_required
def set_language_view(request, lang_code):
    """Change interface language for the current user and redirect back."""
    from django.http import HttpResponseRedirect
    from django.utils import translation

    supported = {'uk', 'en', 'de', 'ru'}
    if lang_code not in supported:
        lang_code = 'uk'

    try:
        profile = request.user.profile
        profile.interface_language = lang_code
        profile.save(update_fields=['interface_language'])
    except Exception:
        pass

    translation.activate(lang_code)
    redirect_to = request.META.get('HTTP_REFERER', '/admin/')
    response = HttpResponseRedirect(redirect_to)
    response.set_cookie('django_language', lang_code, max_age=365 * 24 * 3600)
    return response


def ui_dict_view(request, lang):
    """Словник перекладу інтерфейсу для браузера (core/ui_text/catalog.py). З ?v=<версія> — кешується надовго:
    нова версія словника = нова адреса."""
    from django.http import HttpResponse
    from core.ui_text import catalog
    if not (request.user.is_authenticated and request.user.is_staff):
        body, cache = "window.MV_UI_DICT=null;", "no-store"
    else:
        body = catalog.as_js(lang)
        cache = ("private, max-age=31536000, immutable" if request.GET.get("v") == catalog.version()
                 else "private, no-cache")
    response = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    response["Cache-Control"] = cache
    return response


def _panel_admin(request, model):
    """(панель, ModelAdmin) для моделі «app.model», якщо людина бачить її список; інакше 404 / 403."""
    from django.apps import apps
    from django.contrib import admin
    from django.core.exceptions import PermissionDenied
    from django.http import Http404
    from core import side_panel
    panel = side_panel.get(model)
    if panel is None:
        raise Http404
    try:
        model_cls = apps.get_model(model)
    except (LookupError, ValueError):
        raise Http404
    ma = admin.site._registry.get(model_cls)
    opts = model_cls._meta
    # адреса поза /admin/ — ModuleAccessMiddleware її не закриває, тож право моделі за ролями Minerva (модуль,
    # заборонені моделі), як на власних сторінках модулів, плюс права адмінки моделі
    if (ma is None or not request.user.has_perm(f"{opts.app_label}.view_{opts.model_name}")
            or not (ma.has_view_permission(request) or ma.has_change_permission(request))):
        raise PermissionDenied
    return panel, ma


def _panel_object(request, ma, pk):
    """Запис для панелі — з queryset адмінки моделі: ті самі обмеження, що в списку; інакше 404."""
    from django.http import Http404
    obj = ma.get_queryset(request).filter(pk=pk).first()
    if obj is None:
        raise Http404
    return obj


@staff_member_required
@never_cache
@require_safe
def panel_view(request, model, pk):
    """📌 Панель праворуч (core/side_panel.py): оболонка запису — шапка, дії, розділи у вибраному людиною порядку.
    ?open=<розділ> — вміст цього розділу одразу (навіть якщо він вантажиться окремо)."""
    from django.http import HttpResponse
    from core import side_panel
    panel, ma = _panel_admin(request, model)
    obj = _panel_object(request, ma, pk)
    p = side_panel.bind(request, panel, ma)
    open_key = request.GET.get('open') or None
    return HttpResponse(side_panel.render(panel, obj, request, p, open_key=open_key),
                        content_type="text/html; charset=utf-8")


@staff_member_required
@never_cache
@require_safe
def panel_block_view(request, model, pk, key):
    """Один розділ панелі з вмістом: розділи, що вантажаться окремо, згорнуті, «＋ Додати розділ»."""
    from django.http import Http404, HttpResponse
    from core import side_panel
    panel, ma = _panel_admin(request, model)
    if key not in {b.key for b in side_panel.visible_blocks(panel, request)}:
        raise Http404
    obj = _panel_object(request, ma, pk)
    side_panel.bind(request, panel, ma)
    return HttpResponse(side_panel.render_block(panel, key, obj, request), content_type="text/html; charset=utf-8")


@staff_member_required
@require_http_methods(['POST'])
def panel_edit_view(request, model, pk):
    """Швидка зміна поля з панелі (core/panel_edit.py): {key, value, expect} → JSON з оновленою шапкою,
    розділами й клітинками рядка списку; 409 — запис щойно змінив хтось інший."""
    from django.core.exceptions import PermissionDenied
    from django.http import Http404
    from core import panel_edit, side_panel
    try:
        panel, ma = _panel_admin(request, model)
    except PermissionDenied:
        return JsonResponse({'ok': False, 'error': 'forbidden', 'message': 'Немає права змінювати цей запис.'},
                            status=403)
    except Http404:
        return JsonResponse({'ok': False, 'error': 'not_found', 'message': 'Панелі для цього списку немає.'},
                            status=404)
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad_json', 'message': 'Не вдалося прочитати запит.'}, status=400)
    side_panel.bind(request, panel, ma)
    status, payload = panel_edit.apply_edit(request, panel, ma, pk, data)
    return JsonResponse(payload, status=status)


@staff_member_required
@require_http_methods(['POST'])
def panel_prefs_view(request, model):
    """Вибір людини для панелі (правий клік / ⚙): {set}, {shared}, {reset} або старий запис усього одразу (v1)."""
    from core import side_panel
    panel, _ma = _panel_admin(request, model)
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    return JsonResponse({'ok': True, 'prefs': side_panel.save_prefs(request.user, panel, data)})


@staff_member_required
@require_http_methods(['POST'])
def mobile_nav_view(request):
    """📱 Закріплені розділи нижньої панелі телефона (core/mobile_nav.py): {pinned: [...]} або {pinned: null} — як було."""
    from core import mobile_nav
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    return JsonResponse({'ok': True, 'pinned': mobile_nav.save(request.user, data.get('pinned'))})


@staff_member_required
@require_http_methods(['POST'])
def main_menu_view(request):
    """🧭 Меню на ПК (core/main_menu.py): режим, «Мої», сторінка з повного меню, значки, фільтри, «як було»."""
    from core import main_menu
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    before = main_menu.prefs(request.user)
    prefs = main_menu.save(request.user, data)
    pin = data.get('pin_link')
    refused = bool(isinstance(pin, dict) and ('url:' + str(pin.get('href', '')).strip()) not in prefs['pinned']
                   and len(before['pinned']) < main_menu.MAX_PINNED)
    return JsonResponse({'ok': not refused, 'prefs': prefs,
                         'error': 'Цю сторінку не можна закріпити: її немає в Minerva або вона для вас закрита.'
                         if refused else ''})


@staff_member_required
@require_http_methods(['GET'])
def fx_online_view(request):
    """Онлайн-курси ЄЦБ для сторінок (картка товару, «Курси валют»): {ok, date, rates {код: за 1 EUR}, source}.
    Лише читання публічних курсів (`core.fx_online`, кеш 3 год); не вдалося — ok: false, сторінка лишає свої курси."""
    from core import fx_online
    data = fx_online.latest()
    if not data:
        return JsonResponse({'ok': False, 'error': 'Курси ЄЦБ зараз недоступні (мережа або сайт ЄЦБ) — '
                                                   'спробуйте пізніше.'})
    y, m, d = str(data['date'])[:10].split('-')
    return JsonResponse({'ok': True, 'date': data['date'], 'date_text': f'{d}.{m}.{y}', 'rates': data['rates'],
                         'source': data.get('source') or fx_online.SOURCE})


def custom_403_view(request, exception=None):
    return render(request, 'core/access_denied.html', {
        'title': 'Доступ обмежено',
        'reason': 'У вас немає прав для перегляду цієї сторінки.',
        'module_disabled': False,
        'app_label': '',
    }, status=403)
