"""
core/access_preview.py — «👁 Як бачить Minerva <людина>»: доступи користувача без входу від його імені.

Лише читання: меню адмінки (`admin.site.get_app_list` для цієї людини — ті самі правила, що в її меню), що можна
з кожною моделлю (view / add / change / delete — ModelAdmin.has_*_permission, тобто роль, пакет, ручні модулі,
заборонені моделі, операції), модулі й звідки доступ, особливі права (user_can), права на базу знань і RAG
(rag_assistant.access), сторінки поза адмінкою, перевірка будь-якої адреси (`check_url`: шари
ModuleAccessMiddleware + права моделі). Даних людини (пошта, розмови, приватні документи) сторінка не показує —
у цьому відмінність від «увійти як»: нічого не відкривається й нічого не виконується від її імені.
"""
from django.contrib import admin
from django.test import RequestFactory
from django.urls import Resolver404, resolve, reverse

from core.utils import OP_LABELS, is_minerva_admin, user_can

ACTIONS = ("view", "add", "change", "delete")
ACTION_ICONS = {"view": "👁", "add": "➕", "change": "✏️", "delete": "🗑"}
SPECIAL = [("delete", "🗑 Видаляти записи"), ("export", "📤 Експорт"), ("import", "📥 Імпорт"),
           ("view_audit", "📜 Журнал аудиту"), ("manage_users", "👥 Керувати користувачами")]
# Сторінки поза адмінкою: (назва, адреса, розділ — модуль, від якого залежить доступ; None — усім співробітникам)
PAGES = [
    ("📊 Дашборд", "/dashboard/", "dashboard"),
    ("✉️ Пошта", "/email/", "email_assistant"),
    ("📅 Календар", "/calendar/", "calendar_app"),
    ("📝 Angebote", "/offers/", "offers"),
    ("📦 Packing Lists", "/packing-lists/", "shipping"),
    ("🧾 Commercial Invoices", "/commercial-invoices/", "shipping"),
    ("🏷️ Етикетки DYMO", "/labels/", "labels_app"),
    ("📄 Документи", "/documents/", "documents"),
    ("🏛️ Minerva AI", "/ai/", "ai_assistant"),
    ("📚 Гід по Minerva", "/guide/", None),
]
ALWAYS = frozenset({"core", "auth", "faq", "admin", "authtoken", "dashboard"})


def _fresh(user):
    """Свіжий об'єкт людини з бази — кеші прав і тимчасові прапорці не перетікають у справжню сесію."""
    from django.contrib.auth.models import User
    return User.objects.select_related("profile").get(pk=user.pk)


def _request(user, path="/admin/"):
    request = RequestFactory().get(path)
    request.user = user
    request.session = {}
    return request


def _profile(user):
    try:
        return user.profile
    except Exception:  # noqa: BLE001
        return None


def _source(profile) -> str:
    if profile is None:
        return "немає профілю Minerva"
    if profile.role in ("superadmin", "admin"):
        return f"роль «{profile.get_role_display()}» — усі модулі"
    if profile.allowed_modules is not None:
        return "ручний список модулів у профілі"
    if profile.bundle_id:
        return f"пакет «{profile.bundle}»"
    return f"роль «{profile.get_role_display()}»"


def module_rows(user) -> list:
    """[{app, name, active, allowed, why, ops: [мітки]}] — кожен модуль реєстру: чи відкритий людині й що в ньому."""
    from core.middleware import hook_allows
    from core.models import ModuleRegistry
    from core.permissions import module_allowed
    profile = _profile(user)
    allowed = profile.get_allowed_modules() if profile else []
    rows = []
    for m in ModuleRegistry.objects.order_by("order", "app_label"):
        in_role = allowed == "__all__" or m.app_label in (allowed or [])
        hooked = not in_role and hook_allows(user, m.app_label)
        ok = m.is_active and (m.app_label in ALWAYS or module_allowed(user, m.app_label))
        if not m.is_active:
            why = "модуль вимкнено для всіх («Модулі»)"
        elif in_role or m.app_label in ALWAYS:
            why = _source(profile) if m.app_label not in ALWAYS else "доступний усім співробітникам"
        elif hooked:
            why = "відкрито правами цього модуля (напр. права на базу знань)"
        else:
            why = f"немає в доступі ({_source(profile)})"
        ops = []
        if ok and profile is not None:
            got = profile.get_allowed_operations(m.app_label)
            ops = [OP_LABELS.get(o, o) for o in (["view", "add", "change", "delete", "export", "import"]
                                                 if got == "__all__" else got or [])]
        rows.append({"app": m.app_label, "name": m.name, "active": m.is_active, "allowed": ok, "why": why,
                     "ops": ops})
    return rows


def menu(user) -> list:
    """Меню адмінки, як його бачить людина: [{name, app, models: [{name, url, perms: {дія: bool}}]}]."""
    request = _request(user)
    out = []
    for app in admin.site.get_app_list(request):
        models = []
        for m in app["models"]:
            model_admin = admin.site._registry.get(m.get("model"))
            perms = {}
            for action in ACTIONS:
                try:
                    perms[action] = bool(getattr(model_admin, f"has_{action}_permission")(request))
                except Exception:  # noqa: BLE001
                    perms[action] = False
            models.append({"name": str(m["name"]), "url": m.get("admin_url") or "", "perms": perms,
                           "read_only": perms["view"] and not (perms["add"] or perms["change"] or perms["delete"])})
        if models:
            out.append({"name": str(app["name"]), "app": app["app_label"], "models": models})
    return out


