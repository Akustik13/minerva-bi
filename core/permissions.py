"""
core/permissions.py — права Django (user.has_perm("app.дія_модель")) за моделлю доступу Minerva.

Minerva не призначає людям стандартних прав Django: доступ — роль / пакет / ручний список модулів (+ хуки модулів,
settings.MODULE_ACCESS_HOOKS), заблоковані моделі, операції по модулях (core/utils.py ROLE_OPERATIONS). Без цього
has_perm(...) у власних сторінках модулів (Angebote, проєкти, «🧭 Огляд» у картках, документи бази знань) був би
False для всіх, крім суперкористувача. Правило (core.auth_backend.EmailOrUsernameBackend.has_perm):
  • лише активний співробітник; ролі суперадмін / адміністратор — усе;
  • core / auth (користувачі, ролі, журнал аудиту) — лише стандартними правами Django;
  • модуль вимкнено в реєстрі або його немає в доступних людині — ні;
  • заблокована модель (denied_models) — ні;
  • view — є доступ до модуля; add / change / delete — операція в module_operations / ROLE_OPERATIONS;
    delete — ще й «Може видаляти» (user_can(user, 'delete')).
Інші (власні) права моделей не відповідають — лише стандартні Django.
"""

ACTIONS = ("view", "add", "change", "delete")
ADMIN_ROLES = ("superadmin", "admin")
PROTECTED_APPS = frozenset({"auth", "core", "admin", "contenttypes", "sessions"})


def _cache(user) -> dict:
    cache = getattr(user, "_minerva_perm_cache", None)
    if cache is None:
        cache = {}
        try:
            user._minerva_perm_cache = cache
        except Exception:  # noqa: BLE001
            pass
    return cache


def module_allowed(user, app_label: str) -> bool:
    """Розділ доступний людині: увімкнений у реєстрі, є в модулях ролі / пакета / ручних або відкритий хуком."""
    cache = _cache(user)
    key = ("module", app_label)
    if key not in cache:
        cache[key] = _module_allowed(user, app_label)
    return cache[key]


def _module_allowed(user, app_label):
    from core.middleware import hook_allows
    from core.models import ModuleRegistry
    try:
        profile = user.profile
    except Exception:  # noqa: BLE001 — без профілю: не вирішуємо
        return False
    if not ModuleRegistry.check_active(app_label):
        return False
    allowed = profile.get_allowed_modules()
    return allowed == "__all__" or app_label in allowed or hook_allows(user, app_label)


def has_perm(user, perm: str) -> bool:
    """Чи дає модель доступу Minerva стандартне право Django perm («offers.view_offer»)."""
    if not (user and getattr(user, "is_authenticated", False) and user.is_active and user.is_staff):
        return False
    app_label, _, codename = (perm or "").partition(".")
    action, _, model = codename.partition("_")
    if not app_label or action not in ACTIONS or not model:
        return False
    cache = _cache(user)
    if perm not in cache:
        cache[perm] = _has_perm(user, app_label, action, model)
    return cache[perm]


def _has_perm(user, app_label, action, model):
    try:
        profile = user.profile
    except Exception:  # noqa: BLE001
        return False
    if profile.role in ADMIN_ROLES:
        return True
    if app_label in PROTECTED_APPS or not module_allowed(user, app_label):
        return False
    if f"{app_label}:{model}" in {str(d).lower() for d in (profile.denied_models or [])}:
        return False
    if action == "view":
        return True
    ops = profile.get_allowed_operations(app_label)
    if ops != "__all__" and action not in (ops or []):
        return False
    if action == "delete":
        from core.utils import user_can
        return user_can(user, "delete")
    return True


def has_module_perms(user, app_label: str) -> bool:
    if not (user and getattr(user, "is_authenticated", False) and user.is_active and user.is_staff):
        return False
    try:
        if user.profile.role in ADMIN_ROLES:
            return True
    except Exception:  # noqa: BLE001
        return False
    return app_label not in PROTECTED_APPS and module_allowed(user, app_label)
