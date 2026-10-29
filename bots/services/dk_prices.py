"""bots/services/dk_prices.py — ціни DigiKey Marketplace ← базові ціни складу Minerva.

Логіка лише тут (адмінка — bots/admin_prices.py, cron — manage.py dk_price_sync --if-due):
  plan()      для кожного лістингу «як зараз на DigiKey → як має бути з цін Minerva» (без мережі, з останніх
              прочитаних цін; refresh() — свіжі ціни з DigiKey);
  create_run() людина вибрала рядки → оновлення з позиціями (нові ціни запам'ятовуються як показано);
  push_run()  у фоні: ще раз свіжі офери з DigiKey, PUT з новими prices (решта офера — як на DigiKey);
  verify()    cron / кнопка: GET офера, ціни збігаються — «✅ діє на DigiKey» → сповіщення;
  auto()      те саме самостійно з cron: великі / масові зміни — на підтвердження людини.

DigiKey Offers API (Marketplace2/Offers/v1, swagger UpdateOffer):
  • VolumePrice = {quantityBreak, price} — валюти немає: ціни у валюті акаунта продавця → currency();
  • PUT /offers/{id} замінює офер цілком (supplierSku, title, prices, quantityAvailable, minOrderQuantity,
    leadTimeToShip — обов'язкові) → payload зі свіжого GET-офера, міняємо лише prices: кількість, назва,
    MOQ, строк і активність лишаються такими, як на DigiKey;
  • GET /offers → OfferExpanded: lastUpdateDateUtc, lastUpdatedSource (Manual / Upload / API / System).
"""
from __future__ import annotations

import html
import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

Q4 = Decimal("0.0001")
MAX_TIERS = 9                 # DigiKey Marketplace — до 9 цінових ступенів
VERIFY_FIRST_SECONDS = 60     # перша перевірка — не раніше, ніж за хвилину після PUT
VERIFY_EVERY_MINUTES = 10
VERIFY_TIMEOUT_HOURS = 24     # стільки чекаємо, доки GET покаже нові ціни
STUCK_MINUTES = 30            # «надсилається» довше — процес зупинився, cron продовжить
TG_LINES = 15
BACKGROUND = True             # тести вимикають — push_run виконується одразу
FEW = 5                       # до стількох оферів — точкові GET ?SupplierSku=, більше — весь список
STALE_MINUTES = 30            # сторінка порівняння сама читає ціни DigiKey, якщо вони старші


class PriceError(Exception):
    pass


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace("−", "-").strip())
    except (InvalidOperation, ValueError):
        return None


def settings():
    from bots.models import DigiKeyPriceSettings
    return DigiKeyPriceSettings.get()


def currency(s=None) -> str:
    """Валюта цін офера на DigiKey (API її не повертає): з налаштувань цін, інакше Locale Currency DigiKey."""
    s = s or settings()
    if s.currency:
        return s.currency.upper()
    from bots.models import DigiKeyConfig
    return (DigiKeyConfig.get().locale_currency or "EUR").upper()


def currency_source(s=None) -> str:
    s = s or settings()
    return "«💲 Налаштування цін DigiKey»" if s.currency else "DigiKey → Locale Currency"


def fx(src: str, dst: str) -> Decimal | None:
    """Курс src → dst за «Налаштуваннями цін магазину → Курси валют» (той самий, що для веб-магазину)."""
    from offers.pricing import fx as _fx
    return _fx(src, dst)


def home_currency() -> str:
    from offers.pricing import home_currency as _home
    return _home()


# ── Правила ───────────────────────────────────────────────────────────────────

@dataclass
class Rules:
    """Як ціна Minerva стає ціною DigiKey: × курс × (100 + adjust_pct) % + adjust_fixed → округлення → запобіжник."""
    adjust_pct: Decimal = Decimal(0)
    adjust_fixed: Decimal = Decimal(0)
    rounding: str = "0.01"
    tiers_mode: str = "minerva"
    min_margin_pct: Decimal | None = None
    source: str = "base_or_sale"
    own: str = ""                     # «своя надбавка …» — заповнює for_listing

    @classmethod
    def from_settings(cls, s=None) -> "Rules":
        s = s or settings()
        return cls(adjust_pct=Decimal(s.adjust_pct or 0), adjust_fixed=Decimal(s.adjust_fixed or 0),
                   rounding=s.rounding or "0.01", tiers_mode=s.tiers_mode or "minerva",
                   min_margin_pct=None if s.min_margin_pct is None else Decimal(s.min_margin_pct),
                   source=s.source or "base_or_sale")

    @classmethod
    def from_dict(cls, data: dict, base: "Rules | None" = None) -> "Rules":
        """З форми сторінки або збереженого запуску; ValueError — зрозуміло, яке поле не так."""
        from bots.models import DigiKeyPriceSettings as S
        base = base or cls()
        out = replace(base, own="")

        def num(key, label):
            raw = data.get(key)
            if raw is None:
                return getattr(base, key)
            if str(raw).strip() == "":
                return None if key == "min_margin_pct" else Decimal(0)
            value = _d(raw)
            if value is None:
                raise ValueError(f"«{label}»: потрібне число, напр. 5 або −2,5.")
            return value

        out.adjust_pct = num("adjust_pct", "Надбавка / знижка, %")
        out.adjust_fixed = num("adjust_fixed", "Фіксована добавка")
        out.min_margin_pct = num("min_margin_pct", "Не нижче закупівельної + %")
        if out.adjust_pct is not None and out.adjust_pct <= -100:
            raise ValueError("«Надбавка / знижка, %»: знижка має бути менша за 100 %.")
        for key, choices in (("rounding", S.ROUNDING_CHOICES), ("tiers_mode", S.TIERS_CHOICES),
                             ("source", S.SOURCE_CHOICES)):
            raw = data.get(key)
            if raw not in (None, ""):
                if raw not in dict(choices):
                    raise ValueError(f"Невідоме значення «{raw}».")
                setattr(out, key, raw)
        return out

    def for_listing(self, listing) -> "Rules":
        """Свої надбавка / добавка лістингу замість загальних."""
        if listing.price_adjust_pct is None and listing.price_adjust_fixed is None:
            return self
        out = replace(self)
        own = []
        if listing.price_adjust_pct is not None:
            out.adjust_pct = Decimal(listing.price_adjust_pct)
            own.append(f"{fmt_signed(out.adjust_pct)} %")
        if listing.price_adjust_fixed is not None:
            out.adjust_fixed = Decimal(listing.price_adjust_fixed)
            own.append(fmt_signed(out.adjust_fixed))
        out.own = "своя: " + " · ".join(own)
        return out

    def as_dict(self) -> dict:
        return {"adjust_pct": str(self.adjust_pct), "adjust_fixed": str(self.adjust_fixed),
                "rounding": self.rounding, "tiers_mode": self.tiers_mode,
                "min_margin_pct": "" if self.min_margin_pct is None else str(self.min_margin_pct),
                "source": self.source}

    def describe(self, cur: str = "") -> str:
        from bots.models import DigiKeyPriceSettings as S
        parts = []
        if self.adjust_pct:
            parts.append(f"{fmt_signed(self.adjust_pct)} %")
        if self.adjust_fixed:
            parts.append(f"{fmt_signed(self.adjust_fixed)} {cur}".strip())
        if not parts:
            parts.append("як у Minerva")
        parts.append(dict(S.ROUNDING_CHOICES).get(self.rounding, self.rounding).lower())
        parts.append("ступені: " + {"minerva": "як у Minerva", "digikey": "як на DigiKey",
                                     "template": "шаблон магазину"}.get(self.tiers_mode, self.tiers_mode))
        if self.min_margin_pct is not None:
            parts.append(f"не нижче закупівельної + {fmt_num(self.min_margin_pct)} %")
        return " · ".join(parts)


