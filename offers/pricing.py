"""Ціни, строки поставки й валюта Angebot — лише з даних Minerva, у Decimal.

Ціна позиції (Offer.price_mode, за замовчуванням — з «Налаштувань Angebote»):
- auto — склад Minerva, а немає там ціни — DigiKey Marketplace; stock — лише склад Minerva;
  digikey — спершу DigiKey, потім склад; manual — Minerva ціну не підставляє.
Склад Minerva: 1) магазин за замовчуванням (ступені за кількістю, діюча акція); 2) «Ціна продажу» товару;
3) базові ціни товару (ступені). DigiKey Marketplace — ціни нашого офера (DigiKeyListing.dk_prices,
у валюті DigiKeyConfig.locale_currency, як і «Базові ціни ← DigiKey»).
Інша валюта — × курс із «Налаштувань цін» магазину (ShopSettings.fx_rates: скільки одиниць валюти Minerva за 1
одиницю); курсу немає — ціни немає, без вигадок.
Строк (Offer.lead_mode): є на складі потрібна кількість — «ab Lager»; інакше «Термін поставки» товару; інакше DigiKey.
Валюта (currency_for): за країною клієнта — Європа / інші (OfferSettings), країну не визначено — «Валюта».
Округлення: ціна за одиницю — 4 знаки, суми — 2 знаки, ROUND_HALF_UP.
"""
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
HUNDRED = Decimal("100")

STOCK_TEXT = {"de": "ab Lager", "en": "from stock", "de_en": "ab Lager / from stock", "uk": "зі складу"}
DAYS_TEXT = {"de": "{n} Tage", "en": "{n} days", "de_en": "{n} Tage / days", "uk": "{n} днів"}
MANUAL_LEAD = "Введено вручну"
# Групи джерел для форми: 📦 склад Minerva, 🔷 DigiKey, ✍️ вручну
SOURCE_GROUPS = {"shop": "stock", "sale_price": "stock", "base": "stock", "quote": "stock", "digikey": "digikey",
                 "manual": "manual"}


@dataclass
class PriceQuote:
    unit_price: Decimal | None
    source: str
    note: str


@dataclass
class LeadQuote:
    text: str
    source: str     # stock | product | digikey | manual
    note: str


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Q2, rounding=ROUND_HALF_UP)


def _num(value) -> str:
    """1.2300 → 1.23 для пояснень."""
    d = Decimal(str(value)).normalize()
    return format(d, "f")


def _reached(tiers, qty):
    """Ступінь, якого досягнуто для qty: (min_qty, price) або None."""
    best = None
    for min_qty, price in sorted(tiers, key=lambda t: t[0]):
        if qty >= min_qty:
            best = (min_qty, price)
    return best


# ── Валюта й курси ───────────────────────────────────────────────────────────

def home_currency() -> str:
    from config.models import SystemSettings
    return ((SystemSettings.objects.first() or SystemSettings()).default_currency or "EUR").upper()


def fx(src, dst) -> Decimal | None:
    """Курс src → dst з «Налаштувань цін» магазину ({"USD": 0.86} — 1 USD = 0,86 валюти Minerva); обидві не
    валюта Minerva — через неї. None — курсу немає."""
    src, dst = (src or "").upper(), (dst or "").upper()
    if not src or not dst or src == dst:
        return Decimal(1)
    try:
        from shop.models import ShopSettings
        rates = ShopSettings.get().fx_rates or {}
    except ImportError:
        rates = {}
    home = home_currency()

    def rate(cur):
        if cur == home:
            return Decimal(1)
        try:
            r = Decimal(str(rates.get(cur)))
        except (TypeError, ValueError, ArithmeticError):
            return None
        return r if r > 0 else None
    a, b = rate(src), rate(dst)
    return None if a is None or b is None else a / b


def _in(currency, price, src) -> tuple:
    """Ціна у валюті src → (ціна у валюті Angebot | None, пояснення курсу для примітки)."""
    src = (src or currency).upper()
    rate = fx(src, currency)
    if rate is None:
        return None, f" — немає курсу {src}→{currency} («Налаштування цін» магазину → Курси валют)"
    price = Decimal(str(price))
    if rate == 1:
        return price.quantize(Q4, rounding=ROUND_HALF_UP), ""
    # перерахована ціна — до центів (у документі не «24,854»)
    return (price * rate).quantize(Q2, rounding=ROUND_HALF_UP).quantize(Q4), \
        f": {_num(price)} {src} × курс {_num(rate.quantize(Q4, rounding=ROUND_HALF_UP))}"