def knowledge_rights(user) -> list:
    try:
        from rag_assistant import access
    except ImportError:
        return []
    rights = access.rights_of(user)
    source = {"admin": "адміністратор — усе", "override": "надано людині окремо", "role": "за роллю"}
    return [{"key": key, "label": label, "help": help_text, "value": rights[key]["value"],
             "source": source.get(rights[key]["source"], rights[key]["source"])}
            for key, label, help_text in access.RIGHTS]


def pages(user) -> list:
    from core.permissions import module_allowed
    out = []
    for label, url, app in PAGES:
        ok = user.is_active and user.is_staff and (app is None or app in ALWAYS or is_minerva_admin(user)
                                                   or module_allowed(user, app))
        out.append({"label": label, "url": url, "allowed": ok})
    return out


def preview(user) -> dict:
    """Усе для сторінки «👁 Як бачить Minerva»."""
    user = _fresh(user)
    profile = _profile(user)
    can_login = user.is_active and user.is_staff
    data = {
        "user": user, "profile": profile, "can_login": can_login, "admin": is_minerva_admin(user),
        "superuser": user.is_superuser, "source": _source(profile),
        "special": [{"label": label, "value": user_can(user, key)} for key, label in SPECIAL],
    }
    if not can_login:
        data.update(menu=[], modules=[], knowledge=[], pages=[])
        return data
    data.update(menu=menu(user), modules=module_rows(user), knowledge=knowledge_rights(user), pages=pages(user))
    data["models_count"] = sum(len(a["models"]) for a in data["menu"])
    data["edit_count"] = sum(1 for a in data["menu"] for m in a["models"] if not m["read_only"])
    return data


def check_url(user, url: str) -> dict:
    """Чи відкриє людина адресу Minerva і що зможе там зробити: {ok, level, text, steps: [(крок, ✓/✗, пояснення)]}."""
    from urllib.parse import urlparse
    user = _fresh(user)
    path = urlparse((url or "").strip()).path or "/"
    if not path.startswith("/"):
        path = "/" + path
    steps = []
    if not (user.is_active and user.is_staff):
        return {"ok": False, "level": "no", "path": path, "text": "Не може увійти в Minerva (неактивний або не співробітник).",
                "steps": steps}
    try:
        match = resolve(path)
    except Resolver404:
        return {"ok": False, "level": "no", "path": path, "text": "Такої адреси в Minerva немає.", "steps": steps}
    if path.startswith("/admin/"):
        from core.middleware import ModuleAccessMiddleware
        blocked, ctx = ModuleAccessMiddleware(lambda r: None)._check_access(_request(user, path))
        steps.append(("Розділ (модулі, заборонені моделі, операції)", not blocked,
                      (ctx.get("reason") or "модуль вимкнено для всіх") if blocked else "відкритий"))
        if blocked:
            return {"ok": False, "level": "no", "path": path, "text": "Сторінку буде закрито: " +
                    (ctx.get("reason") or "модуль вимкнено"), "steps": steps}
        model_admin = getattr(match.func, "model_admin", None)
        name = match.url_name or ""
        if model_admin is None:
            for registered in admin.site._registry.values():
                opts = registered.model._meta
                if name.startswith(f"{opts.app_label}_{opts.model_name}_"):
                    model_admin = registered
                    break
        if model_admin is not None:
            request = _request(user, path)
            perms = {a: bool(getattr(model_admin, f"has_{a}_permission")(request)) for a in ACTIONS}
            want = "add" if name.endswith("_add") else "delete" if name.endswith("_delete") else \
                "change" if name.endswith("_change") else "view"
            steps.append(("Права моделі «%s»" % model_admin.model._meta.verbose_name_plural,
                          perms["view"] or perms["change"],
                          ", ".join(f"{ACTION_ICONS[a]} {OP_LABELS.get(a, a)}" for a in ACTIONS if perms[a]) or "жодних"))
            if want in ("add", "delete") and not perms[want]:
                return {"ok": False, "level": "no", "path": path, "steps": steps,
                        "text": f"Відкриє розділ, але «{OP_LABELS.get(want, want)}» заборонено."}
            if not (perms["view"] or perms["change"]):
                return {"ok": False, "level": "no", "path": path, "steps": steps, "text": "Сторінку буде закрито (немає права перегляду)."}
            edit = perms["change"] or (want == "add" and perms["add"])
            return {"ok": True, "level": "edit" if edit else "view", "path": path, "steps": steps,
                    "text": "Відкриє й зможе змінювати." if edit else "Відкриє лише для перегляду (без змін)."}
        return {"ok": True, "level": "view", "path": path, "steps": steps,
                "text": "Відкриє; дії на цій сторінці перевіряє сама сторінка (власні права модуля)."}
    first = path.strip("/").split("/")[0]
    page = next((p for p in pages(user) if p["url"].strip("/") == first), None)
    if page is not None:
        steps.append((f"Розділ «{page['label']}»", page["allowed"], "відкритий" if page["allowed"] else "немає доступу"))
        return {"ok": page["allowed"], "level": "view" if page["allowed"] else "no", "path": path, "steps": steps,
                "text": ("Розділ відкритий; що саме можна на сторінці, перевіряє сама сторінка."
                         if page["allowed"] else "Розділ закритий для цієї людини.")}
    return {"ok": True, "level": "view", "path": path, "steps": steps,
            "text": "Сторінка поза модулями Minerva — перевіряє її сама (вхід співробітника)."}


def preview_url(profile) -> str:
    return reverse("admin:core_userprofile_access", args=[profile.pk])
