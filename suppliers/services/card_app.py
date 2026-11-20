"""
suppliers/services/card_app.py — 📱 додаток «Minerva Візитки» (Android, mobile/cards_app): підключення телефону
QR-кодом, ключ телефону, дані для додатка. Сторінки — suppliers/app_api.py (JSON) і «📱 Додаток на телефон»
(SupplierCardMixin.card_app_view).

  new_pairing(user, base_url) — одноразовий код (PAIR_TTL) → текст QR {"minerva": "cards", "v": 1, "url", "code"};
  pair(code, name, version, ip) — телефон показав код → CardAppDevice + ключ (лише раз; у базі — SHA-256);
  pair_state(user, key)        — чи вже підключили телефон за цим кодом (сторінка опитує);
  device_for(header, ip)       — «Authorization: Bearer mvc_…» → живий телефон активного співробітника;
  meta(user)                   — категорії, пресети, види, позначки, моделі AI, недавні «де взяли», права;
  card_json(m, user, cats)     — візитка для додатка (плитка + поля + стан AI);
  presets_for / default_preset / save_preset — пресети (ті самі, що на сторінці «📷 Візитка / флаєр»);
  save_category(user, data, cat) — нова категорія або правка (як «🗂 Категорії постачальників»);
  start_read(m, user, model)   — 🧠 AI у фоні + позначка «читає» (reading);
  thumb(m, side)               — мініатюра для списку (кеш MEDIA_ROOT/thumbs/cards/).
"""
import hashlib
import json
import logging
import os
import re
import secrets

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "mvc_"
PAIR_TTL = 600                 # QR підключення діє 10 хв
PAIR_PER_HOUR = 20             # спроб підключення з однієї адреси за годину
SEEN_EVERY = 60                # «востаннє на зв'язку» — не частіше
READING_TTL = 900              # позначка «🧠 AI читає»
THUMB_PX = 480
PAGE = 40
APP_URL = getattr(settings, "MINERVA_CARDS_APP_URL",
                  "https://github.com/Akustik13/minerva-bi/releases/latest/download/minerva-cards.apk")
RELEASES_URL = getattr(settings, "MINERVA_CARDS_RELEASES_URL",
                       "https://github.com/Akustik13/minerva-bi/releases/latest")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class PairError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash(value) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def can(user, op) -> bool:
    return bool(user and user.is_active and user.has_perm(f"inventory.{op}_supplier"))


def can_upload(user) -> bool:
    return can(user, "add") or can(user, "change")


def can_edit(user, m) -> bool:
    """Як на сторінці фото: з правом змінювати постачальників — будь-яку, з правом додавати — свою."""
    return can(user, "change") or (can(user, "add") and m.created_by_id == user.pk)


# ── 🔗 Підключення телефону ──

def new_pairing(user, base_url) -> dict:
    from core.shared_cache import cache
    code = secrets.token_urlsafe(24)
    key = _hash(code)
    cache.set(f"sup-app-pair:{key}", {"user": user.pk}, PAIR_TTL)
    qr =json.dumps({"minerva": "cards", "v": 1, "url": base_url.rstrip("/"), "code": code},
                    ensure_ascii=False, separators=(",", ":"))
    return {"key": key[:16], "qr": qr, "code": code, "expires_in": PAIR_TTL, "url": base_url.rstrip("/")}


def pair_state(user, key) -> dict:
    """Сторінка опитує: {paired: False} поки телефон не показав код; {paired: True, device: …} — підключено."""
    from core.shared_cache import cache
    key = str(key or "")[:16]
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        return {"paired": False, "expired": True}
    state = cache.get(f"sup-app-paired-short:{key}")
    if not state or state.get("user") != user.pk:
        return {"paired": False}
    return {"paired": True, "device": state.get("device") or ""}


def _rate(ip, kind="pair"):
    """Спроби з однієї адреси за годину (підключення й «чий QR» — окремо) → PairError 429 понад PAIR_PER_HOUR."""
    from core.shared_cache import cache
    hour = timezone.now().strftime("%Y%m%d%H")
    rate_key = f"sup-app-{kind}-ip:{ip or '-'}:{hour}"
    cache.add(rate_key, 0, 3700)
    try:
        tries = cache.incr(rate_key)
    except ValueError:
        tries = 1
    if tries > PAIR_PER_HOUR:
        raise PairError("Забагато спроб підключення — спробуйте за годину.", 429)