# Країна в останньому рядку адреси / підпису: «12345 Berlin, Germany», «Kyiv, Ukraine»
_ALIASES = {"USA": "US", "U.S.A.": "US", "U.S.": "US", "UNITED STATES OF AMERICA": "US", "UK": "GB", "U.K.": "GB",
            "ENGLAND": "GB", "SCOTLAND": "GB", "GREAT BRITAIN": "GB", "HOLLAND": "NL", "CZECH REPUBLIC": "CZ",
            "RUSSIAN FEDERATION": "RU", "TURKEY": "TR", "SOUTH KOREA": "KR", "REPUBLIC OF KOREA": "KR",
            "KOREA": "KR", "UAE": "AE", "P.R. CHINA": "CN", "PR CHINA": "CN", "BRD": "DE", "SCHWEIZ": "CH",
            "SUISSE": "CH", "ÖSTERREICH": "AT", "УКРАЇНА": "UA", "УКРАИНА": "UA"}
_NOT_COUNTRY = {"GEORGIA"}    # штат США; Грузія — «Georgien» / «Грузія»
_names = _country_re = None


def _country_names() -> dict:
    """{НАЗВА ВЕЛИКИМИ: ISO-код} — назви uk / de / en з shop/countries.py і поширені скорочення."""
    global _names
    if _names is None:
        from shop.countries import COUNTRIES
        _names = dict(_ALIASES)
        for code, c in COUNTRIES.items():
            for lang in ("uk", "de", "en"):
                if c[lang].upper() not in _NOT_COUNTRY:
                    _names.setdefault(c[lang].upper(), code)
    return _names


def _country_in_text(text) -> str:
    """ISO-код країни з кінця рядка адреси (останній рядок з назвою країни); не знайдено — ''."""
    global _country_re
    names = _country_names()
    if _country_re is None:
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        _country_re = re.compile(rf"(?:^|[\s,/(-])({alt})\)?\.?\s*$", re.IGNORECASE)
    found = ""
    for line in (text or "").splitlines():
        m = _country_re.search(line.strip())
        if m:
            found = names.get(m.group(1).upper(), "")
    return found


def _country_of_email(email) -> str:
    from shop.countries import COUNTRIES
    tld = (email or "").rsplit(".", 1)[-1].upper() if "@" in (email or "") else ""
    tld = {"UK": "GB"}.get(tld, tld)
    return tld if len(tld) == 2 and tld in COUNTRIES else ""


def is_europe(code) -> bool:
    from shop.countries import COUNTRIES
    return COUNTRIES.get((code or "").upper(), {}).get("region") in ("eu", "europe")


def currency_for(customer=None, address="", email="") -> dict:
    """Валюта Angebot за країною клієнта: {currency, country, reason}. Країна — картка CRM → країна в адресі
    (або підписі листа) → домен email (.de, .ua…); не визначено — «Валюта» з налаштувань."""
    from shop.countries import COUNTRIES, name

    from .models import OfferSettings
    s = OfferSettings.get()
    if not s.currency_by_country:
        return {"currency": s.default_currency, "country": "", "reason": "валюта з «Налаштувань Angebote»"}
    country, how = "", ""
    if customer is not None and (customer.country or "").upper() in COUNTRIES:
        country, how = customer.country.upper(), "країна в картці клієнта CRM"
    if not country:
        from .services import customer_address
        country = _country_in_text(address or customer_address(customer))
        how = "країна в адресі клієнта" if country else ""
    if not country:
        country = _country_of_email(email or getattr(customer, "email", ""))
        how = f"домен email .{country.lower()}" if country else ""
    if not country:
        return {"currency": s.default_currency, "country": "",
                "reason": "країну клієнта не визначено — валюта з «Налаштувань Angebote»"}
    europe = is_europe(country)
    cur = s.currency_europe if europe else s.currency_other
    return {"currency": cur, "country": country,
            "reason": f"{name(country)} ({how}) → {'Європа' if europe else 'поза Європою'}: {cur}"}