# ── Формат ────────────────────────────────────────────────────────────────────

def fmt_num(value) -> str:
    """5 · 2.5 · 12.75 — без зайвих нулів, до 2 знаків."""
    return format(Decimal(value).quantize(Decimal("0.01")).normalize(), "f")


def fmt_price(value) -> str:
    """12.50 · 0.1234 — ціна з 2 знаками, або точніше, якщо так задано."""
    value = Decimal(value)
    return f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else format(value.normalize(), "f")


def fmt_signed(value) -> str:
    value = Decimal(value)
    return ("+" if value >= 0 else "−") + fmt_num(abs(value))


def fmt_tiers(tiers, cur: str = "") -> str:
    if not tiers:
        return "—"
    text = " · ".join(f"{q}: {fmt_price(p)}" for q, p in tiers)
    return f"{text} {cur}".strip()


def tiers_of(rows) -> list[tuple[int, Decimal]]:
    """[{qty, price}] / [{quantityBreak, price}] / [{min_qty, unit_price}] → [(кількість, ціна)] за зростанням."""
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        qty = r.get("qty", r.get("quantityBreak", r.get("breakQuantity", r.get("min_qty"))))
        price = r.get("price", r.get("unitPrice", r.get("unit_price")))
        try:
            qty, price = int(qty), Decimal(str(price)).quantize(Q4)
        except (TypeError, ValueError, InvalidOperation):
            continue
        if qty >= 1 and price > 0:
            out[qty] = price
    return sorted(out.items())


def tiers_json(tiers) -> list[dict]:
    return [{"qty": q, "price": format(Decimal(p).normalize(), "f")} for q, p in tiers or []]


def _key(tiers) -> tuple:
    return tuple((int(q), Decimal(p).quantize(Q4)) for q, p in tiers or [])


# ── Розрахунок ────────────────────────────────────────────────────────────────

def minerva_tiers(product, source: str, home: str | None = None) -> tuple[list, str, str]:
    """Ціни Minerva товару: (ступені, валюта, звідки)."""
    from inventory.services.base_prices import tiers as base_tiers
    tiers = base_tiers(product)
    if tiers:
        return tiers, (product.base_price_currency or "EUR").upper(), "базові ціни"
    if source == "base_or_sale" and product.sale_price:
        return [(1, Decimal(product.sale_price))], home or home_currency(), "ціна продажу"
    return [], "", ""


def _price_at(tiers, qty: int) -> Decimal:
    price = tiers[0][1]
    for q, p in tiers:
        if q <= qty:
            price = p
        else:
            break
    return price


def _template():
    try:
        from shop.models import ShopSettings
        return sorted(ShopSettings.get().price_breaks or [], key=lambda r: int(r["min_qty"]))
    except Exception:
        return []


def _round(value: Decimal, mode: str) -> Decimal:
    from shop.services import round_price
    return round_price(value, mode or "0.01")


def compute(minerva, dk, rules: Rules, rate: Decimal, moq: int = 1,
            purchase=None, purchase_rate=None, template=None) -> tuple[list | None, list[str]]:
    """Нові ступені DigiKey з цін Minerva (у валюті Minerva) — або (None, причина)."""
    notes: list[str] = []
    if rules.tiers_mode == "digikey" and dk:
        breaks = [q for q, _ in dk]
    elif rules.tiers_mode == "template":
        schedule = _template() if template is None else template
        breaks = [1] + [int(s["min_qty"]) for s in schedule if int(s["min_qty"]) >= 2]
        discount = {int(s["min_qty"]): Decimal(str(s["discount"])) for s in schedule}
    else:
        breaks = [q for q, _ in minerva]
    moq = max(1, int(moq or 1))
    if moq > 1 and any(q < moq for q in breaks):
        notes.append(f"ступені менші за MOQ {moq} шт. об'єднано в {moq} шт.")
        breaks = [max(q, moq) for q in breaks]
    breaks = sorted(set(breaks))
    if len(breaks) > MAX_TIERS:
        notes.append(f"на DigiKey — до {MAX_TIERS} ступенів, решту відкинуто")
        breaks = breaks[:MAX_TIERS]
    floor = None
    if rules.min_margin_pct is not None and purchase and purchase_rate is not None:
        floor = Decimal(purchase) * purchase_rate * (100 + rules.min_margin_pct) / 100
    out, raised = [], False
    one = _price_at(minerva, 1)
    for q in breaks:
        base = one * (100 - discount.get(q, Decimal(0))) / 100 if rules.tiers_mode == "template" \
            else _price_at(minerva, q)
        price = base * rate * (100 + rules.adjust_pct) / 100 + rules.adjust_fixed
        price = _round(price, rules.rounding)
        if floor is not None and price < floor:
            price, raised = floor.quantize(Decimal("0.01"), rounding=ROUND_CEILING), True
        if price <= 0:
            return None, notes + ["ціна вийшла ≤ 0 — перевірте знижку / фіксовану добавку"]
        out.append((q, price))
    if raised:
        notes.append(f"піднято до закупівельної + {fmt_num(rules.min_margin_pct)} %")
    if any(b[1] > a[1] for a, b in zip(out, out[1:])):
        notes.append("більша кількість дорожча за меншу")
    return out, notes


