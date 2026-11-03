"""Базові ціни товару (ступені за кількістю): нормалізація, зміна з історією, імпорт з DigiKey, переведення у
валюту Minerva. Базові ціни тримаються у валюті Minerva (`offers.pricing.home_currency`, EUR): DigiKey, магазин і
Angebote перераховують їх у свою валюту за курсом."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone

_Q = Decimal("0.0001")


def normalize(rows) -> list[dict]:
    """[{min_qty, unit_price}] → відсортовано, без дублікатів, ціни рядками (Decimal)."""
    out = {}
    for r in rows or []:
        try:
            qty = int(r.get("min_qty", r.get("qty")))
            price = Decimal(str(r.get("unit_price", r.get("price")))).quantize(_Q)
        except (TypeError, ValueError, InvalidOperation, AttributeError):
            continue
        if qty >= 1 and price > 0:
            out[qty] = price
    return [{"min_qty": q, "unit_price": format(p.normalize(), "f")} for q, p in sorted(out.items())]


def tiers(product) -> list[tuple[int, Decimal]]:
    """[(кількість, ціна)] базових цін товару."""
    return [(r["min_qty"], Decimal(r["unit_price"])) for r in normalize(product.base_prices)]


def parse_text(text: str) -> list[dict]:
    """«1: 4,89\n10: 4.52» → [{min_qty, unit_price}]. ValueError з номерами помилкових рядків."""
    rows, bad = [], []
    for i, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s*(?:шт\.?|pcs)?\s*[:=;\t ]\s*([\d.,]+)$", line, re.I)
        if not m:
            bad.append(str(i))
            continue
        rows.append({"min_qty": int(m.group(1)), "unit_price": m.group(2).replace(",", ".")})
    if bad:
        raise ValueError("Рядки " + ", ".join(bad) + ": формат «кількість: ціна», напр. «10: 4.52»")
    rows = normalize(rows)
    if rows and rows[0]["min_qty"] != 1:
        raise ValueError("Перший ступінь має бути від 1 шт.")
    return rows


def format_text(rows) -> str:
    return "\n".join(f'{r["min_qty"]}: {r["unit_price"]}' for r in normalize(rows))


def home_currency() -> str:
    from offers.pricing import home_currency as _home
    return _home()


def rate_to_home(src: str, home: str | None = None) -> Decimal | None:
    """Курс src → валюта Minerva (1 src = N home) — той самий, яким ціни Minerva стають цінами DigiKey
    («💲 Налаштування цін DigiKey» → «Курс для цін DigiKey»), тож після переведення ціни на DigiKey не зрушать.
    None — курсу немає."""
    home = (home or home_currency()).upper()
    src = (src or home).upper()
    if src == home:
        return Decimal(1)
    try:
        from bots.services.dk_prices import fx
        return fx(src, home)
    except ImportError:
        from offers.pricing import fx
        return fx(src, home)


def converted(price, rate, cents: bool = False) -> Decimal:
    """Ціна × курс. За замовчуванням — 4 знаки (точність базових цін): тоді зворотний перерахунок для DigiKey дає ту
    саму ціну (268,39 $ → 230,8154 € → 268,39 $; з центами 230,82 € → 268,40 $). `cents` — від 1 до центів."""
    value = Decimal(str(price)) * Decimal(str(rate))
    q = Decimal("0.01") if cents and value >= 1 else _Q
    return value.quantize(q, rounding=ROUND_HALF_UP).normalize()


def rate_text(rate) -> str:
    return format(Decimal(str(rate)).quantize(Decimal("0.000001")).normalize(), "f")


def rate_label(src: str, dst: str, rate) -> str:
    """«1 USD = 0.86 EUR» — що означає курс, без здогадів."""
    return f"1 {src} = {rate_text(rate)} {dst}"


def fx_details(src: str, dst: str, rate, origin: str = "") -> dict:
    """Подробиці переведення для історії (`ProductPriceHistory.details`)."""
    out = {"rate": rate_text(rate), "rate_from": src.upper(), "rate_to": dst.upper()}
    if origin:
        out["rate_origin"] = origin[:60]
    return out


_FX_RE = re.compile(r"^([A-Z]{3})>([A-Z]{3}):(\d+(?:\.\d+)?)(?:@(\d+))?(?::(.{0,60}))?$")


def parse_fx(raw: str) -> dict | None:
    """Позначка конвертера картки «USD>EUR:0.86:ЄЦБ на 30.10.2026» → `fx_details`; «USD>EUR:0.86@<запис>:…» —
    переведено заново з цін до переведення `<запис>` історії (галочка «однаковий курс з DigiKey», `redo_of`
    перевіряє `set_base_prices`); інше — None."""
    m = _FX_RE.match((raw or "").strip())
    if not m:
        return None
    try:
        rate = Decimal(m.group(3))
    except InvalidOperation:
        return None
    if rate <= 0:
        return None
    out = fx_details(m.group(1), m.group(2), rate, (m.group(5) or "").strip())
    if m.group(4):
        out["redo_of"] = int(m.group(4))
    return out


def convert(product, to: str, rate, *, user=None, note: str = "", cents: bool = False, origin: str = "") -> bool:
    """Базові ціни товару → валюта `to` за курсом (1 стара валюта = `rate` нової), з історією
    «Переведено з USD в EUR за курсом 1 USD = 0.86 EUR». `cents` — округлити до центів (ціни DigiKey можуть
    зрушити на цент). `origin` — звідки курс («ЄЦБ на 30.10.2026»). False — немає цін або вже в цій валюті."""
    rows = normalize(product.base_prices)
    src = (product.base_price_currency or "EUR").upper()
    to = (to or "").upper()
    if not rows or src == to:
        return False
    rate = Decimal(str(rate))
    if rate <= 0:
        raise ValueError("Курс має бути більшим за 0")
    new = [{"min_qty": r["min_qty"], "unit_price": format(converted(r["unit_price"], rate, cents), "f")} for r in rows]
    label = (f"Переведено з {src} в {to} за курсом {rate_label(src, to, rate)}" + (f" ({origin})" if origin else "")
             + (", до центів" if cents else ""))
    details = {**fx_details(src, to, rate, origin), **({"cents": True} if cents else {})}
    return set_base_prices(product, new, source="manual", user=user, currency=to, source_label=label[:200],
                           note=(f"{label} · {note}" if note else label)[:255], details=details)


def not_home(qs=None, home: str | None = None):
    """Товари з базовими цінами не у валюті Minerva (список, бо «є ціни» — JSON)."""
    from inventory.models import Product
    home = (home or home_currency()).upper()
    qs = Product.objects.all() if qs is None else qs
    return [p for p in qs.exclude(base_price_currency__iexact=home).order_by("sku") if normalize(p.base_prices)]


def digikey_source_label(listing) -> str:
    parts = ["DigiKey"]
    if listing.dk_offer_id:
        parts.append(f"офер {listing.dk_offer_id}")
    if listing.last_synced_at:
        parts.append("синхр. " + timezone.localtime(listing.last_synced_at).strftime("%d.%m.%Y %H:%M"))
    return " · ".join(parts)


def set_base_prices(product, rows, *, source: str = "manual", user=None, currency: str | None = None,
                    source_label: str | None = None, note: str = "", details: dict | None = None) -> bool:
    """Змінює базові ціни і пише історію. Повертає False, якщо нічого не змінилось.
    `details` — курс переведення / ціни, зчитані з DigiKey (див. `ProductPriceHistory.details`)."""
    from inventory.models import ProductPriceHistory

    new = normalize(rows)
    old = normalize(product.base_prices)
    old_currency = (product.base_price_currency or "").upper()
    currency = (currency or product.base_price_currency or "EUR").upper()
    if new == old and currency == product.base_price_currency:
        return False
    details = {"old_currency": old_currency, **(details or {})}
    redo_of = details.pop("redo_of", None)
    if redo_of:   # «однаковий курс з DigiKey»: ті самі ціни до переведення — заново, іншим курсом
        last = last_conversion(product)
        if last and last["pk"] == redo_of and last["from"] == details.get("rate_from"):
            details["redo"] = {"rate": last["rate"], "prices": last["rows"]}
    if details.get("rate") and details.get("rate_to") != currency:
        details = {k: v for k, v in details.items() if not k.startswith(("rate", "redo"))}   # перевели не в цю валюту
    if details.get("rate") and not source_label and source == "manual":
        source_label = (f"Переведено {'заново ' if details.get('redo') else ''}з {details['rate_from']} в {currency} за курсом "
                        f"{rate_label(details['rate_from'], currency, details['rate'])}"
                        + (f" ({details['rate_origin']})" if details.get("rate_origin") else ""))
    who = (user.get_username() if user is not None and getattr(user, "is_authenticated", False) else "") or "система"
    label = source_label if source_label is not None else (
        f"Змінено вручну: {who}" if source == "manual" else dict(ProductPriceHistory.SOURCE_CHOICES).get(source, source))
    product.base_prices = new
    product.base_price_currency = currency
    product.base_prices_source = label[:200]
    product.base_prices_updated_at = timezone.now()
    product.base_prices_updated_by = who[:150]
    product.save(update_fields=["base_prices", "base_price_currency", "base_prices_source",
                                "base_prices_updated_at", "base_prices_updated_by"])
    ProductPriceHistory.objects.create(
        product=product, user=user if getattr(user, "is_authenticated", False) else None, user_label=who[:150],
        source=source, currency=currency, old_prices=old, new_prices=new, note=(note or label)[:255],
        details=details)
    return True


def import_from_digikey(product, user=None) -> bool | None:
    """Базові ціни ← ціни офера DigiKey (DigiKeyListing.dk_prices), переведені у валюту Minerva за курсом для
    цін DigiKey (`rate_to_home`); курсу немає — у валюті DigiKey з приміткою. None — у DigiKey цін немає."""
    from django.core.exceptions import ObjectDoesNotExist
    try:
        listing = product.dk_listing
    except ObjectDoesNotExist:
        return None
    rows = normalize(listing.dk_prices if listing else [])
    if not rows:
        return None
    try:
        from bots.services.dk_prices import currency as dk_currency   # як «Валюта цін на DigiKey»
        currency = dk_currency() or "USD"
    except Exception:
        currency = "USD"
    currency = currency.upper()
    where = digikey_source_label(listing).removeprefix("DigiKey").lstrip(" ·")
    label = f"Зчитано з DigiKey у {currency}"
    details = {"read": {"from": "DigiKey", "currency": currency, "prices": rows}}
    home = home_currency()
    if currency != home:
        rate = rate_to_home(currency, home)
        if rate:
            origin = _dk_rate_origin()
            rows = [{"min_qty": r["min_qty"], "unit_price": format(converted(r["unit_price"], rate), "f")}
                    for r in rows]
            label += f" · переведено в {home} за курсом {rate_label(currency, home, rate)}"
            details.update(fx_details(currency, home, rate, origin))
            currency = home
        else:
            label += f" · немає курсу {currency} → {home}, лишилось у {currency}"
    label += f" · {where}" if where else ""
    return set_base_prices(product, rows, source="digikey", user=user, currency=currency,
                           source_label=label[:200], details=details)


def last_conversion(product) -> dict | None:
    """Останнє переведення цін товару в їхню теперішню валюту, якщо ціни відтоді не змінювали: з якої валюти, за
    яким курсом (і звідки курс), ціни до переведення. Картка за ним бачить, що переведено не за курсом DigiKey, і
    пропонує перевести заново (`parse_fx` «…@<pk>»). None — останній запис історії не переведення."""
    h = product.price_history.order_by("-created_at", "-pk").first() if product.pk else None
    if h is None:
        return None
    d = h.details or {}
    rate, src = d.get("rate"), (d.get("rate_from") or "").upper()
    cur = (product.base_price_currency or "").upper()
    if (not rate or not src or (d.get("rate_to") or "").upper() != cur or (h.currency or "").upper() != cur
            or normalize(h.new_prices) != normalize(product.base_prices)):
        return None
    redo, read = d.get("redo") or {}, d.get("read") or {}
    if redo.get("prices"):
        before = redo["prices"]
    elif read.get("prices") and (read.get("currency") or "").upper() == src:
        before = read["prices"]
    elif (d.get("old_currency") or "").upper() == src:
        before = h.old_prices
    else:
        return None
    rows = normalize(before)
    if not rows:
        return None
    return {"pk": h.pk, "from": src, "rate": rate_text(rate), "origin": d.get("rate_origin", ""), "rows": rows}


def _dk_rate_origin() -> str:
    try:
        from bots.services.dk_prices import rate_source_text
        return rate_source_text()
    except Exception:  # noqa: BLE001
        return ""


# ── Історія: що в що перевели ────────────────────────────────────────────────

_NOTE_FX_RE = re.compile(r"Переведено (?:заново )?з ([A-Z]{3}) в ([A-Z]{3}) за курсом (?:1 [A-Z]{3} = )?(\d+(?:\.\d+)?)")
_NOTE_DK_FX_RE = re.compile(r"\b([A-Z]{3}) → ([A-Z]{3}) за курсом (\d+(?:\.\d+)?)")   # старий імпорт з DigiKey
_MIGRATED = "Перенесено з цін DigiKey при оновленні"


def _note_rest(h, used: list[re.Pattern]) -> str:
    """Що в примітці є понад заголовок: офер, дата синхронізації, слова людини."""
    note = (h.note or "").strip()
    extra = []
    if note.startswith(_MIGRATED):
        extra.append("перенесено під час оновлення Minerva")
        note = note[len(_MIGRATED):].strip().strip("()")
    parts = []
    for part in note.split(" · "):
        p = part.strip()
        if not p or p == "DigiKey" or p.startswith(("Змінено вручну", "Зчитано з DigiKey", "переведено в ")):
            continue
        if any(rx.search(p) for rx in used):
            if ", до центів" in p:
                parts.append("округлено до центів")
            continue
        parts.append(p)
    return " · ".join(extra + parts)


def describe(h, older_currency: str = "") -> dict:
    """Запис історії → що сталося людською мовою + таблиця «кількість | було | (DigiKey) | стало» з валютами.
    Старі записи (до `details`) — з примітки: «Переведено з USD в EUR за курсом 0.86», «USD → EUR за курсом»,
    «Перенесено з цін DigiKey…»; валюта «було» — валюта попереднього запису (`older_currency`)."""
    d = h.details or {}
    note = h.note or ""
    new_cur = (h.currency or "").upper()
    rate, r_from, r_to = d.get("rate"), d.get("rate_from"), d.get("rate_to")
    read = d.get("read") or {}
    redo = d.get("redo") or {}
    from_dk = h.source == "digikey" or bool(read) or note.startswith(_MIGRATED)
    used = [_NOTE_FX_RE, _NOTE_DK_FX_RE]
    if not rate:
        m = (_NOTE_DK_FX_RE if from_dk else _NOTE_FX_RE).search(note)
        if m:
            r_from, r_to, rate = m.groups()
    if from_dk and not read:
        read = {"currency": r_from if rate else new_cur, "prices": None}
    old_cur = (d.get("old_currency") or (r_from if rate and not from_dk else "") or older_currency or new_cur).upper()
    old = normalize(h.old_prices)
    new = normalize(h.new_prices)
    read_rows = normalize(read.get("prices")) if read.get("prices") and rate else []
    read_cur = (read.get("currency") or "").upper()

    if from_dk:
        icon, title = "📥", f"Зчитано з DigiKey у {read_cur or new_cur}"
        if rate:
            title += f" → переведено в {r_to}"
        elif read_cur and read_cur != new_cur:
            title += f", лишилось у {new_cur}"
    elif rate:
        icon, title = "💱", f"Переведено {'заново ' if redo else ''}з {r_from} в {r_to}"
    else:
        icon = {"api": "🔌", "migration": "📦"}.get(h.source, "✏️")
        title = h.get_source_display() if h.source != "manual" else "Змінено вручну"
        if old and old_cur != new_cur:
            title += f" · валюта {old_cur} → {new_cur} без перерахунку цін"
    rate_line = ""
    if rate:
        try:
            inverse = (Decimal(1) / Decimal(str(rate))).quantize(_Q)
            rate_line = f"{rate_label(r_from, r_to, rate)} · {rate_label(r_to, r_from, inverse)}"
        except (InvalidOperation, ZeroDivisionError):
            rate_line = rate_label(r_from, r_to, rate)
        if d.get("rate_origin"):
            rate_line += f" · курс: {d['rate_origin']}"

    by_old = {r["min_qty"]: r["unit_price"] for r in old}
    by_new = {r["min_qty"]: r["unit_price"] for r in new}
    by_read = {r["min_qty"]: r["unit_price"] for r in read_rows}
    by_redo = {r["min_qty"]: r["unit_price"] for r in (normalize(redo.get("prices")) if rate else [])}
    qtys = sorted(set(by_old) | set(by_new) | set(by_read) | set(by_redo))
    same_cur = old_cur == new_cur
    lines = []
    if by_redo:   # переведено заново: з тих самих цін, що й попереднє переведення
        lines.append({"label": f"До переведення, {r_from}", "cells": [{"v": by_redo.get(q, "—")} for q in qtys]})
    if old:
        lines.append({"label": f"Було, {old_cur}", "cells": [{"v": by_old.get(q, "—")} for q in qtys]})
    if read_rows:
        lines.append({"label": f"DigiKey, {read_cur}", "cells": [{"v": by_read.get(q, "—")} for q in qtys]})
    lines.append({"label": f"Стало, {new_cur}", "strong": True, "cells": [
        {"v": by_new.get(q, "—"),
         "changed": bool(old) and same_cur and by_old.get(q) != by_new.get(q)} for q in qtys]})
    note = _note_rest(h, used)
    if by_redo and redo.get("rate"):
        note = " · ".join(filter(None, [f"раніше переведено за курсом {rate_label(r_from, r_to, redo['rate'])}", note]))
    return {"icon": icon, "title": title, "rate": rate_line, "qtys": qtys, "lines": lines,
            "note": note, "source": h.get_source_display()}


def history(product, limit: int = 10) -> list[dict]:
    """Останні зміни базових цін для картки товару (найновіші першими), кожна — `describe` + коли / хто."""
    items = list(product.price_history.all()[:limit + 1])
    out = []
    for i, h in enumerate(items[:limit]):
        older = items[i + 1].currency if i + 1 < len(items) else ""
        out.append({**describe(h, older), "when": timezone.localtime(h.created_at), "who": h.user_label or "—"})
    return out


# ── 💱 Переведення багатьох товарів у фоні (сторінка «💱 Базові ціни → EUR») ────────────────────
# Кожне збереження перераховує позиції магазинів і шле вебхук «stock.changed» сайту, тож 150 товарів по одному
# тримали сторінку хвилину. Тепер — у фоні, пачками по CONVERT_CHUNK в одній транзакції (одна подія вебхука на
# пачку), хід — у core.shared_cache (convert_state), сторінка показує смугу.

logger = logging.getLogger(__name__)
CONVERT_KEY, CONVERT_LOCK = "base-currency-convert", "base-currency-convert-lock"
CONVERT_CHUNK = 20
CONVERT_STALE = 180                   # стільки секунд без ходу — переведення вважається зупиненим
CONVERT_LIST_MAX = 50                 # пропущені й помилки в стані — перші N
BACKGROUND = True


def _cache():
    from core.shared_cache import cache
    return cache


def _save_state(st):
    _cache().set(CONVERT_KEY, st, 7 * 24 * 3600)


def convert_state() -> dict:
    """Хід переведення: {running, total, done, converted, skipped, errors, home, by, started, beat, finished,
    stalled}; {} — ще не запускали."""
    st = dict(_cache().get(CONVERT_KEY) or {})
    if st.get("running"):
        try:
            age = (timezone.now() - datetime.fromisoformat(st.get("beat") or st["started"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = CONVERT_STALE + 1
        if age > CONVERT_STALE:                 # процес перезапустили посеред роботи — можна почати знову
            st.update(running=False, stalled=True)
    return st


def start_convert(plan, home: str, *, user=None, note: str = "", cents: bool = False) -> bool:
    """plan — [(pk товару, курс, звідки курс)]. Переведення у фоні; False — уже йде інше."""
    if not _cache().add(CONVERT_LOCK, 1, 3600):
        if not convert_state().get("stalled"):
            return False
        _cache().set(CONVERT_LOCK, 1, 3600)
    plan = [(int(pk), str(rate), (origin or "")[:60]) for pk, rate, origin in plan]
    now = timezone.now().isoformat()
    who = (user.get_username() if user is not None and getattr(user, "is_authenticated", False) else "") or ""
    _save_state({"running": True, "total": len(plan), "done": 0, "converted": 0, "skipped": [], "errors": [],
                 "home": home.upper(), "by": who, "started": now, "beat": now, "finished": ""})
    args = (plan, home.upper(), getattr(user, "pk", None), note, cents)
    if not BACKGROUND:
        _convert_thread(*args, close=False)
        return True
    threading.Thread(target=_convert_thread, args=args, daemon=True, name="MinervaBaseCurrency").start()
    return True


def _convert_thread(plan, home, user_id, note, cents, close=True):
    from django.db import connection
    try:
        _convert_run(plan, home, user_id, note, cents)
    except Exception as exc:  # noqa: BLE001 — стан має закритись, інакше сторінка чекатиме вічно
        logger.exception("Base prices → %s", home)
        st = convert_state()
        st.update(running=False, finished=timezone.now().isoformat(), error=str(exc)[:300])
        _save_state(st)
    finally:
        _cache().delete(CONVERT_LOCK)
        if close:
            connection.close()


def _convert_run(plan, home, user_id, note, cents):
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from inventory.models import Product
    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    st = convert_state()
    for i in range(0, len(plan), CONVERT_CHUNK):
        chunk = plan[i:i + CONVERT_CHUNK]
        products = {p.pk: p for p in Product.objects.filter(pk__in=[pk for pk, _, _ in chunk])}
        with transaction.atomic():              # вебхук «stock.changed» — одна подія на всю пачку
            for pk, rate, origin in chunk:
                p = products.get(pk)
                try:
                    with transaction.atomic():  # один товар з помилкою не відкочує пачку
                        done = bool(p) and convert(p, home, Decimal(rate), user=user, note=note, cents=cents,
                                                   origin=origin)
                except Exception as exc:  # noqa: BLE001
                    if len(st["errors"]) < CONVERT_LIST_MAX:
                        st["errors"].append([p.sku if p else str(pk), str(exc)[:200]])
                    continue
                if done:
                    st["converted"] += 1
                elif len(st["skipped"]) < CONVERT_LIST_MAX:
                    st["skipped"].append(p.sku if p else str(pk))
        st.update(done=min(i + len(chunk), len(plan)), beat=timezone.now().isoformat())
        _save_state(st)
    st.update(running=False, finished=timezone.now().isoformat())
    _save_state(st)
