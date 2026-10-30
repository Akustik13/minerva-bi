"""core/context_processors.py — Inject user role + allowed modules into templates."""
import json
from django.conf import settings as _settings


def user_modules(request):
    """
    Returns:
        allowed_modules_json: JSON string — list of app_labels or the string '__all__'
        user_role: role string ('admin', 'manager', etc.)
    """
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return {'allowed_modules_json': '"__all__"', 'user_role': ''}
    try:
        from core.middleware import with_hooks
        profile = request.user.profile
        modules = with_hooks(request.user, profile.get_allowed_modules())
        denied  = profile.denied_models or []
        return {
            'allowed_modules_json': json.dumps(modules),
            'denied_models_json':   json.dumps(denied),
            'user_role': profile.role,
        }
    except Exception:
        return {'allowed_modules_json': '"__all__"', 'denied_models_json': '[]', 'user_role': ''}


def ui_context(request):
    """Тема й доступність людини (`core.ui_prefs`) — для скрипта в <head> base_site.html (без «блимання»)."""
    from core import ui_prefs
    profile = None
    if hasattr(request, 'user') and request.user.is_authenticated:
        try:
            profile = request.user.profile
        except Exception:
            profile = None
    out = {}
    try:
        out['MV_UI_JSON'] = ui_prefs.page_config(profile)
    except Exception:
        out['MV_UI_JSON'] = ui_prefs.page_config(None)
    # 📱 нижня панель телефона (core/mobile_nav.py) і 🧭 меню ПК у новому вигляді (core/main_menu.py):
    # розділи, до яких людина має доступ, і закріплене нею
    if profile is not None:
        try:
            from core import main_menu, mobile_nav
            allowed = mobile_nav.available(request.user)
            out['MV_MOBILE_JSON'] = mobile_nav.page_config(request.user, allowed)
            out['MV_MENU_JSON'] = main_menu.page_config(request.user, allowed)
        except Exception:
            pass
    # переклад інтерфейсу іншою мовою (core/ui_text): версія словника, якщо для мови вже є переклади
    if profile is not None:
        try:
            from core.ui_text import catalog
            lang = (getattr(request, 'LANGUAGE_CODE', '') or '')[:2]
            info = catalog.page_info(lang)
            if info:
                out['MV_UI_DICT'] = {'lang': lang, 'ver': info['ver']}
        except Exception:
            pass
    return out


def language_context(request):
    """Inject LANGUAGES list and CURRENT_LANGUAGE into all templates."""
    lang = getattr(request, 'LANGUAGE_CODE', 'uk')[:2]
    return {
        'LANGUAGES':        getattr(_settings, 'LANGUAGES', []),
        'CURRENT_LANGUAGE': lang,
    }
