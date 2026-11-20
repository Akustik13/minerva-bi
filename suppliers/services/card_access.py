"""
suppliers/services/card_access.py — 👁 хто бачить візитку / флаєр і хто її зняв звідки.

  visible(qs, user)        — лише ті, які людина бачить: 👥 «бачать усі» + свої 🔒 «лише я» (приватну чужу не бачить
                             ніхто, і адміністратор теж — як «🔒 лише я» в базі знань);
  can_see(user, m)         — те саме для однієї;
  can_set_visibility(user, m) — змінити «хто бачить» може лише той, хто зняв;
  clean(value)             — «team» / «private» (інше — «team»);
  role_label(user)         — роль у Minerva («🔑 Адміністратор», суперкористувач — «👑 Суперадмін»);
  who(user)                — «Viacheslav (🔑 Адміністратор)»;
  source_label(m)          — «📱 Samsung SM-S918B» / «🌐 Сторінка «📷 Візитка / флаєр»» / «🏭 Картка постачальника».
"""
from django.db.models import Q


def clean(value) -> str:
    from suppliers.models import SupplierMedia
    return SupplierMedia.PRIVATE if str(value or "") == SupplierMedia.PRIVATE else SupplierMedia.TEAM


def visible(qs, user):
    from suppliers.models import SupplierMedia
    if user is None or not getattr(user, "pk", None):
        return qs.filter(visibility=SupplierMedia.TEAM)
    return qs.filter(Q(visibility=SupplierMedia.TEAM) | Q(created_by=user))


def can_see(user, m) -> bool:
    from suppliers.models import SupplierMedia
    return m.visibility != SupplierMedia.PRIVATE or (user is not None and m.created_by_id == user.pk)


def can_set_visibility(user, m) -> bool:
    return user is not None and m.created_by_id is not None and m.created_by_id == user.pk


def role_label(user) -> str:
    if user is None:
        return ""
    try:
        role = user.profile.get_role_display()
    except Exception:  # noqa: BLE001 — без профілю
        role = ""
    if not role and getattr(user, "is_superuser", False):
        role = "👑 Суперадмін"
    return role


def person(user) -> str:
    return (user.get_full_name() or user.username) if user is not None else ""


def who(user) -> str:
    if user is None:
        return ""
    role = role_label(user)
    return person(user) + (f" ({role})" if role else "")


def source_label(m) -> str:
    from suppliers.models import SupplierMedia
    if m.source == SupplierMedia.S_APP:
        name = m.device.name if getattr(m, "device_id", None) and m.device else ""
        return "📱 " + (name or "Додаток на телефоні")
    return dict(SupplierMedia.SOURCES).get(m.source, "")