def pair_owner(code, ip=None):
    """Чий QR підключення (код ще не використано) → людина; код лишається дійсним — телефон спершу питає
    «Підключити до акаунта …?»."""
    from django.contrib.auth import get_user_model
    from core.shared_cache import cache
    _rate(ip, "pair-info")
    code = str(code or "").strip()
    key = _hash(code)
    data = cache.get(f"sup-app-pair:{key}") if code else None
    if not data or cache.get(f"sup-app-pair-used:{key}"):
        raise PairError("QR-код недійсний або застарів — на сторінці «📱 Додаток на телефон» у Minerva покажіть "
                        "новий.", 410)
    user = get_user_model().objects.filter(pk=data.get("user"), is_active=True, is_staff=True).first()
    if user is None or not can_upload(user):
        raise PairError("Цей акаунт Minerva не може додавати візитки.", 403)
    return user


def pair(code, name="", version="", ip=None):
    """→ (CardAppDevice, ключ). Код — одноразовий, PAIR_TTL; з однієї адреси — PAIR_PER_HOUR спроб на годину."""
    from django.contrib.auth import get_user_model
    from core.shared_cache import cache
    from suppliers.models import CardAppDevice
    _rate(ip)
    code = str(code or "").strip()
    key = _hash(code)
    data = cache.get(f"sup-app-pair:{key}") if code else None
    if not data or not cache.add(f"sup-app-pair-used:{key}", 1, PAIR_TTL + 60):
        raise PairError("QR-код недійсний або застарів — на сторінці «📱 Додаток на телефон» у Minerva покажіть "
                        "новий.", 410)
    cache.delete(f"sup-app-pair:{key}")
    user = get_user_model().objects.filter(pk=data.get("user"), is_active=True, is_staff=True).first()
    if user is None or not can_upload(user):
        raise PairError("Цей акаунт Minerva не може додавати візитки.", 403)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = CardAppDevice.objects.create(
        user=user, name=" ".join(str(name or "").split())[:120] or "Android",
        app_version=str(version or "")[:20], token_hash=_hash(token), token_hint=token[-4:],
        last_seen_at=timezone.now(), last_ip=ip or None)
    cache.set(f"sup-app-paired-short:{key[:16]}", {"user": user.pk, "device": device.name}, PAIR_TTL + 300)
    return device, token


def device_for(header, ip=None):
    """«Bearer mvc_…» → CardAppDevice (з user) або None: ключ невідомий / відключений, людина не активна."""
    from suppliers.models import CardAppDevice
    m = re.match(r"^\s*(?:Bearer|Token)\s+(\S+)\s*$", str(header or ""))
    if not m or not m.group(1).startswith(TOKEN_PREFIX):
        return None
    device = (CardAppDevice.objects.select_related("user")
              .filter(token_hash=_hash(m.group(1)), revoked_at=None).first())
    if device is None or not device.user.is_active or not device.user.is_staff:
        return None
    now = timezone.now()
    if device.last_seen_at is None or (now - device.last_seen_at).total_seconds() > SEEN_EVERY \
            or (ip and device.last_ip != ip):
        CardAppDevice.objects.filter(pk=device.pk).update(last_seen_at=now, last_ip=ip or None)
    return device


def devices_for(user, everyone=False):
    from suppliers.models import CardAppDevice
    qs = CardAppDevice.objects.select_related("user", "user__profile")
    return qs if everyone else qs.filter(user=user)


def revoke(device):
    from suppliers.models import CardAppDevice
    CardAppDevice.objects.filter(pk=device.pk, revoked_at=None).update(revoked_at=timezone.now())


# ── 📌 Пресети ──