@dataclass
class Row:
    """Рядок порівняння одного лістингу."""
    listing: object
    sku: str
    title: str
    state: str                       # ok · staged · no_offer · no_prices · no_rate · bad
    state_text: str = ""
    dk: list = field(default_factory=list)
    minerva: list = field(default_factory=list)
    minerva_currency: str = ""
    minerva_source: str = ""
    rate: Decimal | None = None
    new: list | None = None
    notes: list = field(default_factory=list)
    rules: Rules | None = None
    change_pct: Decimal | None = None
    max_change_pct: Decimal | None = None

    @property
    def differs(self) -> bool:
        return self.state == "ok" and self.new is not None and _key(self.new) != _key(self.dk)

    @property
    def direction(self) -> str:
        if not self.differs:
            return "same"
        if self.change_pct is None:
            return "new"
        ups = downs = 0
        old = dict(self.dk)
        for q, p in self.new:
            if q in old:
                ups += p > old[q]
                downs += p < old[q]
        if ups and downs:
            return "mixed"
        if ups:
            return "up"
        return "down" if downs else "tiers"

    @property
    def problem(self) -> bool:
        return self.state not in ("ok",)


STATE_TEXT = {
    "staged": "⏳ офер чекає затвердження DigiKey",
    "no_offer": "немає опублікованого офера на DigiKey",
    "no_prices": "у товару немає цін у Minerva",
}


def _changes(dk, new) -> tuple[Decimal | None, Decimal | None]:
    old = dict(dk)
    pcts = [(q, (p / old[q] - 1) * 100) for q, p in new if q in old and old[q] > 0]
    if not pcts:
        return None, None
    first = pcts[0][1].quantize(Decimal("0.01"))
    biggest = max((abs(p) for _, p in pcts)).quantize(Decimal("0.01"))
    return first, biggest


def _cached(cache, key, fn):
    if cache is None:
        return fn()
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def row_for(listing, rules: Rules, dk_currency: str, dk_tiers=None, cache: dict | None = None) -> Row:
    """cache — спільний для одного plan(): курси й валюта Minerva без запиту на кожен лістинг."""
    product = listing.product
    rules = rules.for_listing(listing)
    row = Row(listing=listing, sku=listing.get_supplier_sku() or (product.sku if product else ""),
              title=(listing.dk_title or (product.name if product else ""))[:200], state="ok", rules=rules,
              dk=tiers_of(listing.dk_prices) if dk_tiers is None else list(dk_tiers))
    if listing.sync_status != listing.SYNC_PUBLISHED or not listing.dk_offer_id:
        row.state = "staged" if listing.sync_status == listing.SYNC_STAGED else "no_offer"
        row.state_text = STATE_TEXT[row.state]
        return row
    if product is None:
        row.state, row.state_text = "no_prices", "лістинг не прив'язано до товару Minerva"
        return row
    row.minerva, row.minerva_currency, row.minerva_source = minerva_tiers(
        product, rules.source, _cached(cache, "home", home_currency))
    if not row.minerva:
        row.state, row.state_text = "no_prices", STATE_TEXT["no_prices"]
        return row
    row.rate = _cached(cache, ("fx", row.minerva_currency, dk_currency), lambda: fx(row.minerva_currency, dk_currency))
    if row.rate is None:
        row.state = "no_rate"
        row.state_text = (f"немає курсу {row.minerva_currency} → {dk_currency} "
                          "(«Налаштування цін магазину → Курси валют»)")
        return row
    home = _cached(cache, "home", home_currency)
    purchase_rate = (_cached(cache, ("fx", home, dk_currency), lambda: fx(home, dk_currency))
                     if product.purchase_price else None)
    row.new, row.notes = compute(row.minerva, row.dk, rules, row.rate, listing.dk_min_order_qty,
                                 product.purchase_price, purchase_rate,
                                 template=_cached(cache, "template", _template) if rules.tiers_mode == "template" else None)
    if row.new is None:
        row.state, row.state_text = "bad", "; ".join(row.notes)
        return row
    if rules.own:
        row.notes.insert(0, rules.own)
    row.change_pct, row.max_change_pct = _changes(row.dk, row.new)
    return row


def listings_qs():
    from bots.models import DigiKeyListing
    return DigiKeyListing.objects.filter(product__isnull=False).select_related("product")


def plan(listings=None, rules: Rules | None = None, s=None) -> list[Row]:
    """Порівняння для всіх лістингів (або вибраних) — без мережі, з останніх прочитаних цін DigiKey."""
    s = s or settings()
    rules = rules or Rules.from_settings(s)
    cur = currency(s)
    listings = listings_qs() if listings is None else listings
    cache = {}
    rows = [row_for(l, rules, cur, cache=cache) for l in listings]
    return sorted(rows, key=lambda r: r.sku.lower())


def stale(listings) -> bool:
    """Ціни якогось опублікованого офера не читались довше STALE_MINUTES."""
    edge = timezone.now() - timedelta(minutes=STALE_MINUTES)
    return any(l.sync_status == "published" and l.dk_offer_id and (not l.price_checked_at or l.price_checked_at < edge)
               for l in listings)