# ── Ціна позиції ─────────────────────────────────────────────────────────────

def dk_listing(product=None, sku=""):
    """Наш офер на DigiKey Marketplace: лістинг товару або лістинг із тим самим Supplier SKU."""
    try:
        from bots.models import DigiKeyListing
    except ImportError:
        return None
    if product is not None:
        listing = DigiKeyListing.objects.filter(product=product).first()
        if listing is not None:
            return listing
    sku = (sku or getattr(product, "sku", "") or "").strip()
    return DigiKeyListing.objects.filter(dk_supplier_sku__iexact=sku).first() if sku else None


def dk_currency() -> str:
    try:
        from bots.models import DigiKeyConfig
        return (DigiKeyConfig.get().locale_currency or "USD").upper()
    except Exception:  # noqa: BLE001 — немає налаштувань DigiKey
        return "USD"


def _dk_when(listing) -> str:
    from django.utils import timezone
    when = listing.last_synced_at or listing.updated_at
    return f", дані від {timezone.localtime(when):%d.%m.%Y}" if when else ""


def _stock_price(product, qty, currency, sku="") -> PriceQuote:
    from offers.models import OfferLine
    if product is None:
        return PriceQuote(None, OfferLine.MANUAL, "")
    misses = []
    try:
        from shop.models import Shop, ShopListing
        from shop.services import offer_percent, price_breaks
        shop = Shop.objects.filter(is_default=True, is_active=True).first()
        listing = ShopListing.objects.filter(shop=shop, product=product).select_related("shop", "product") \
            .first() if shop else None
        if listing is not None:
            reached = _reached([(t["min_qty"], t["unit_price"]) for t in price_breaks(listing)
                                if t["unit_price"] is not None], qty)
            if reached:
                pct = offer_percent(listing)
                price, how = _in(currency, reached[1], shop.currency)
                note = f"Склад Minerva · магазин «{shop.name}»: від {reached[0]} шт" + \
                       (f", акція −{_num(pct)}%" if pct else "") + how
                if price is not None:
                    return PriceQuote(price, OfferLine.SHOP, note)
                misses.append(note)
    except ImportError:
        pass

    if product.sale_price is not None:
        price, how = _in(currency, product.sale_price, home_currency())
        note = "Склад Minerva · ціна продажу товару" + how
        if price is not None:
            return PriceQuote(price, OfferLine.SALE_PRICE, note)
        misses.append(note)

    from inventory.services.base_prices import tiers as base_tiers
    reached = _reached(base_tiers(product), qty)
    if reached:
        price, how = _in(currency, reached[1], product.base_price_currency or home_currency())
        note = f"Склад Minerva · базова ціна від {reached[0]} шт" + how
        if price is not None:
            return PriceQuote(price, OfferLine.BASE, note)
        misses.append(note)
    return PriceQuote(None, OfferLine.MANUAL, "; ".join(misses) or "На складі Minerva ціни немає")


def _digikey_price(product, qty, currency, sku="") -> PriceQuote:
    from offers.models import OfferLine
    listing = dk_listing(product, sku)
    if listing is None:
        return PriceQuote(None, OfferLine.MANUAL, "")
    tiers = []
    for t in listing.dk_prices or []:
        try:
            tiers.append((Decimal(str(t["qty"])), Decimal(str(t["price"]))))
        except (KeyError, TypeError, ArithmeticError):
            continue
    if not tiers:
        return PriceQuote(None, OfferLine.MANUAL, "DigiKey Marketplace: у офера немає цін")
    reached = _reached(tiers, qty)
    below = reached is None          # менше першого ступеня DigiKey — ціна першого ступеня з приміткою
    reached = reached or min(tiers)
    price, how = _in(currency, reached[1], dk_currency())
    note = f"DigiKey Marketplace: від {_num(reached[0])} шт" + how + _dk_when(listing) + \
        (f" (DigiKey продає від {_num(reached[0])} шт)" if below else "") + \
        ("" if listing.dk_is_active else " · офер неактивний")
    return PriceQuote(price, OfferLine.DIGIKEY if price is not None else OfferLine.MANUAL, note)


PRODUCTION_NOTE = "🏭 Немає в каталозі Minerva і на DigiKey — схоже на виробництво / розробку: ціну введіть вручну"