def presets_for(user) -> list:
    from suppliers.models import SupplierCardPreset
    qs = (SupplierCardPreset.objects.filter(Q(owner=user) | Q(shared=True)).select_related("owner")
          .prefetch_related("categories"))
    return [{"id": p.pk, "name": p.name, "event": p.event, "kind": p.kind, "role": p.role, "marks": p.marks or [],
             "note": p.note, "categories": [c.pk for c in p.categories.all()], "read_ai": p.read_ai,
             "model": p.model, "shared": p.shared, "mine": p.owner_id == user.pk,
             "owner": "" if p.owner_id == user.pk else (p.owner.get_full_name() or p.owner.username)} for p in qs]


def default_preset(user):
    profile = getattr(user, "profile", None)
    value = (getattr(profile, "ui_prefs", None) or {}).get("supplier_card_preset") if profile else None
    return value if isinstance(value, int) else None


def _ids(values) -> list:
    if isinstance(values, str):
        values = values.split(",")
    return [int(x) for x in (values or []) if str(x).strip().isdigit()]


def _strs(values) -> list:
    if isinstance(values, str):
        values = values.split(",")
    return [str(x).strip() for x in (values or []) if str(x).strip()]


def save_preset(user, data: dict) -> dict:
    """data: action save | delete | default, id, name, event, kind, role, marks, note, categories, read_ai, model, shared,
    make_default → {presets, default, id}. Змінити / видалити — лише свій пресет. ValueError — зрозуміла причина."""
    from core.models import UserProfile
    from suppliers.models import SupplierCardPreset, SupplierCategory, SupplierMedia
    from suppliers.services.ai import MODELS
    action, pid = data.get("action"), str(data.get("id") or "")
    mine = SupplierCardPreset.objects.filter(owner=user)
    default, saved = default_preset(user), None
    if action == "save":
        name = " ".join(str(data.get("name") or "").split())[:120]
        if not name:
            raise ValueError("Назва пресету порожня.")
        p = (mine.filter(pk=int(pid)).first() if pid.isdigit() else None) or SupplierCardPreset(owner=user)
        p.name, p.event, p.note = name, str(data.get("event") or "")[:200], str(data.get("note") or "")[:4000]
        p.kind = data.get("kind") if data.get("kind") in dict(SupplierMedia.KINDS) else SupplierMedia.CARD
        p.role = data.get("role") if data.get("role") in dict(SupplierMedia.ROLES) else ""
        p.marks = [x for x in _strs(data.get("marks")) if x in dict(SupplierMedia.MARKS)]
        p.read_ai, p.shared = _bool(data.get("read_ai")), _bool(data.get("shared"))
        p.model = data.get("model") if data.get("model") in dict(MODELS) else ""
        p.save()
        p.categories.set(SupplierCategory.objects.filter(pk__in=_ids(data.get("categories"))))
        saved = p.pk
        if _bool(data.get("make_default")):
            default = p.pk
    elif action == "delete":
        if pid.isdigit():
            mine.filter(pk=int(pid)).delete()
        if str(default) == pid:
            default = None
    elif action == "default":
        default = int(pid) if pid.isdigit() and SupplierCardPreset.objects.filter(
            Q(owner=user) | Q(shared=True), pk=int(pid)).exists() else None
    else:
        raise ValueError("Невідома дія")
    if default != default_preset(user):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        from django.db import transaction
        with transaction.atomic():
            prefs = dict(UserProfile.objects.select_for_update().get(pk=profile.pk).ui_prefs or {})
            if default:
                prefs["supplier_card_preset"] = default
            else:
                prefs.pop("supplier_card_preset", None)
            UserProfile.objects.filter(pk=profile.pk).update(ui_prefs=prefs)
        if getattr(user, "profile", None) is not None:
            user.profile.ui_prefs = prefs
    return {"presets": presets_for(user), "default": default, "id": saved}


def _bool(value) -> bool:
    return value is True or str(value or "").strip().lower() in ("1", "true", "on", "yes")


# ── 🗂 Категорії ──

def category_json(c) -> dict:
    return {"id": c.pk, "name": c.name, "icon": c.icon or "🗂", "color": c.color, "description": c.description,
            "parent": c.parent_id, "order": c.order}