def summary(rows) -> dict:
    out = {"total": len(rows), "differs": 0, "up": 0, "down": 0, "same": 0, "problems": 0,
           "no_offer": 0, "no_prices": 0, "no_rate": 0}
    for r in rows:
        if r.differs:
            out["differs"] += 1
            out["up"] += r.direction == "up"
            out["down"] += r.direction == "down"
        elif r.state == "ok":
            out["same"] += 1
        else:
            out["problems"] += r.state in ("no_rate", "bad", "no_prices")
            key = "no_offer" if r.state in ("staged", "no_offer") else r.state
            if key in out:
                out[key] += 1
    return out


# ── DigiKey: читання й запис офера ───────────────────────────────────────────

def _fetch_offers() -> list:
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import fetch_offers
    return fetch_offers(DigiKeyConfig.get(), max_count=5000)


def _fetch_one(sku: str) -> dict | None:
    """GET /offers?SupplierSku= — лише офер з точно таким артикулом; помилка DigiKey — виняток."""
    import requests as req
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import (_OFFERS_BASE, DKMarketplaceError, _base_url, _headers,
                                              get_marketplace_token)
    config = DigiKeyConfig.get()
    resp = req.get(f"{_base_url(config)}{_OFFERS_BASE}/offers", params={"SupplierSku": sku, "Max": 5},
                   headers=_headers(config, get_marketplace_token(config)), timeout=15)
    if resp.status_code >= 400:
        raise DKMarketplaceError(f"GET offers {resp.status_code}: {resp.text[:200]}")
    want = (sku or "").strip().lower()
    return next((o for o in resp.json().get("offers") or []
                 if (o.get("supplierSku") or "").strip().lower() == want), None)


def _offers_for(listings=None) -> list:
    """Свіжі офери: кілька лістингів — точково, інакше весь список продавця."""
    if listings is not None:
        listings = [l for l in listings if l is not None]
        if len(listings) <= FEW:
            return [o for o in (_fetch_one(l.get_supplier_sku()) for l in listings) if o]
    return _fetch_offers()


def _match(offers: list) -> tuple[dict, dict]:
    by_id = {o.get("id"): o for o in offers if o.get("id")}
    by_sku = {(o.get("supplierSku") or "").strip().lower(): o for o in offers if o.get("supplierSku")}
    return by_id, by_sku


def offer_for(listing, maps) -> dict | None:
    by_id, by_sku = maps
    return by_id.get(listing.dk_offer_id) or by_sku.get((listing.get_supplier_sku() or "").strip().lower())


def refresh(listings=None) -> dict:
    """Свіжі ціни офера з DigiKey → DigiKeyListing.dk_prices. Повертає {updated, unchanged, not_found, maps}."""
    listings = list(listings_qs() if listings is None else listings)
    maps = _match(_offers_for(listings if len(listings) <= FEW else None))
    now = timezone.now()
    updated = unchanged = 0
    not_found = []
    for listing in listings:
        offer = offer_for(listing, maps)
        if offer is None:
            if listing.dk_offer_id:
                not_found.append(listing.get_supplier_sku())
            continue
        _store(listing, tiers_of(offer.get("prices")), now, offer)
        if listing._price_changed:
            updated += 1
        else:
            unchanged += 1
    return {"updated": updated, "unchanged": unchanged, "not_found": not_found, "maps": maps}


def _store(listing, tiers, now, offer=None):
    """Записує прочитані з DigiKey ціни в лістинг (формат dk_prices — [{qty, price: float}])."""
    fields = ["price_checked_at"]
    listing._price_changed = bool(tiers) and _key(tiers) != _key(tiers_of(listing.dk_prices))
    if listing._price_changed:
        listing.dk_prices = [{"qty": q, "price": float(p)} for q, p in tiers]
        fields.append("dk_prices")
    qty = (offer or {}).get("quantityAvailable")
    if qty is not None:
        try:
            if listing.dk_quantity_available != int(qty):
                listing.dk_quantity_available = int(qty)
                fields.append("dk_quantity_available")
        except (TypeError, ValueError):
            pass
    listing.price_checked_at = now
    listing.save(update_fields=fields)


# Поля UpdateOffer, які беремо з GET-офера як є (крім prices). additionalFields не надсилаємо — як і
# решта Minerva (sync_quantity, масові ціни), щоб не зачепити атрибути іншим форматом.
_OFFER_KEEP = ("description", "isActive", "isAvailable", "minQuantityAlert", "maxOrderQuantity",
               "incrementQuantity", "availableStartDateUtc", "availableEndDateUtc", "logisticClassCode",
               "dateCodeProvided", "returnsAccepted")


def offer_payload(offer: dict, tiers, listing=None) -> dict:
    """Тіло PUT /offers/{id}: усе як на DigiKey, лише нові prices."""
    qty = offer.get("quantityAvailable")
    if qty is None:          # без кількості в GET не можна слати 0 — товар зник би з продажу
        qty = listing.get_dk_quantity() if listing else 0
    payload = {
        "supplierSku": offer.get("supplierSku") or (listing.get_supplier_sku() if listing else ""),
        "title": (offer.get("title") or (listing.dk_title if listing else "") or "").strip()[:50],
        "prices": [{"quantityBreak": int(q), "price": float(p)} for q, p in tiers],
        "quantityAvailable": max(0, int(qty)),
        "minOrderQuantity": max(1, int(offer.get("minOrderQuantity") or (listing.dk_min_order_qty if listing else 1))),
        "leadTimeToShip": max(0, int(offer.get("leadTimeToShip") if offer.get("leadTimeToShip") is not None
                                     else (listing.dk_lead_time_days if listing else 0))),
    }
    for key in _OFFER_KEEP:
        if offer.get(key) is not None:
            payload[key] = offer[key]
    if isinstance(payload.get("description"), str):
        payload["description"] = payload["description"][:500]
    return payload


