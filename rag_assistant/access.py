"""
🔐 Права на базу знань і RAG: хто може користуватись помічником «Minerva» (📚 гід / 📦 даташити), пошуком у базі
знань, додавати документи, керувати джерелами й налаштуваннями.

Правило: суперкористувач і ролі суперадмін / адміністратор — усе; інакше ручне перевизначення людини
(KnowledgeAccess.rights {право: true | false}), а без нього — за роллю (ROLE_DEFAULTS). Пошук RAG і так
повертає лише дані, які людина бачить (власні скриньки, документи за видимістю) — право вирішує, чи можна
шукати взагалі. Надавати права — суперкористувач або той, хто «Може керувати юзерами» (can_grant);
кожна зміна — у журналі аудиту.
"""
from django.contrib.auth import get_user_model

RIGHTS = [
    ("guide", "📚 Гід у віконці «Minerva»",
     "Відповіді віконця «Minerva» про роботу в системі за гідом (AI — рахується в бюджет AI)."),
    ("products", "📦 Помічник по продукції",
     "Відповіді віконця «Minerva» про товари за даташитами й каталогом (RAG по продукції)."),
    ("search", "🔎 Пошук у базі знань",
     "RAG у пошті («🔎 Доповнити через RAG», «📋 Підготувати відповідь», «❓ Запитати»), у картках («❓ Запитати», "
     "«🧭 Огляд») і в огляді проєкту — лише серед даних, які людина й так бачить."),
    ("documents", "📎 Документи й посилання",
     "Додавати файли й посилання в базу знань у картках клієнта, проєкту, замовлення й товару, змінювати свої."),
    ("sources", "📂 Джерела знань",
     "Створювати й змінювати джерела знань: папки, файли з ПК, сайти."),
    ("manage", "🧠 Керування базою знань",
     "Налаштування бази знань і помічника по продукції, ключі, переіндексація, пауза, 📊 огляд бази знань, "
     "розмови помічника."),
]
RIGHT_KEYS = [key for key, _label, _help in RIGHTS]
LABELS = {key: label for key, label, _help in RIGHTS}

ADMIN_ROLES = ("superadmin", "admin")
ROLE_DEFAULTS = {
    "manager": {"guide", "products", "search", "documents"},
    "warehouse": {"guide", "products"},
    "accountant": {"guide"},
    "ai": {"guide", "products", "search"},
    "readonly": {"guide"},
}


def _role(user) -> str:
    try:
        return user.profile.role
    except Exception:  # noqa: BLE001 — без профілю: як менеджер (так створює core.signals)
        return "manager"


def _is_admin(user) -> bool:
    return bool(user.is_superuser or _role(user) in ADMIN_ROLES)


def overrides(user) -> dict:
    """Ручні перевизначення людини (кеш на об'єкті користувача в межах запиту)."""
    cached = getattr(user, "_knowledge_rights", None)
    if cached is None:
        from .models import KnowledgeAccess
        row = KnowledgeAccess.objects.filter(user_id=user.pk).values_list("rights", flat=True).first()
        cached = {k: bool(v) for k, v in (row or {}).items() if k in RIGHT_KEYS and v is not None}
        try:
            user._knowledge_rights = cached
        except Exception:  # noqa: BLE001
            pass
    return cached


def role_default(role: str, right: str) -> bool:
    return role in ADMIN_ROLES or right in ROLE_DEFAULTS.get(role, set())


def has(user, right: str) -> bool:
    """Чи має людина право на базу знань / RAG."""
    if right not in RIGHT_KEYS:
        raise ValueError(f"невідоме право бази знань: {right}")
    if not (user and getattr(user, "is_authenticated", False) and user.is_active and user.is_staff):
        return False
    if _is_admin(user):
        return True
    own = overrides(user)
    if right in own:
        return own[right]
    return role_default(_role(user), right)


def rights_of(user) -> dict:
    """{право: {"value": bool, "source": "admin" | "override" | "role", "role_default": bool}} — для сторінки прав."""
    role, admin, own = _role(user), _is_admin(user), overrides(user)
    out = {}
    for key in RIGHT_KEYS:
        default = role_default(role, key)
        if admin:
            out[key] = {"value": True, "source": "admin", "role_default": True}
        elif key in own:
            out[key] = {"value": own[key], "source": "override", "role_default": default}
        else:
            out[key] = {"value": default, "source": "role", "role_default": default}
    return out


def can_grant(user) -> bool:
    """Надавати права на базу знань: суперкористувач або «Може керувати юзерами»."""
    if not (user and getattr(user, "is_authenticated", False) and user.is_active):
        return False
    if user.is_superuser:
        return True
    from core.utils import user_can
    return user_can(user, "manage_users")


def set_rights(target, changes: dict, by_user, ip=None) -> dict:
    """changes {право: True | False | None (за роллю)} → збережені перевизначення; запис у журнал аудиту."""
    from core.models import AuditLog
    from .models import KnowledgeAccess
    row, _ = KnowledgeAccess.objects.get_or_create(user=target)
    before = {k: bool(v) for k, v in (row.rights or {}).items() if k in RIGHT_KEYS}
    after = dict(before)
    for key, value in changes.items():
        if key not in RIGHT_KEYS:
            continue
        if value is None:
            after.pop(key, None)
        else:
            after[key] = bool(value)
    if after != before:
        row.rights, row.updated_by = after, by_user
        row.save(update_fields=["rights", "updated_by", "updated_at"])
        diff = {k: {"було": before.get(k, "за роллю"), "стало": after.get(k, "за роллю")}
                for k in RIGHT_KEYS if before.get(k) != after.get(k)}
        AuditLog.objects.create(user=by_user, action=AuditLog.Action.SETTINGS, object_id=str(target.pk),
                                object_repr=f"Права бази знань: {target.get_username()}"[:500], ip_address=ip,
                                extra={"knowledge_rights": diff})
    try:
        del target._knowledge_rights
    except AttributeError:
        pass
    return after


def staff_users():
    return get_user_model().objects.filter(is_active=True, is_staff=True).select_related("profile").order_by(
        "username")


def module_access(user, app_label: str):
    """Хук ModuleAccessMiddleware (settings.MODULE_ACCESS_HOOKS): сторінки бази знань в адмінці відкриваються тим,
    хто має право керувати / джерелами / документами, навіть без модуля rag_assistant у ролі. None — не вирішує."""
    if app_label == "rag_assistant" and (any(has(user, r) for r in ("manage", "sources", "documents"))
                                         or can_grant(user)):
        return True
    return None