def save_category(user, data: dict, cat=None):
    """Нова категорія (suppliers.add_suppliercategory) або правка наявної (change): назва, значок, колір, опис.
    Та сама назва вже є — ValueError (назва унікальна)."""
    from suppliers.models import SupplierCategory
    perm = "suppliers.change_suppliercategory" if cat is not None else "suppliers.add_suppliercategory"
    if not user.has_perm(perm):
        raise PermissionError("Немає права " + ("змінювати" if cat is not None else "додавати") + " категорії.")
    name = " ".join(str(data.get("name") or (cat.name if cat else "")).split())[:120]
    if len(name) < 2:
        raise ValueError("Назва категорії — щонайменше 2 літери.")
    others = SupplierCategory.objects.exclude(pk=cat.pk if cat else None).values_list("name", flat=True)
    if any(n.casefold() == name.casefold() for n in others):     # без огляду на регістр (і кирилиця в SQLite)
        raise ValueError(f"Категорія «{name}» уже є.")
    c = cat or SupplierCategory()
    c.name = name
    if "icon" in data or cat is None:
        c.icon = str(data.get("icon") or "").strip()[:8] or "🏭"
    if data.get("color") and _COLOR_RE.match(str(data["color"])):
        c.color = str(data["color"])
    if "description" in data:
        c.description = str(data.get("description") or "")[:1000]
    c.save()
    return c


# ── 🪪 Візитки ──

def reading(pk) -> bool:
    from core.shared_cache import cache
    return bool(cache.get(f"sup-card-reading:{pk}"))


def start_read(m, user, model=None) -> str:
    """🧠 AI читає фото у фоні (те саме ai.card, що на сторінці фото); поки читає — card_json(…)["reading"]."""
    from core.shared_cache import cache
    from suppliers.services import ai, jobs
    model = ai.model_of(model)
    cache.set(f"sup-card-reading:{m.pk}", 1, READING_TTL)

    def run():
        try:
            ai.card(m, user=user, model=model)
        finally:
            cache.delete(f"sup-card-reading:{m.pk}")
        return {"message": "🧠 Прочитано"}
    return jobs.start(user, f"card {m.pk}", run)


def _person(u) -> str:
    return (u.get_full_name() or u.username) if u is not None else ""


def card_json(m, user, cats=None) -> dict:
    from suppliers.services import card_access, cards
    d = cards.card_data(m)
    i = cards.info(m, cats)
    addr = ", ".join(x for x in (d.get("addr_street"), " ".join(y for y in (d.get("addr_zip"), d.get("addr_city"))
                                                                     if y), i["country"]) if x)
    return {
        "id": m.pk, "uuid": m.client_uuid, "kind": m.kind, "title": m.title, "event": m.event, "note": m.note,
        "role": m.role, "role_ai": m.role_ai, "role_why": i["role_why"],
        "marks": m.marks or [], "state": m.state, "ai": i["ai"]["key"], "ai_label": i["ai"]["label"],
        "reading": reading(m.pk), "error": m.error, "cost_usd": float(m.cost_usd or 0),
        "company": i["company"], "person": i["person"], "position": i["position"], "country": i["country"],
        "flag": i["flag"], "site": i["site"], "desc": i["desc"], "categories": [c.pk for c in i["cats"]],
        "cats_guessed": i["cats_guessed"], "own_categories": [c.pk for c in m.categories.all()],
        "new_category": i["new_category"],
        "email": d.get("contact_email") or d.get("email") or "", "company_email": d.get("email") or "",
        "phone": d.get("contact_phone") or d.get("phone") or "", "mobile": d.get("contact_mobile") or "",
        "address": addr, "notes": d.get("notes") or "", "qr": bool(m.qr_text),
        "supplier": {"id": m.supplier_id, "name": m.supplier.name} if m.supplier_id else None,
        "customer": {"id": m.customer_id, "name": m.customer.company or m.customer.name}
        if m.customer_id else None,
        "has_back": bool(m.image_back), "created_at": m.created_at.isoformat(),
        # 📎 ще фото групи (друга візитка, флаєр, каталог…) і люди з інших візиток
        "photos": [{"id": p.pk, "kind": p.kind, "label": p.get_kind_display(), "note": p.note, "uuid": p.client_uuid}
                   for p in m.photos.all()],
        "people": card_photos_people(m),
        "by": _person(m.created_by), "mine": m.created_by_id == user.pk, "editable": can_edit(user, m),
        # 👁 хто бачить і хто / звідки зняв
        "visibility": m.visibility, "can_set_visibility": card_access.can_set_visibility(user, m),
        "by_role": card_access.role_label(m.created_by), "source": m.source,
        "source_label": card_access.source_label(m),
        "preset": m.preset_id,
    }


