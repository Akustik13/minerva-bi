"""core/views.py — Personal settings page + custom error views."""
import json
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods


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

        # «Email / Telegram сповіщення» і «Записів на сторінку» прибрано: їх ніщо не читало
        # (особисті повідомлення йдуть на Telegram ID, а що надсилати — у налаштуваннях кожної функції).
        if 'interface_language' in data:
            valid_langs = [c[0] for c in profile._meta.get_field('interface_language').choices]
            if data['interface_language'] in valid_langs:
                profile.interface_language = data['interface_language']
                fields_updated.append('interface_language')
        # тема й доступність — core/ui_prefs.py (лише відомі значення; тема пишеться й у старе поле theme)
        ui = dict(data.get('ui') or {}) if isinstance(data.get('ui'), dict) else {}
        if 'theme' in data:
            ui['theme'] = data['theme']
        if ui:
            from core import ui_prefs
            fields_updated += ui_prefs.save(profile, ui)
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

        return JsonResponse({'ok': True, 'updated': fields_updated})

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


@staff_member_required
def panel_view(request, model, pk):
    """📌 Панель праворуч (core/side_panel.py): HTML вибраних людиною блоків запису."""
    from django.http import Http404, HttpResponse
    from core import side_panel
    panel, ma = _panel_admin(request, model)
    obj = ma.get_queryset(request).filter(pk=pk).first()         # ті самі обмеження, що в списку
    if obj is None:
        raise Http404
    return HttpResponse(side_panel.render(panel, obj, request), content_type="text/html; charset=utf-8")


@staff_member_required
@require_http_methods(['POST'])
def panel_prefs_view(request, model):
    """Вибір людини для панелі: блоки, порядок, швидкі дії, ширина, увімкнено (правий клік / ⚙ на панелі)."""
    from core import side_panel
    panel, _ma = _panel_admin(request, model)
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    return JsonResponse({'ok': True, 'prefs': side_panel.save_prefs(request.user, panel, data)})


def custom_403_view(request, exception=None):
    return render(request, 'core/access_denied.html', {
        'title': 'Доступ обмежено',
        'reason': 'У вас немає прав для перегляду цієї сторінки.',
        'module_disabled': False,
        'app_label': '',
    }, status=403)