def _put_offer(offer_id: str, payload: dict) -> dict:
    import requests as req
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import (_OFFERS_BASE, DKMarketplaceError, _base_url, _headers,
                                              get_marketplace_token)
    config = DigiKeyConfig.get()
    token = get_marketplace_token(config)
    resp = req.put(f"{_base_url(config)}{_OFFERS_BASE}/offers/{offer_id}", json=payload,
                   headers=_headers(config, token), timeout=30)
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:300]
        raise DKMarketplaceError(f"PUT offer {resp.status_code}: {body}")
    try:
        return resp.json()
    except ValueError:
        return {}


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if timezone.is_aware(dt) else dt.replace(tzinfo=dt_timezone.utc)


# ── Оновлення ─────────────────────────────────────────────────────────────────

def _who(user) -> str:
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()[:150]
    return str(user or "")[:150]


def _line_from_row(run, row: Row, status: str, note: str = ""):
    from bots.models import DigiKeyPriceChange
    notes = list(row.notes) + ([note] if note else [])
    return DigiKeyPriceChange(
        run=run, listing=row.listing, sku=row.sku[:100], title=row.title[:200], status=status,
        old_prices=tiers_json(row.dk), new_prices=tiers_json(row.new), minerva_prices=tiers_json(row.minerva),
        minerva_currency=row.minerva_currency, rate=row.rate, change_pct=row.change_pct,
        max_change_pct=row.max_change_pct, notes=notes)


def create_run(rows, user=None, rules: Rules | None = None, kind: str = "manual", held: bool = False):
    """Оновлення з позицій, що відрізняються (рядки plan()). Нові ціни — ті, що людина бачила."""
    from bots.models import DigiKeyPriceChange, DigiKeyPriceRun
    s = settings()
    rows = [r for r in rows if r.differs]
    if not rows:
        raise PriceError("Немає позицій з різницею цін — нічого оновлювати.")
    rules = rules or Rules.from_settings(s)
    with transaction.atomic():
        run = DigiKeyPriceRun.objects.create(
            created_by=_who(user) or "Minerva", kind=kind, currency=currency(s), rules=rules.as_dict(),
            status=DigiKeyPriceRun.HELD if held else DigiKeyPriceRun.QUEUED)
        DigiKeyPriceChange.objects.bulk_create(
            [_line_from_row(run, r, DigiKeyPriceChange.HELD if held else DigiKeyPriceChange.QUEUED) for r in rows])
        refresh_counts(run)
    return run


def start(run):
    """Надіслати в фоні (сторінка показує хід); у тестах — одразу."""
    if not BACKGROUND:
        return push_run(run.pk)

    def work():
        from django.db import connection
        try:
            push_run(run.pk)
        except Exception:
            logger.exception("dk_prices push_run #%s", run.pk)
        finally:
            connection.close()

    threading.Thread(target=work, name=f"dk-prices-{run.pk}", daemon=True).start()


def _claim(run_pk) -> bool:
    """Один процес на оновлення: QUEUED (або завислий SENDING) → SENDING."""
    from django.db.models import Q
    from bots.models import DigiKeyPriceRun
    now = timezone.now()
    stuck = Q(status=DigiKeyPriceRun.SENDING, started_at__lt=now - timedelta(minutes=STUCK_MINUTES))
    return bool(DigiKeyPriceRun.objects.filter(Q(status=DigiKeyPriceRun.QUEUED) | stuck, pk=run_pk)
                .update(status=DigiKeyPriceRun.SENDING, started_at=now, progress="читаю офери з DigiKey…"))


def push_run(run_pk):
    """Надсилає позиції «у черзі»: свіжий офер з DigiKey → PUT лише з новими prices → «надіслано»."""
    from bots.models import DigiKeyPriceChange, DigiKeyPriceLog, DigiKeyPriceRun
    if not _claim(run_pk):
        return None
    run = DigiKeyPriceRun.objects.get(pk=run_pk)
    lines = list(run.lines.filter(status=DigiKeyPriceChange.QUEUED).select_related("listing__product"))
    try:
        maps = _match(_offers_for([l.listing for l in lines]))
    except Exception as exc:
        for line in lines:
            line.status, line.error = DigiKeyPriceChange.ERROR, f"Не вдалося прочитати офери DigiKey: {exc}"[:2000]
            line.save(update_fields=["status", "error"])
        return _finish(run)
    now = timezone.now()
    for i, line in enumerate(lines, 1):
        DigiKeyPriceRun.objects.filter(pk=run.pk).update(progress=f"{i} з {len(lines)}: {line.sku}")
        listing = line.listing
        new = tiers_of(line.new_prices)
        offer = offer_for(listing, maps) if listing else None
        if offer is None:
            line.status, line.error = DigiKeyPriceChange.ERROR, "Офер не знайдено на DigiKey."
            line.save(update_fields=["status", "error"])
            continue
        current = tiers_of(offer.get("prices"))
        line.old_prices = tiers_json(current)
        if _key(current) == _key(new):
            line.status = DigiKeyPriceChange.VERIFIED
            line.notes = list(line.notes or []) + ["на DigiKey вже ці ціни"]
            line.verified_at = line.checked_at = now
            line.save(update_fields=["old_prices", "status", "notes", "verified_at", "checked_at"])
            _store(listing, current, now, offer)
            continue
        try:
            answer = _put_offer(offer.get("id") or listing.dk_offer_id, offer_payload(offer, new, listing))
        except Exception as exc:                   # DKMarketplaceError, мережа, токен
            line.status, line.error = DigiKeyPriceChange.ERROR, str(exc)[:2000]
            line.save(update_fields=["old_prices", "status", "error"])
            continue
        line.status, line.sent_at, line.error = DigiKeyPriceChange.SENT, timezone.now(), ""
        echoed = tiers_of((answer or {}).get("prices"))
        if echoed and _key(echoed) != _key(new):
            line.notes = list(line.notes or []) + ["DigiKey у відповіді показав інші ціни — перевіряємо"]
        line.save(update_fields=["old_prices", "status", "sent_at", "error", "notes"])
        _store(listing, new, now)                  # локально — нові; verify() звірить з DigiKey
        old_one, new_one = (current[0][1] if current else None), new[0][1]
        DigiKeyPriceLog.objects.create(
            listing=listing, delta_pct=float((new_one / old_one - 1) * 100) if old_one else 0,
            old_prices=tiers_json(current), new_prices=tiers_json(new),
            user=f"💲 {run.created_by}"[:150])
    return _finish(run)