def card_photos_people(m) -> list:
    from suppliers.services import card_photos
    return card_photos.people(m)


def cards_qs(user=None):
    """Візитки для додатка — лише ті, які людина бачить (👥 усі + свої 🔒, card_access.visible)."""
    from suppliers.models import SupplierMedia
    from suppliers.services import card_access
    qs = SupplierMedia.objects.select_related("supplier", "customer", "created_by", "device", "created_by__profile") \
        .prefetch_related("categories", "supplier__categories", "photos")
    return card_access.visible(qs, user)


def search(user, params) -> tuple:
    """Фільтри списку (як «🪪 Візитки й флаєри»): q, cat, ai (done / none / failed / nocat), event, mark, mine, role,
    show new; ids — лише ці (стан знімків, ще не прочитаних AI). → (візитки, є ще)."""
    from suppliers.models import SupplierCategory, SupplierMedia
    qs = cards_qs(user)
    ids = _ids(params.get("ids"))
    if ids:
        qs = qs.filter(pk__in=ids[:200])
        all_cats = list(SupplierCategory.objects.filter(is_active=True))
        return [card_json(m, user, all_cats) for m in qs], False
    if _bool(params.get("mine")):
        qs = qs.filter(created_by=user)
    role = params.get("role") or ""
    if role == "-":
        qs = qs.filter(role="")
    elif role and role in dict(SupplierMedia.ROLES):
        qs = qs.filter(role=role)
    if params.get("show") == "new":
        qs = qs.filter(supplier=None, customer=None)
    if params.get("vis") in (SupplierMedia.TEAM, SupplierMedia.PRIVATE):     # 👁 лише 🔒 мої / 👥 спільні
        qs = qs.filter(visibility=params.get("vis"))
    event = " ".join(str(params.get("event") or "").split())[:200]
    if event == "-":
        qs = qs.filter(event="")
    elif event:
        qs = qs.filter(event__iexact=event)
    ai_filter = params.get("ai") or ""
    if ai_filter == "done":
        qs = qs.filter(cost_usd__gt=0)
    elif ai_filter == "none":
        qs = qs.filter(cost_usd=0).exclude(state=SupplierMedia.FAILED)
    elif ai_filter == "failed":
        qs = qs.filter(state=SupplierMedia.FAILED)
    elif ai_filter == "nocat":
        qs = qs.filter(categories=None, supplier=None)
    q = " ".join(str(params.get("q") or "").split())[:100]
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(note__icontains=q) | Q(event__icontains=q) |
                       Q(supplier__name__icontains=q) | Q(customer__company__icontains=q) |
                       Q(extracted__name__icontains=q) | Q(extracted__contact_name__icontains=q) |
                       Q(extracted__description__icontains=q))
    mark = str(params.get("mark") or "")
    cat = str(params.get("cat") or "")
    cat_id = int(cat) if cat.isdigit() else None
    if cat_id:
        qs = qs.filter(Q(categories=cat_id) | Q(supplier__categories=cat_id) | Q(categories=None)).distinct()
    page = max(1, min(int(params.get("page")) if str(params.get("page") or "").isdigit() else 1, 200))
    all_cats = list(SupplierCategory.objects.filter(is_active=True))
    if not (mark or cat_id):
        rows = list(qs[(page - 1) * PAGE:page * PAGE + 1])
        return [card_json(m, user, all_cats) for m in rows[:PAGE]], len(rows) > PAGE
    out, skip, more = [], (page - 1) * PAGE, False
    for m in qs[:3000]:
        if mark and mark not in (m.marks or []):
            continue
        j = card_json(m, user, all_cats)
        if cat_id and cat_id not in j["categories"]:
            continue
        if skip:
            skip -= 1
            continue
        if len(out) >= PAGE:
            more = True
            break
        out.append(j)
    return out, more


