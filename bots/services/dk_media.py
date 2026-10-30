"""🖼 Фото й даташити на DigiKey: звідки посилання, що зараз на DigiKey, звірка й надсилання.

Посилання для DigiKey (`target`) — за «🛒 DigiKey → 🖼 Фото й даташити для DigiKey» (`DigiKeyConfig.media_source`):
`stock` — як на складі й у магазинах (посилання товару, без нього — правило «Налаштувань складу»),
`rule` — свої правила для DigiKey (`media_image_template` / `media_datasheet_template`, {key} — як на складі).
Ним користуються «Зі складу на DigiKey» (нова картка), картка лістингу («за правилом»), сторінка звірки.

DigiKey Products API приймає фото полем `imageUrl`; окремого поля даташита в API немає — лише додаткове поле
DigiKey (Custom v1), якщо воно є й відкрите для запису (`datasheet_field`, назва / код з «datasheet»). Тоді
`upsert_product` надсилає `dk_datasheet_url` у ньому (`add_datasheet`), інакше даташит лишається в Minerva.

Зміни товару, вже опублікованого на DigiKey, DigiKey перевіряє окремо (Products → Pending) — як будь-яку
зміну картки (`dk_view.push_product`).
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone

from inventory.services import media_links
from inventory.services.media_files import DATASHEET, IMAGE, probe

logger = logging.getLogger(__name__)

KINDS = (IMAGE, DATASHEET)
FIELD = {IMAGE: "dk_image_url", DATASHEET: "dk_datasheet_url"}
LABEL = {IMAGE: "фото", DATASHEET: "даташит"}
URL_MAX = 200                         # URLField лістингу
FEW = 5                               # до стількох лістингів — товари DigiKey точково, інакше весь список
MAX_ITEMS = 500
WORKERS = 6
BACKGROUND = True
STATE_KEY, LOCK_KEY = "dk-media", "dk-media-lock"
LOCK_SECONDS = 30 * 60

SAME, FILL, REPLACE, KEEP, BROKEN, PUSH, NONE = "same", "fill", "replace", "keep", "broken", "push", "none"
ACTION_TEXT = {
    SAME: "без змін",
    FILL: "заповнити",
    REPLACE: "замінити",
    KEEP: "залишити — нове не відповідає",
    BROKEN: "нове не відповідає",
    PUSH: "на DigiKey інше — надіслати",
    NONE: "немає посилання за правилом",
}


def _cache():
    from core.shared_cache import cache
    return cache


def config():
    from bots.models import DigiKeyConfig
    return DigiKeyConfig.get()


# ── Звідки посилання ──────────────────────────────────────────────────────────

def _public(url) -> str:
    from bots.services.dk_from_stock import _public as public
    return public(url)


def candidates(product, kind, sku: str = "", cfg=None) -> tuple[list[str], str]:
    """Можливі посилання для DigiKey і звідки: (['https://…'], 'правило DigiKey' / 'товар' / 'правило складу')."""
    cfg = cfg or config()
    if cfg.media_source == cfg.MEDIA_RULE:
        template = (cfg.media_image_template if kind == IMAGE else cfg.media_datasheet_template) or ""
        key = media_links.key_for(product, kind) if product else (sku or "")
        found = media_links.candidates(template.strip(), key, kind)
        return found, ("правило DigiKey" if found else "")
    if product is not None:
        url = _public(product.image_display_url if kind == IMAGE else product.datasheet_display_url)
        if url:
            return [url], "товар (склад)"
        found = media_links.candidates(media_links.templates()[kind], media_links.key_for(product, kind), kind)
        if found:
            return found, "правило складу"
    return [], ""


def target(product, kind, sku: str = "", cfg=None) -> str:
    """Основне посилання для DigiKey (без перевірки) або ''; довше URL_MAX — '' (поле лістингу не прийме)."""
    found, _ = candidates(product, kind, sku, cfg)
    url = found[0] if found else ""
    return url if len(url) <= URL_MAX else ""


def example(cfg=None) -> dict:
    """Для налаштувань: перший лістинг з товаром — {kind: (посилання, звідки)}."""
    from bots.models import DigiKeyListing
    listing = DigiKeyListing.objects.select_related("product").exclude(product=None).order_by("pk").first()
    out = {}
    for kind in KINDS:
        found, how = candidates(listing.product if listing else None, kind,
                                listing.get_supplier_sku() if listing else "", cfg)
        out[kind] = (found[0] if found else "", how)
    return out


# ── Даташит на DigiKey — лише додатковим полем ───────────────────────────────

def datasheet_field() -> dict | None:
    """Поле DigiKey для даташита з останнього отриманого списку полів (без мережі): {code, name, type} або None."""
    from bots.services.dk_from_stock import FIELDS_CACHE_KEY
    defs = _cache().get(FIELDS_CACHE_KEY) or {}
    found = [d for c, d in defs.items() if "datasheet" in f"{c} {d.get('name', '')}".lower().replace(" ", "")]
    found.sort(key=lambda d: d.get("type") != "Link")
    return found[0] if found else None


def add_datasheet(additional_fields: list, listing) -> list:
    """Даташит лістингу — у додаткові поля для upsert, якщо DigiKey має таке поле (і його там ще немає)."""
    field = datasheet_field()
    url = (listing.dk_datasheet_url or "").strip()
    if field and url and all(a.get("code") != field["code"] for a in additional_fields):
        additional_fields.append({"code": field["code"], "type": "String", "value": url})
    return additional_fields


def on_digikey(product: dict | None) -> dict:
    """Що зараз у товарі DigiKey: {image: url, datasheet: url} ('' — немає)."""
    from bots.services.dk_marketplace import _additional_field_str
    if not product:
        return {}
    attrs = {str(f.get("code")): _additional_field_str(f) for f in product.get("additionalFields") or []}
    field = datasheet_field()
    if field:
        sheet = attrs.get(field["code"], "")
    else:
        sheet = next((v for c, v in attrs.items() if "datasheet" in c.lower()), "")
    return {IMAGE: (product.get("imageUrl") or "").strip(), DATASHEET: sheet}


def live_products(listings) -> dict:
    """{pk лістингу: товар DigiKey або None}: до FEW — точково, інакше весь список своїх товарів."""
    from bots.services import dk_marketplace as dkm
    cfg = config()
    listings = list(listings)
    if len(listings) <= FEW:
        return {l.pk: dkm.fetch_product_by_sku(cfg, l.get_supplier_sku(), l.dk_product_id or "") for l in listings}
    own = dkm.fetch_own_products(cfg)
    by_id = {p.get("_id"): p for p in own if p.get("_id")}
    by_sku = {p.get("partNumber"): p for p in own if p.get("partNumber")}
    return {l.pk: by_id.get(l.dk_product_id) or by_sku.get(l.get_supplier_sku()) for l in listings}


# ── Звірка ────────────────────────────────────────────────────────────────────

def row(listing, kind, dk: str | None = None, checked=None, cfg=None) -> dict:
    """Одне посилання лістингу: на DigiKey (None — не читали), у картці, за правилом, що робити."""
    found, how = candidates(listing.product, kind, listing.get_supplier_sku(), cfg)
    card = (getattr(listing, FIELD[kind]) or "").strip()
    if checked is not None:
        new, new_error = media_links.resolve(found, kind, checked)
        card_error = checked(card, kind) if card else None
    else:
        new, new_error, card_error = (found[0] if found else ""), None, None
    if new and len(new) > URL_MAX:
        new_error = f"довше {URL_MAX} символів — поле лістингу не прийме"
    if not new:
        action, pick = NONE, False
    else:
        action, pick = media_links.decide(card, new, new_error, card_error)
        if action == SAME and dk is not None and dk != card and new_error in (None, ""):
            action, pick = PUSH, True
    return {"pk": listing.pk, "kind": kind, "label": LABEL[kind], "card": card, "card_error": card_error,
            "new": new, "new_error": new_error, "how": how, "dk": dk, "action": action,
            "action_text": ACTION_TEXT.get(action, action), "pick": pick,
            "dk_same": dk is not None and dk == (new or card)}


def rows(listings, products=None, check=False, cfg=None) -> list:
    """[{listing, items: [row фото, row даташит], published}]; products — {pk: товар DigiKey} або None."""
    cfg = cfg or config()
    checked = (lambda url, kind: probe(url, kind)) if check else None
    out = []
    for listing in listings:
        dk = on_digikey((products or {}).get(listing.pk)) if products is not None else None
        out.append({"listing": listing, "published": bool(listing.dk_product_id),
                    "found": products is not None and (products or {}).get(listing.pk) is not None,
                    "items": [row(listing, kind, None if dk is None else dk.get(kind, ""), checked, cfg)
                              for kind in KINDS]})
    return out


def parse_picks(values) -> dict:
    """['12:image', '12:datasheet'] → {12: {'image', 'datasheet'}}."""
    out = {}
    for v in values or []:
        pk, _, kind = str(v).partition(":")
        if pk.isdigit() and kind in KINDS:
            out.setdefault(int(pk), set()).add(kind)
    return out


def apply(picks: dict, cfg=None) -> tuple[int, list]:
    """Записати посилання за правилом у вибрані лістинги (без мережі). → (змінено полів, [лістинги з товаром DigiKey])."""
    from bots.models import DigiKeyListing
    cfg = cfg or config()
    changed, touched = 0, []
    for listing in DigiKeyListing.objects.filter(pk__in=list(picks)).select_related("product"):
        fields = []
        for kind in picks[listing.pk]:
            url = target(listing.product, kind, listing.get_supplier_sku(), cfg)
            if url and getattr(listing, FIELD[kind]) != url:
                setattr(listing, FIELD[kind], url)
                fields.append(FIELD[kind])
        if fields:
            listing.save(update_fields=fields)
            changed += len(fields)
        if listing.dk_product_id:
            touched.append(listing.pk)
    return changed, touched


# ── Фонові перевірка й надсилання (стан — core.shared_cache) ─────────────────

def state() -> dict:
    return _cache().get(STATE_KEY) or {}


def _save(st):
    _cache().set(STATE_KEY, st, 7 * 24 * 3600)


def _serial(rs) -> list:
    return [{"pk": r["listing"].pk, "found": r["found"], "items": r["items"]} for r in rs]


def check(listing_ids, user="") -> dict:
    """Прочитати товари DigiKey і перевірити посилання (чи відповідають, чи там справді фото / PDF)."""
    from bots.models import DigiKeyListing
    listings = list(DigiKeyListing.objects.filter(pk__in=list(listing_ids)).select_related("product")[:MAX_ITEMS])
    st = {"mode": "check", "running": True, "total": len(listings), "total_links": 0, "done": 0, "by": user,
          "error": "",
          "started": timezone.now().isoformat(), "finished": "", "rows": []}
    _save(st)
    try:
        products = live_products(listings)
    except Exception as exc:  # noqa: BLE001 — без товарів DigiKey перевіряємо лише посилання
        products, st["error"] = None, f"Товари DigiKey не прочитано: {exc}"[:300]
    cfg = config()
    # що перевіряти — у цьому потоці (база); у паралельних потоках лише мережа (probe)
    todo = set()
    for listing in listings:
        for kind in KINDS:
            found, _ = candidates(listing.product, kind, listing.get_supplier_sku(), cfg)
            todo.update((u, kind) for u in found)
            card = (getattr(listing, FIELD[kind]) or "").strip()
            if card:
                todo.add((card, kind))
    st["total_links"], results = len(todo), {}

    def one(item):
        return item, probe(*item)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for item, error in pool.map(one, sorted(todo)):
            results[item] = error
            st["done"] = len(results)
            if len(results) % 10 == 0:
                _save(st)
    checked = lambda url, kind: results.get((url, kind), "не перевірено")   # noqa: E731
    done = []
    for listing in listings:
        dk = on_digikey(products.get(listing.pk)) if products is not None else None
        done.append({"listing": listing, "found": products is not None and products.get(listing.pk) is not None,
                     "items": [row(listing, kind, None if dk is None else dk.get(kind, ""), checked, cfg)
                               for kind in KINDS]})
    st.update(running=False, finished=timezone.now().isoformat(), rows=_serial(done), read=products is not None,
              done=len(results))
    _save(st)
    return st


def push(listing_ids, user="") -> dict:
    """Надіслати картки вибраних лістингів на DigiKey (фото — imageUrl, даташит — додатковим полем, якщо є)."""
    from bots.models import DigiKeyListing
    from bots.services.dk_view import push_product
    listings = list(DigiKeyListing.objects.filter(pk__in=list(listing_ids)).exclude(dk_product_id="")
                    .select_related("product")[:MAX_ITEMS])
    st = {"mode": "push", "running": True, "total": len(listings), "done": 0, "ok": 0, "errors": [], "by": user,
          "started": timezone.now().isoformat(), "finished": "",
          "no_datasheet": datasheet_field() is None and any(l.dk_datasheet_url for l in listings)}
    _save(st)
    for listing in listings:
        try:
            push_product(listing)
            st["ok"] += 1
        except Exception as exc:  # noqa: BLE001 — один лістинг не зупиняє решту
            st["errors"].append([listing.get_supplier_sku(), str(exc)[:300]])
        st["done"] += 1
        _save(st)
    st.update(running=False, finished=timezone.now().isoformat())
    _save(st)
    return st


def start(mode: str, listing_ids, user="") -> bool:
    """Запустити перевірку (`check`) або надсилання (`push`) у фоні; False — уже йде інше."""
    if not _cache().add(LOCK_KEY, 1, LOCK_SECONDS):
        return False
    fn = check if mode == "check" else push
    ids = list(listing_ids)
    if not BACKGROUND:
        try:
            fn(ids, user)
        finally:
            _cache().delete(LOCK_KEY)
        return True
    threading.Thread(target=_run, args=(fn, ids, user), daemon=True).start()
    return True


def _run(fn, ids, user):
    from django.db import connection
    try:
        fn(ids, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception("DK media %s", fn.__name__)
        st = state()
        st.update(running=False, finished=timezone.now().isoformat(), error=str(exc)[:300])
        _save(st)
    finally:
        _cache().delete(LOCK_KEY)
        connection.close()