def _finish(run):
    from bots.models import DigiKeyPriceRun
    run.finished_at = timezone.now()
    run.progress = ""
    run.save(update_fields=["finished_at", "progress"])
    refresh_counts(run)
    run.refresh_from_db()
    if not run.notified_sent and run.sent + run.errors + run.held:
        notify(run, "sent")
        DigiKeyPriceRun.objects.filter(pk=run.pk).update(notified_sent=True)
    return run


def refresh_counts(run):
    """Лічильники й стан оновлення з його позицій."""
    from django.db.models import Count
    from bots.models import DigiKeyPriceChange as L, DigiKeyPriceRun as R
    c = dict(run.lines.values_list("status").annotate(n=Count("id")))
    n = lambda *keys: sum(c.get(k, 0) for k in keys)   # noqa: E731
    fields = {"total": sum(c.values()), "sent": n(L.SENT, L.VERIFIED, L.MISMATCH),
              "verified": n(L.VERIFIED), "held": n(L.HELD), "errors": n(L.ERROR, L.MISMATCH)}
    if n(L.QUEUED) and run.status in (R.QUEUED, R.SENDING):
        status = run.status
    elif n(L.QUEUED):
        status = R.QUEUED
    elif n(L.SENT):
        status = R.SENT
    elif n(L.VERIFIED):
        status = R.PARTIAL if n(L.ERROR, L.MISMATCH) else R.VERIFIED
    elif n(L.ERROR, L.MISMATCH):
        status = R.FAILED
    elif n(L.HELD):
        status = R.HELD
    else:
        status = R.NOTHING
    fields["status"] = status
    if status in (R.VERIFIED, R.PARTIAL) and not run.verified_at and n(L.VERIFIED):
        fields["verified_at"] = timezone.now()
    R.objects.filter(pk=run.pk).update(**fields)
    for k, v in fields.items():
        setattr(run, k, v)
    return run


def confirm(run, line_ids, user=None) -> int:
    """«✓ Оновити на DigiKey» для позицій, що чекали: перерахувати з поточними цінами Minerva й надіслати."""
    from bots.models import DigiKeyPriceChange as L, DigiKeyPriceRun as R
    rules = Rules.from_dict(run.rules or {}, Rules.from_settings())
    cur = currency()
    queued = 0
    for line in run.lines.filter(status=L.HELD, pk__in=line_ids).select_related("listing__product"):
        if line.listing is None:
            line.status, line.error = L.SKIPPED, "Лістинг видалено."
            line.save(update_fields=["status", "error"])
            continue
        row = row_for(line.listing, rules, cur)
        if not row.differs:
            line.status = L.SKIPPED
            line.notes = list(line.notes or []) + [row.state_text or "різниці вже немає"]
            line.save(update_fields=["status", "notes"])
            continue
        fresh = _line_from_row(run, row, L.QUEUED, f"підтвердив {_who(user)}" if _who(user) else "")
        for f in ("old_prices", "new_prices", "minerva_prices", "minerva_currency", "rate", "change_pct",
                  "max_change_pct", "notes", "status"):
            setattr(line, f, getattr(fresh, f))
        line.save()
        queued += 1
    if queued:
        R.objects.filter(pk=run.pk).update(status=R.QUEUED, notified_sent=False, notified_verified=False)
        run.refresh_from_db()
        start(run)
    else:
        refresh_counts(run)
    return queued


def reject(run, line_ids, user=None) -> int:
    from bots.models import DigiKeyPriceChange as L
    n = 0
    for line in run.lines.filter(status=L.HELD, pk__in=line_ids):
        line.status = L.REJECTED
        line.notes = list(line.notes or []) + [f"відхилив {_who(user)}".strip()]
        line.save(update_fields=["status", "notes"])
        n += 1
    refresh_counts(run)
    return n


# ── Перевірка на DigiKey ──────────────────────────────────────────────────────

def verify(run=None, force: bool = False) -> dict:
    """Надіслані ціни вже показує DigiKey? Так — «✅ діє»; 24 год ні або змінено вручну — «⚠ інші ціни»."""
    from bots.models import DigiKeyPriceChange as L, DigiKeyPriceRun as R
    now = timezone.now()
    qs = L.objects.filter(status=L.SENT).select_related("listing__product", "run")
    if run is not None:
        qs = qs.filter(run=run)
    if not force:
        qs = qs.filter(sent_at__lte=now - timedelta(seconds=VERIFY_FIRST_SECONDS)).exclude(
            checked_at__gt=now - timedelta(minutes=VERIFY_EVERY_MINUTES))
    lines = list(qs)
    if not lines:
        return {"checked": 0, "verified": 0, "mismatch": 0}
    maps = _match(_offers_for([l.listing for l in lines]))
    verified = mismatch = 0
    runs = {}
    for line in lines:
        runs[line.run_id] = line.run
        offer = offer_for(line.listing, maps) if line.listing else None
        line.checks += 1
        line.checked_at = now
        fields = ["checks", "checked_at"]
        if offer is not None:
            current = tiers_of(offer.get("prices"))
            updated_at = _parse_dt(offer.get("lastUpdateDateUtc"))
            if updated_at:
                line.dk_updated_at = updated_at
                fields.append("dk_updated_at")
            if _key(current) == _key(tiers_of(line.new_prices)):
                line.status, line.verified_at = L.VERIFIED, now
                fields += ["status", "verified_at"]
                verified += 1
                _store(line.listing, current, now, offer)
            elif updated_at and line.sent_at and updated_at > line.sent_at + timedelta(seconds=5) and \
                    (offer.get("lastUpdatedSource") or "") in ("Manual", "Upload"):
                line.status = L.MISMATCH
                line.error = (f"Після нашого оновлення ціни змінено на DigiKey "
                              f"({offer.get('lastUpdatedSource')}): {fmt_tiers(current)}")
                fields += ["status", "error"]
                mismatch += 1
                _store(line.listing, current, now, offer)
        if line.status == L.SENT and line.sent_at and now - line.sent_at > timedelta(hours=VERIFY_TIMEOUT_HOURS):
            line.status = L.MISMATCH
            line.error = (f"За {VERIFY_TIMEOUT_HOURS} год DigiKey так і не показав нових цін"
                          + ("" if offer else " (офер не знайдено)") + ".")
            fields += ["status", "error"]
            mismatch += 1
        line.save(update_fields=list(dict.fromkeys(fields)))
    for r in runs.values():
        refresh_counts(r)
        if r.status in (R.VERIFIED, R.PARTIAL, R.FAILED) and not r.notified_verified and r.sent:
            notify(r, "verified")
            R.objects.filter(pk=r.pk).update(notified_verified=True)
    return {"checked": len(lines), "verified": verified, "mismatch": mismatch}