def quote_price(product, qty, currency, mode="auto", sku="") -> PriceQuote:
    """Ціна за одиницю для кількості qty у валюті Angebot за способом mode (auto / stock / digikey / manual)."""
    from offers.models import OfferLine
    qty = Decimal(str(qty or 1))
    currency = (currency or "").upper()
    if mode == "manual":
        return PriceQuote(None, OfferLine.MANUAL, "Ціни — вручну («Ціни з: вручну»)")
    steps = {"stock": (_stock_price,), "digikey": (_digikey_price, _stock_price)}.get(
        mode, (_stock_price, _digikey_price))
    misses = []
    for step in steps:
        q = step(product, qty, currency, sku)
        if q.unit_price is not None:
            return q
        if q.note:
            misses.append(q.note)
    if product is None and dk_listing(None, sku) is None:
        return PriceQuote(None, OfferLine.MANUAL, PRODUCTION_NOTE)
    return PriceQuote(None, OfferLine.MANUAL, "; ".join(misses) or "Ціни в Minerva немає — введіть вручну")


# ── Строк поставки ───────────────────────────────────────────────────────────

def _days(n, language) -> str:
    return DAYS_TEXT.get(language, DAYS_TEXT["en"]).format(n=n)


def _lead_stock(product, qty, language, sku="") -> LeadQuote:
    if product is None:
        return LeadQuote("", "stock", "")
    from inventory.services.stock import get_available
    avail = get_available(product)
    if avail >= qty:
        return LeadQuote(STOCK_TEXT.get(language, STOCK_TEXT["en"]), "stock",
                         f"Склад Minerva: доступно {_num(avail)} шт")
    return LeadQuote("", "stock", f"на складі {_num(max(avail, 0))} з {_num(qty)} шт")


def _lead_product(product, qty, language, sku="") -> LeadQuote:
    if product is None or not getattr(product, "lead_time_days", None):
        return LeadQuote("", "product", "")
    return LeadQuote(_days(product.lead_time_days, language), "product",
                     f"Картка товару: термін поставки {product.lead_time_days} днів")


def _lead_digikey(product, qty, language, sku="") -> LeadQuote:
    listing = dk_listing(product, sku)
    if listing is None or not listing.dk_lead_time_days:
        return LeadQuote("", "digikey", "")
    return LeadQuote(_days(listing.dk_lead_time_days, language), "digikey",
                     f"DigiKey Marketplace: відвантаження за {listing.dk_lead_time_days} днів")


def quote_lead(product, qty, language, mode="auto", sku="") -> LeadQuote:
    """Строк поставки для кількості qty мовою документа за способом mode (auto / product / digikey / manual)."""
    if mode == "manual":
        return LeadQuote("", "manual", "Строк — вручну («Строки з: вручну»)")
    qty = Decimal(str(qty or 1))
    steps = {"product": (_lead_product, _lead_digikey), "digikey": (_lead_digikey, _lead_product)}.get(
        mode, (_lead_stock, _lead_product, _lead_digikey))
    notes = []
    for step in steps:
        q = step(product, qty, language, sku)
        if q.text:
            if notes:
                q.note += f" ({'; '.join(notes)})"
            return q
        if q.note:
            notes.append(q.note)
    return LeadQuote("", "manual", "; ".join(notes + ["строку немає — введіть вручну"]))


# ── Суми ─────────────────────────────────────────────────────────────────────

def line_total(qty, unit_price, discount_percent) -> Decimal:
    if unit_price is None:
        return Decimal("0.00")
    gross = Decimal(str(qty)) * Decimal(str(unit_price))
    k = (HUNDRED - Decimal(str(discount_percent or 0))) / HUNDRED
    return (gross * k).quantize(Q2, rounding=ROUND_HALF_UP)


def totals(line_totals, shipping_cost, vat_rate) -> dict:
    net = sum((Decimal(str(t)) for t in line_totals), Decimal("0")) + money(shipping_cost)
    net = net.quantize(Q2, rounding=ROUND_HALF_UP)
    vat = (net * Decimal(str(vat_rate or 0)) / HUNDRED).quantize(Q2, rounding=ROUND_HALF_UP)
    return {"net": net, "vat": vat, "gross": net + vat}
