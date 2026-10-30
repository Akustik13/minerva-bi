"""
core/access_editor.py — «✏️ Змінити доступ» на сторінці «👁 Як бачить Minerva»: права людини кліками.

Чернетка (`draft`) — лише відмінності від збереженого: {"modules": {розділ: hidden|view|work|delete},
"extra": {розділ: [export, import]}, "pages": {"app:Model": true — сховати / false — показати},
"special": {delete|export|import|view_audit|manage_users: true|false|null — за роллю}, "reset": true — як у ролі}.
`apply` накладає її на профіль у пам'яті (нічого не пише) — нею ж малюється прев'ю; `save` записує в профіль
(allowed_modules — ручний список, module_operations — явні операції, denied_models, can_*) і в журнал аудиту.
Рівні розділу: hidden — немає в модулях; view — лише перегляд; work — + створює й змінює; delete — + видаляє
(видаляти ще потрібне «🗑 Видаляти записи»). Окрема сторінка — лише показати / сховати (denied_models):
операції Minerva зберігає по розділу. Хто може: адміністратор Minerva або «Може керувати юзерами» (останні —
без надання «Може керувати юзерами»), не собі й не адміністраторам.
"""
from core.utils import ROLE_DEFAULTS, is_minerva_admin, user_can

LEVELS = [("hidden", "🚫", "Сховано"), ("view", "👁", "Лише перегляд"), ("work", "✏️", "Працює"),
          ("delete", "🗑", "+ видаляє")]
LEVEL_KEYS = [k for k, _i, _l in LEVELS]
LEVEL_LABELS = {k: f"{i} {l}" for k, i, l in LEVELS}
LEVEL_OPS = {"view": ["view"], "work": ["view", "add", "change"], "delete": ["view", "add", "change", "delete"]}
EXTRA_OPS = ("export", "import")
ALWAYS = frozenset({"core", "auth", "faq", "admin", "authtoken", "dashboard"})
SPECIAL = ("delete", "export", "import", "view_audit", "manage_users")
SPECIAL_LABELS = {"delete": "🗑 Видаляти записи", "export": "📤 Експорт", "import": "📥 Імпорт",
                  "view_audit": "📜 Журнал аудиту", "manage_users": "👥 Керувати користувачами"}
ADMIN_ROLES = ("superadmin", "admin")


class AccessError(Exception):
    pass


def _modules():
    from core.models import ModuleRegistry
    return list(ModuleRegistry.objects.order_by("order", "app_label"))


def level_of(ops) -> str:
    ops = ["view", "add", "change", "delete", "export", "import"] if ops == "__all__" else list(ops or [])
    if "delete" in ops:
        return "delete"
    if "add" in ops or "change" in ops:
        return "work"
    return "view"


def current(profile) -> dict:
    """Збережені права людини в мові редактора: {modules, extra, pages, special}."""
    allowed = profile.get_allowed_modules()
    modules, extra = {}, {}
    for m in _modules():
        app = m.app_label
        if app in ALWAYS:
            continue
        if allowed != "__all__" and app not in (allowed or []):
            modules[app] = "hidden"
            continue
        ops = profile.get_allowed_operations(app)
        modules[app] = level_of(ops)
        extra[app] = [o for o in EXTRA_OPS if ops == "__all__" or o in (ops or [])]
    return {"modules": modules, "extra": extra, "pages": sorted(profile.denied_models or []),
            "special": {k: getattr(profile, f"can_{k}") for k in SPECIAL}}


def clean(draft) -> dict:
    """Лише відомі ключі й значення — тіло запиту з браузера."""
    draft = draft if isinstance(draft, dict) else {}
    out = {"modules": {}, "extra": {}, "pages": {}, "special": {}, "reset": draft.get("reset") is True}
    for app, lvl in (draft.get("modules") or {}).items():
        if isinstance(app, str) and app not in ALWAYS and lvl in LEVEL_KEYS:
            out["modules"][app] = lvl
    for app, ops in (draft.get("extra") or {}).items():
        if isinstance(app, str) and isinstance(ops, list):
            out["extra"][app] = [o for o in EXTRA_OPS if o in ops]
    for page, hide in (draft.get("pages") or {}).items():
        if isinstance(page, str) and ":" in page and len(page) < 120 and isinstance(hide, bool):
            out["pages"][page] = hide
    for key, value in (draft.get("special") or {}).items():
        if key in SPECIAL and value in (True, False, None):
            out["special"][key] = value
    return out