def recent_events(limit=20, user=None) -> list:
    from suppliers.models import SupplierMedia
    from suppliers.services import card_access
    seen, out = set(), []
    qs = card_access.visible(SupplierMedia.objects.exclude(event=""), user)
    for e in qs.order_by("-created_at").values_list("event", flat=True)[:500]:
        k = " ".join(e.lower().split())
        if k and k not in seen:
            seen.add(k)
            out.append(e)
        if len(out) >= limit:
            break
    return out


def meta(user) -> dict:
    from suppliers.models import SupplierCategory, SupplierMedia, SupplierMediaPhoto
    from suppliers.services import card_access
    from suppliers.services.ai import DEFAULT_MODEL, MODELS
    return {
        "user": {"name": _person(user), "email": user.email},
        "perms": {"add": can(user, "add"), "change": can(user, "change"), "view": can(user, "view"),
                  "cat_add": user.has_perm("suppliers.add_suppliercategory"),
                  "cat_change": user.has_perm("suppliers.change_suppliercategory"),
                  "upload": can_upload(user),
                  "crm_add": user.has_perm("crm.add_customer"), "crm_change": user.has_perm("crm.change_customer")},
        "roles": [[k, v] for k, v in SupplierMedia.ROLES],
        "categories": [category_json(c) for c in SupplierCategory.objects.filter(is_active=True)],
        "presets": presets_for(user), "default_preset": default_preset(user),
        "kinds": [[k, v] for k, v in SupplierMedia.KINDS], "marks": [[k, v] for k, v in SupplierMedia.MARKS],
        "photo_kinds": [[k, v] for k, v in SupplierMediaPhoto.KINDS],
        "models": [[k, v] for k, v in MODELS], "default_model": DEFAULT_MODEL,
        "events": recent_events(user=user), "app_url": APP_URL,
        "visibility": [[k, v] for k, v in SupplierMedia.VISIBILITY], "role_label": card_access.role_label(user),
    }


# ── 🖼 Мініатюри ──

def side_field(m, side):
    """front / back / p<id> (ще фото групи) → FieldFile або None."""
    side = str(side or "front")
    if side.startswith("p") and side[1:].isdigit():
        p = m.photos.filter(pk=int(side[1:])).first()
        return p.image if p is not None and p.image else None
    field = m.image_back if side == "back" else m.image
    return field or None


def thumb(m, side="front"):
    """Мініатюра THUMB_PX (JPEG) для списку додатка → шлях до файлу або None (немає фото / не картинка)."""
    field = side_field(m, side)
    if not field:
        return None
    try:
        mtime = int(field.storage.get_modified_time(field.name).timestamp())
    except Exception:  # noqa: BLE001
        mtime = 0
    folder = os.path.join(settings.MEDIA_ROOT, "thumbs", "cards")
    path = os.path.join(folder, f"{m.pk}-{side}-{_hash(field.name + str(mtime))[:12]}.jpg")
    if os.path.exists(path):
        return path
    try:
        from PIL import Image, ImageOps
        with field.open("rb") as fh:
            img = ImageOps.exif_transpose(Image.open(fh)).convert("RGB")
        img.thumbnail((THUMB_PX, THUMB_PX))
        os.makedirs(folder, exist_ok=True)
        tmp = path + ".part"
        img.save(tmp, "JPEG", quality=82)
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("card thumb failed for %s", m.pk, exc_info=True)
        return None


def is_image(upload) -> bool:
    """Фото з телефона — справді картинка (Pillow), а не будь-який файл під назвою photo.jpg."""
    try:
        from PIL import Image
        pos = upload.tell() if hasattr(upload, "tell") else 0
        Image.open(upload).verify()
        upload.seek(pos)
        return True
    except Exception:  # noqa: BLE001
        try:
            upload.seek(0)
        except Exception:  # noqa: BLE001
            pass
        return False
