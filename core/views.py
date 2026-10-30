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


def custom_403_view(request, exception=None):
    return render(request, 'core/access_denied.html', {
        'title': 'Доступ обмежено',
        'reason': 'У вас немає прав для перегляду цієї сторінки.',
        'module_disabled': False,
        'app_label': '',
    }, status=403)