def apply(profile, draft):
    """Чернетка → профіль у пам'яті (не зберігає). Повертає той самий профіль."""
    draft = clean(draft)
    if draft["reset"]:
        profile.allowed_modules = None
        profile.module_operations = None
        profile.denied_models = []
        for key in SPECIAL:
            setattr(profile, f"can_{key}", None)
    if not (draft["modules"] or draft["extra"] or draft["pages"] or draft["special"]):
        return profile
    base = current(profile)
    if draft["modules"] or draft["extra"]:
        known = {m.app_label for m in _modules()}
        levels = {**base["modules"], **draft["modules"]}
        extra = {**base["extra"], **draft["extra"]}
        old = profile.get_allowed_modules()
        keep = [a for a in (old if isinstance(old, list) else []) if a not in known]   # розділи поза реєстром
        allowed = [a for a, lvl in levels.items() if lvl != "hidden"]
        profile.allowed_modules = sorted(set(allowed) | set(keep) | (ALWAYS & set(old if isinstance(old, list) else [])))
        profile.module_operations = {a: LEVEL_OPS[levels[a]] + [o for o in extra.get(a, []) if o in EXTRA_OPS]
                                     for a in allowed}
    if draft["pages"]:
        pages = set(base["pages"])
        for page, hide in draft["pages"].items():
            (pages.add if hide else pages.discard)(page)
        profile.denied_models = sorted(pages)
    for key, value in draft["special"].items():
        setattr(profile, f"can_{key}", value)
    return profile


def summary(profile, draft) -> list:
    """Що зміниться, людською мовою: [рядок]."""
    from copy import copy
    names = {m.app_label: m.name for m in _modules()}
    before = current(profile)
    after_profile = apply(copy(profile), draft)
    after = current(after_profile)
    lines = []
    if clean(draft)["reset"]:
        lines.append("↺ усе як у ролі / пакеті (ручні модулі, операції, сховані сторінки й особливі права скинуто)")
    for app in sorted(set(before["modules"]) | set(after["modules"]), key=lambda a: names.get(a, a)):
        a, b = before["modules"].get(app), after["modules"].get(app)
        if a != b:
            lines.append(f"{names.get(app, app)}: {LEVEL_LABELS.get(a, '—')} → {LEVEL_LABELS.get(b, '—')}")
        ea, eb = set(before["extra"].get(app, [])), set(after["extra"].get(app, []))
        if b != "hidden" and ea != eb:
            add, rem = sorted(eb - ea), sorted(ea - eb)
            lines.append(f"{names.get(app, app)}: " + ", ".join([f"+ {x}" for x in add] + [f"− {x}" for x in rem]))
    hidden, shown = set(after["pages"]) - set(before["pages"]), set(before["pages"]) - set(after["pages"])
    from core.access_preview import page_label
    lines += [f"🚫 сховати сторінку «{page_label(p)}»" for p in sorted(hidden, key=page_label)]
    lines += [f"👁 показати сторінку «{page_label(p)}»" for p in sorted(shown, key=page_label)]
    for key in SPECIAL:
        a, b = before["special"][key], after["special"][key]
        if a != b:
            fmt = {True: "так", False: "ні", None: "за роллю"}
            lines.append(f"{SPECIAL_LABELS[key]}: {fmt[a]} → {fmt[b]}")
    return lines


def can_edit(editor, profile) -> str:
    """'' — можна редагувати, інакше чому ні."""
    if not (is_minerva_admin(editor) or user_can(editor, "manage_users")):
        return "Змінювати доступ може адміністратор або той, хто має «Може керувати юзерами»."
    if profile.user_id == editor.pk:
        return "Свої права змінювати не можна — попросіть іншого адміністратора."
    if profile.user.is_superuser or profile.role in ADMIN_ROLES:
        return (f"{profile.user.get_username()} — адміністратор і бачить усе; щоб обмежити, спершу змініть роль у "
                "профілі.")
    if not is_minerva_admin(editor) and (profile.can_manage_users or ROLE_DEFAULTS.get(profile.role, {}).get(
            "can_manage_users")):
        return "Права іншого керівника користувачів змінює лише адміністратор."
    return ""


def save(profile, draft, editor, ip=None) -> list:
    """Записати чернетку в профіль і журнал аудиту. Повертає перелік змін (порожній — нічого не змінено)."""
    from core.models import AuditLog
    why = can_edit(editor, profile)
    if why:
        raise AccessError(why)
    draft = clean(draft)
    if draft["special"].get("manage_users") and not is_minerva_admin(editor):
        raise AccessError("Надати «Керувати користувачами» може лише адміністратор.")
    lines = summary(profile, draft)
    if not lines:
        return []
    before = current(profile)
    apply(profile, draft)
    profile.save(update_fields=["allowed_modules", "module_operations", "denied_models",
                                "can_delete", "can_export", "can_import", "can_view_audit", "can_manage_users"])
    AuditLog.objects.create(user=editor, action=AuditLog.Action.SETTINGS, object_id=str(profile.user_id),
                            object_repr=f"Доступ: {profile.user.get_username()}"[:500], ip_address=ip,
                            extra={"access_changes": lines, "before": before, "after": current(profile)})
    return lines


def draft_from(source_profile) -> dict:
    """«Скопіювати права з …»: повні права іншої людини як чернетка."""
    cur = current(source_profile)
    return {"modules": cur["modules"], "extra": cur["extra"],
            "pages": {p: True for p in cur["pages"]}, "special": cur["special"], "reset": True}
