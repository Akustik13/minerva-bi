"""
bots/services/dk_lead.py — ⏱ строки відвантаження офертів DigiKey масово.

`set_days` — нове значення `DigiKeyListing.dk_lead_time_days` вибраним лістингам; `start_push` — надіслати на
DigiKey лише строк: свіжий офер з DigiKey (один GET /offers на всіх) → PUT з усім, як на DigiKey
(`dk_prices.offer_payload`: ціни, залишок, MOQ, назва — без змін), і новим `leadTimeToShip`. У фоні (`BACKGROUND`,
тести — одразу), один запуск одночасно (`LOCK_KEY`), хід — `state()` у core.shared_cache.
"""
import logging
import threading

from django.utils import timezone

logger = logging.getLogger(__name__)

BACKGROUND = True
STATE_KEY, LOCK_KEY = "dk-lead-push", "dk-lead-push-lock"
LOCK_SECONDS = 30 * 60
MAX_DAYS = 365
MAX_ITEMS = 500


def _cache():
    from core.shared_cache import cache
    return cache


def parse_days(value) -> int:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Строк — ціле число днів") from None
    if not 0 <= days <= MAX_DAYS:
        raise ValueError(f"Строк — від 0 до {MAX_DAYS} днів")
    return days


def live_map(listings) -> dict:
    """{pk лістингу: офер DigiKey або None} — один список оферів (dk_prices._offers_for)."""
    from bots.services.dk_prices import _match, _offers_for, offer_for
    listings = list(listings)
    maps = _match(_offers_for(listings))
    return {l.pk: offer_for(l, maps) for l in listings}


def rows(listings, offers=None) -> list:
    """[{listing, minerva, dk, has_offer, differs}] для сторінки; offers — {pk: офер} або None (не читали)."""
    out = []
    for l in listings:
        offer = (offers or {}).get(l.pk)
        dk = offer.get("leadTimeToShip") if offer else None
        out.append({"listing": l, "minerva": l.dk_lead_time_days, "dk": dk, "has_offer": bool(l.dk_offer_id),
                    "read": offers is not None, "differs": offer is not None and dk != l.dk_lead_time_days})
    return out


def set_days(listing_ids, days) -> int:
    from bots.models import DigiKeyListing
    return DigiKeyListing.objects.filter(pk__in=list(listing_ids)).exclude(dk_lead_time_days=days) \
        .update(dk_lead_time_days=days)


def state() -> dict:
    return _cache().get(STATE_KEY) or {}


def _save(st):
    _cache().set(STATE_KEY, st, 7 * 24 * 3600)


def push(listing_ids, user="") -> dict:
    """Надіслати строки вибраних лістингів з офером (лише leadTimeToShip). Повертає стан запуску."""
    from bots.models import DigiKeyListing
    from bots.services.dk_prices import _put_offer, offer_payload, tiers_of
    listings = list(DigiKeyListing.objects.filter(pk__in=list(listing_ids)).exclude(dk_offer_id="")
                    .select_related("product")[:MAX_ITEMS])
    st = {"running": True, "total": len(listings), "done": 0, "ok": 0, "same": 0, "errors": [], "by": user,
          "started": timezone.now().isoformat(), "finished": ""}
    _save(st)
    try:
        offers = live_map(listings)
    except Exception as exc:  # noqa: BLE001 — без списку оферів не надсилаємо нічого
        st.update(running=False, finished=timezone.now().isoformat(), errors=[["—", f"офери DigiKey: {exc}"]])
        _save(st)
        _cache().delete(LOCK_KEY)
        return st
    for listing in listings:
        sku = listing.get_supplier_sku()
        offer = offers.get(listing.pk)
        try:
            if offer is None:
                raise RuntimeError("офера немає на DigiKey (або інший ID) — нічого не надіслано")
            if offer.get("leadTimeToShip") == listing.dk_lead_time_days:
                st["same"] += 1
            else:
                payload = offer_payload(offer, tiers_of(offer.get("prices")), listing)
                payload["leadTimeToShip"] = listing.dk_lead_time_days
                _put_offer(offer["id"], payload)
                st["ok"] += 1
        except Exception as exc:  # noqa: BLE001 — один лістинг не зупиняє решту
            st["errors"].append([sku, str(exc)[:300]])
        st["done"] += 1
        _save(st)
    st.update(running=False, finished=timezone.now().isoformat())
    _save(st)
    _cache().delete(LOCK_KEY)
    logger.info("DK lead push: %s", {k: st[k] for k in ("total", "ok", "same")})
    return st


def start_push(listing_ids, user="") -> bool:
    """Запустити надсилання; False — уже йде інше."""
    if not _cache().add(LOCK_KEY, 1, LOCK_SECONDS):
        return False
    ids = list(listing_ids)
    if not BACKGROUND:
        push(ids, user)
        return True
    threading.Thread(target=_run, args=(ids, user), daemon=True).start()
    return True


def _run(ids, user):
    from django.db import connection
    try:
        push(ids, user)
    except Exception:  # noqa: BLE001
        logger.exception("DK lead push")
        _cache().delete(LOCK_KEY)
    finally:
        connection.close()