# ── Автооновлення ─────────────────────────────────────────────────────────────

def _suppressed(row) -> bool:
    """Те саме вже чекає підтвердження або людина його відхилила — не пропонувати знову."""
    from bots.models import DigiKeyPriceChange as L
    key = _key(row.new)
    return any(_key(tiers_of(line.new_prices)) == key for line in
               L.objects.filter(listing=row.listing, status__in=(L.HELD, L.REJECTED)).only("new_prices"))


def auto(force: bool = False) -> str:
    """Знайти різницю між цінами Minerva й DigiKey і оновити (або запитати) — для cron. Повертає підсумок."""
    from bots.models import DigiKeyPriceChange as L, DigiKeyPriceRun as R, DigiKeyPriceSettings as S
    s = settings()
    now = timezone.now()
    if s.auto_mode == S.AUTO_OFF and not force:
        return "автооновлення вимкнено"
    S.objects.filter(pk=1).update(last_auto_at=now)
    rules = Rules.from_settings(s)
    quiet = now - timedelta(minutes=s.auto_delay_minutes or 0)
    listings = [l for l in listings_qs().filter(price_auto=True, sync_status="published").exclude(dk_offer_id="")
                if not (l.product.base_prices_updated_at and l.product.base_prices_updated_at > quiet)]
    candidates = [r.listing for r in plan(listings, rules, s) if r.differs]
    if not candidates:
        return _auto_result("без змін")
    try:
        refresh(candidates)
    except Exception as exc:
        return _auto_result(f"не вдалося прочитати офери DigiKey: {exc}"[:300])
    rows = [r for r in plan(candidates, rules, s) if r.differs and not _suppressed(r)]
    if not rows:
        return _auto_result("різниця вже чекає рішення або відхилена")
    run = R.objects.create(created_by="Minerva (авто)", kind=R.KIND_AUTO, currency=currency(s),
                           rules=rules.as_dict(), status=R.QUEUED)
    lines = []
    for i, row in enumerate(rows):
        why = ""
        if s.auto_mode != S.AUTO_APPLY:
            why = "режим «питати»"
        elif row.max_change_pct is None:
            why = "на DigiKey інші ступені — порівняти неможливо"
        elif row.max_change_pct > s.max_change_pct:
            why = f"зміна {fmt_num(row.max_change_pct)} % > {fmt_num(s.max_change_pct)} %"
        elif i >= s.auto_max_items:
            why = f"більше {s.auto_max_items} товарів за раз"
        lines.append(_line_from_row(run, row, L.HELD if why else L.QUEUED,
                                    f"на підтвердження: {why}" if why else ""))
    L.objects.bulk_create(lines)
    refresh_counts(run)
    if run.status == R.QUEUED:
        push_run(run.pk)
    else:
        notify(run, "sent")
        R.objects.filter(pk=run.pk).update(notified_sent=True)
    run.refresh_from_db()
    return _auto_result(f"оновлення #{run.pk}: надіслано {run.sent}, чекає {run.held}, помилок {run.errors}")


def _auto_result(text: str) -> str:
    from bots.models import DigiKeyPriceSettings as S
    S.objects.filter(pk=1).update(last_auto_result=text[:300])
    return text


def resume_stuck() -> int:
    """Оновлення, що зависли «у черзі» / «надсилається» (процес сайту перезапустився), — дослати з cron."""
    from django.db.models import Q
    from bots.models import DigiKeyPriceRun as R
    old = timezone.now() - timedelta(minutes=5)
    stuck = timezone.now() - timedelta(minutes=STUCK_MINUTES)
    n = 0
    for run in R.objects.filter(Q(status=R.QUEUED, created_at__lt=old) | Q(status=R.SENDING, started_at__lt=stuck)):
        if push_run(run.pk) is not None:
            n += 1
    return n


def due(s=None, now=None) -> bool:
    from bots.models import DigiKeyPriceSettings as S
    s = s or settings()
    if s.auto_mode == S.AUTO_OFF:
        return False
    now = now or timezone.now()
    return not s.last_auto_at or now - s.last_auto_at >= timedelta(minutes=max(5, s.auto_interval_minutes or 30))


# ── Сповіщення ────────────────────────────────────────────────────────────────

def run_url(run) -> str:
    from django.urls import reverse
    from dashboard.notifications import _abs_url
    return _abs_url(reverse("admin:bots_digikeypricerun_change", args=[run.pk]))


def _line_text(line, cur) -> str:
    new = tiers_of(line.new_prices)
    old = dict(tiers_of(line.old_prices))
    q, p = new[0] if new else (1, None)
    if p is None:
        return f"• <code>{html.escape(line.sku)}</code>"
    was = old.get(q)
    pct = f" ({fmt_signed(line.change_pct)} %)" if line.change_pct is not None else ""
    was_text = f"{fmt_price(was)} → " if was is not None else ""
    more = f" +{len(new) - 1} ступ." if len(new) > 1 else ""
    return f"• <code>{html.escape(line.sku)}</code> {q} шт: {was_text}{fmt_price(p)} {cur}{pct}{more}"


def notify(run, event: str):
    """Telegram + email: event sent (надіслано / чекає) або verified (ціни діють на DigiKey)."""
    from bots.models import DigiKeyPriceChange as L, DigiKeyPriceRun as R
    s = settings()
    if run.kind == R.KIND_MANUAL and not s.notify_manual:
        return
    try:
        from dashboard.notifications import _get_company_name, _get_ns
    except ImportError:
        return
    ns = _get_ns()
    if ns is None:
        return
    lines = list(run.lines.order_by("sku"))
    cur = run.currency
    now = timezone.localtime().strftime("%d.%m.%Y %H:%M")
    if event == "verified":
        shown = [l for l in lines if l.status in (L.VERIFIED, L.MISMATCH)]
        title = "✅ DigiKey: нові ціни діють" if run.status == R.VERIFIED else "⚠ DigiKey: ціни перевірено"
        head = f"Діє: <b>{run.verified}</b> з {run.sent}" + (f" · розбіжностей: <b>{run.errors}</b>" if run.errors else "")
    else:
        shown = [l for l in lines if l.status in (L.SENT, L.VERIFIED, L.ERROR, L.HELD)]
        sent = [l for l in lines if l.status in (L.SENT, L.VERIFIED)]
        title = "💲 DigiKey: ціни оновлено" if sent else "✋ DigiKey: зміни цін чекають підтвердження"
        up = sum(1 for l in sent if (l.change_pct or 0) > 0)
        down = sum(1 for l in sent if (l.change_pct or 0) < 0)
        head = f"Надіслано: <b>{len(sent)}</b> (↑ {up} · ↓ {down})"
        if run.held:
            head += f" · чекає підтвердження: <b>{run.held}</b>"
        if run.errors:
            head += f" · помилок: <b>{run.errors}</b>"
    url = run_url(run)
    kind = "автоматично" if run.kind == R.KIND_AUTO else f"вручну ({run.created_by})"
    if s.notify_telegram and ns.telegram_enabled and ns.telegram_bot_token and ns.telegram_chat_id:
        try:
            from dashboard.notifications import _send_telegram
            tg = [f"🏛️ <b>{html.escape(_get_company_name())}</b>", f"<b>{title}</b> · <i>{now}</i>",
                  f"#{run.pk} · {kind}", "", head, ""]
            for line in shown[:TG_LINES]:
                mark = {"held": "✋ ", "error": "❌ ", "mismatch": "⚠ "}.get(line.status, "")
                tg.append(mark + _line_text(line, cur))
            if len(shown) > TG_LINES:
                tg.append(f"… ще {len(shown) - TG_LINES}")
            tg += ["", f"🔗 {url}"]
            _send_telegram(ns, "\n".join(tg))
        except Exception as exc:
            logger.warning("dk_prices telegram #%s: %s", run.pk, exc)
    if s.notify_email and ns.email_enabled:
        try:
            _send_email(ns, s, run, title, head, shown, url, kind)
        except Exception as exc:
            logger.warning("dk_prices email #%s: %s", run.pk, exc)


def _send_email(ns, s, run, title, head, lines, url, kind):
    from django.core.mail import EmailMultiAlternatives
    from dashboard.notifications import _get_company_name, _smtp_connection, outlook_safe
    to = [e.strip() for e in (s.notify_email_to or ns.email_to or "").split(",") if e.strip()]
    if not to:
        return
    cur = run.currency
    colors = {"verified": "#1a7f37", "sent": "#0969da", "held": "#9a6700", "error": "#cf222e", "mismatch": "#cf222e"}
    rows = []
    for line in lines:
        color = colors.get(line.status, "#57606a")
        reason = line.error or "; ".join(line.notes or [])
        pct = "" if line.change_pct is None else f"{fmt_signed(line.change_pct)} %"
        rows.append(
            "<tr>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #d0d7de;font-family:Consolas,monospace'>{html.escape(line.sku)}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #d0d7de'>{html.escape(line.title[:60])}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #d0d7de;white-space:nowrap'>{fmt_tiers(tiers_of(line.old_prices))}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #d0d7de;white-space:nowrap'><b>{fmt_tiers(tiers_of(line.new_prices))}</b></td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #d0d7de;text-align:right'>{pct}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #d0d7de;color:{color}'>{html.escape(line.get_status_display())}"
            f"{'<br><small>' + html.escape(reason[:200]) + '</small>' if reason else ''}</td></tr>")
    body = (
        "<div style='max-width:900px;font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1c2128'>"
        f"<h2 style='margin:0 0 6px'>{html.escape(title)}</h2>"
        f"<p style='margin:0 0 4px;color:#57606a'>{html.escape(_get_company_name())} · оновлення #{run.pk} · {html.escape(kind)} · "
        f"ціни в {html.escape(cur)}</p><p style='margin:0 0 12px'>{head}</p>"
        "<table width='100%' style='width:100%;border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f6f8fa'><th align='left' style='padding:6px 8px'>Артикул</th>"
        "<th align='left' style='padding:6px 8px'>Назва</th><th align='left' style='padding:6px 8px'>Було на DigiKey</th>"
        "<th align='left' style='padding:6px 8px'>Стало</th><th align='right' style='padding:6px 8px'>Зміна</th>"
        "<th align='left' style='padding:6px 8px'>Стан</th></tr>"
        + "".join(rows) + "</table>"
        f"<p style='margin:14px 0 0'><a href='{html.escape(url)}'>Відкрити звіт у Minerva</a></p></div>")
    subject = f"{title.split(' ', 1)[1] if ' ' in title else title} — #{run.pk}"
    from_email = (ns.email_from or ns.email_host_user or "noreply@minerva.local").strip()
    msg = EmailMultiAlternatives(subject=subject, body="", from_email=from_email, to=to,
                                 connection=_smtp_connection(ns))
    msg.attach_alternative(outlook_safe(body), "text/html")
    msg.send()
