"""💶 Порівняння пропозицій запиту постачальникам (RFQ) — так, щоб порівнювати чесно й бачити, що неповне.

  table(req) → {rows, qtys, home, fx, missing_fx, needed, counts, unparsed}:
    колонки — кількості із запиту (5 / 2500 / 10000 …); у клітинці — ціна за 1 шт. разом з разовими витратами
    (оснастка, трафарет, NRE, test jig — `SupplierRequestRecipient.one_time`) у валюті Minerva, разом за кількість
    і ціна постачальника як є; 🥇 — найдешевше для цієї кількості серед порівнюваних, інші — «+N %»;
    ⚠️ неповні (gaps): немає ціни на частину кількостей, у ціні немає того, що просили (лише плати замість PCBA —
    `includes` проти `needed`), немає курсу валюти — у 🥇 не беруть участі; «не вказано» (notes) — строк,
    доставка, термін дії — видно, але порівнянню не заважає;
    курс — «Курси валют» Minerva, немає — довідковий ЄЦБ (core/fx_online.py), джерело видно над таблицею.
  comparison(req) — лише рядки (старі ключі price_home / total_home / all_in / cheapest / fastest теж є).
  fingerprint(req), stale(req) — чи «🏆 Порада AI» складена до теперішніх цін.
  money(d) / price(d) — числа однаково по всій сторінці.
"""
import datetime
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils.formats import number_format

# що входить у ціну: ключ, значок, назва
SCOPE = [("pcb", "🟩", "плати"), ("assembly", "🔧", "монтаж"), ("components", "🔩", "компоненти")]
SCOPE_KEYS = [k for k, _i, _n in SCOPE]
# форма запиту → що мало бути в ціні
NEEDED = {"pcb": ("pcb",), "pcba": ("assembly",), "pcb_pcba": ("pcb", "assembly"), "components": ("components",)}
NEAR = (Decimal("0.8"), Decimal("1.25"))   # ціна на 3000 шт замість 2500 — «≈», далі — «немає ціни»
ONLINE_FX = True                          # тести вимикають (без мережі)
CENT = Decimal("0.01")


def dec(value):
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def _fmt(d: Decimal, places: int) -> str:
    q = d.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    shown = min(places, max(2, -q.normalize().as_tuple().exponent))
    return number_format(q, decimal_pos=shown, use_l10n=True, force_grouping=True)


def price(d) -> str:
    """Ціна за шт.: 246,20 · 0,574 · 0,0123 (до 4 знаків для дрібних, зайві нулі прибрано)."""
    if d is None:
        return ""
    d = Decimal(d)
    return _fmt(d, 2 if abs(d) >= 100 else 4)


def money(d) -> str:
    """Сума: 1 231,00."""
    if d is None:
        return ""
    return number_format(Decimal(d).quantize(CENT, ROUND_HALF_UP), decimal_pos=2, use_l10n=True,
                         force_grouping=True)


def scope_text(keys) -> str:
    return " · ".join(f"{i} {n}" for k, i, n in SCOPE if k in (keys or ()))


def needed(req) -> tuple:
    return NEEDED.get(req.template or "", ())


def clean_includes(values) -> list:
    return [k for k in SCOPE_KEYS if k in set(values or ())]


def clean_one_time(items) -> list:
    """[{name, amount, qty}] з AI / форми → перевірене (сума ≥ 0, qty — ціле > 0 або None)."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        amount = dec(it.get("amount"))
        if amount is None:
            continue
        try:
            qty = int(Decimal(str(it.get("qty")))) if it.get("qty") not in (None, "", 0, "0") else None
        except (InvalidOperation, ValueError, TypeError):
            qty = None
        out.append({"name": " ".join(str(it.get("name") or "").split())[:80] or "Разово",
                    "amount": str(amount.quantize(CENT)), "qty": qty if qty and qty > 0 else None})
    return out[:12]


def parse_one_time(text) -> list:
    """«Stencil 30; Test jig 200; NRE 150 @2500» → разові витрати (@N — лише для кількості N)."""
    import re
    out = []
    for part in re.split(r"[;\n]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)[\s:=]*([\d\s]+(?:[.,]\d+)?)\s*(?:@\s*(\d+))?\s*$", part)
        if not m or dec(m.group(2)) is None:
            from suppliers.services.rfq import RFQError
            raise RFQError(f"Разові витрати: «{part}» — напишіть «назва сума», напр. «Stencil 30».")
        out.append({"name": m.group(1).strip(" :-") or "Разово", "amount": m.group(2), "qty": m.group(3)})
    return clean_one_time(out)


def one_time_text(items) -> str:
    return "; ".join(f"{it['name']} {it['amount']}" + (f" @{it['qty']}" if it.get("qty") else "") for it in items or [])


def parse_tiers(text) -> list:
    """«5: 246,2; 2500: 15,362 / 14 дн» → ступені [{qty, unit_price, lead_days}]."""
    import re
    out = []
    for part in re.split(r"[;\n]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d[\d\s.]*)\s*(?:шт|pcs|pc)?\.?\s*[:=×x]\s*([\d\s]+(?:[.,]\d+)?)\s*(?:/\s*(\d+)\s*\S*)?$",
                     part, re.I)
        if not m:
            from suppliers.services.rfq import RFQError
            raise RFQError(f"Ціни за кількістю: «{part}» — пишіть «кількість: ціна», напр. «2500: 15,36».")
        qty = int(m.group(1).replace(" ", "").replace(".", ""))
        unit = dec(m.group(2))
        if qty > 0 and unit is not None:
            t = {"qty": qty, "unit_price": str(unit.quantize(Decimal("0.0001")))}
            if m.group(3):
                t["lead_days"] = int(m.group(3))
            out.append(t)
    return sorted(out, key=lambda t: t["qty"])[:12]


def tiers_text(tiers) -> str:
    return "; ".join(f"{t['qty']}: {t['unit_price']}" + (f" / {t['lead_days']} дн" if t.get("lead_days") else "")
                     for t in tiers or [] if isinstance(t, dict))


class FX:
    """Курс валюти → валюта Minerva: «Курси валют» Minerva, немає — довідковий ЄЦБ; що взято — used."""

    def __init__(self, home):
        from sales.revenue import Rates
        self.home = home
        self.rates = Rates(home)
        self.used = {}
        self._ecb = None

    def rate(self, currency):
        cur = (currency or "").strip().upper() or self.home
        if cur == self.home:
            return Decimal(1)
        if cur not in self.used:
            r, src = self.rates.rate(cur), "«Курси валют» Minerva"
            if r is None and ONLINE_FX:
                from core import fx_online
                if self._ecb is None:
                    self._ecb = fx_online.latest() or {}
                r = fx_online.cross(self._ecb, cur, self.home) if self._ecb else None
                src = f"довідковий курс ЄЦБ на {self._ecb.get('date', '')}" if r is not None else ""
            self.used[cur] = (r, src)
        return self.used[cur][0]

    def notes(self) -> list:
        return [{"cur": c, "rate": price(r), "source": s} for c, (r, s) in sorted(self.used.items()) if r is not None]

    def missing(self) -> list:
        return [c for c, (r, _s) in sorted(self.used.items()) if r is None]


def _tiers(r, qtys) -> list:
    out = []
    for t in r.tiers or []:
        if not isinstance(t, dict):
            continue
        unit = dec(t.get("unit_price"))
        try:
            qty = int(Decimal(str(t.get("qty"))))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if qty > 0 and unit is not None:
            lead = t.get("lead_days")
            out.append({"qty": qty, "unit": unit, "lead": lead if isinstance(lead, int) and lead >= 0 else None})
    if not out and (r.price is not None or r.total is not None):    # лише «ціна за шт.» / «сума» — на першу кількість
        q = qtys[0] if qtys else 1
        unit = r.price if r.price is not None else (r.total / q)
        out.append({"qty": q, "unit": Decimal(unit), "lead": None})
    return sorted(out, key=lambda t: t["qty"])


def _tier_for(tiers, q):
    exact = next((t for t in tiers if t["qty"] == q), None)
    if exact:
        return exact
    near = [t for t in tiers if NEAR[0] <= Decimal(t["qty"]) / q <= NEAR[1]]
    return min(near, key=lambda t: abs(t["qty"] - q)) if near else None


def _one_time_sum(items, q) -> Decimal:
    return sum((Decimal(it["amount"]) for it in items if not it.get("qty") or it["qty"] == q), Decimal(0))


def table(req) -> dict:
    from sales.revenue import home_currency
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services.overview import purchases, requests_stats, score
    from suppliers.services.rfq import parse_quantities
    home = home_currency()
    fx = FX(home)
    need = needed(req)
    recips = list(req.recipients.select_related("supplier", "contact", "reply_message").order_by("pk"))
    qtys = parse_quantities(req.quantities)
    if not qtys:                     # кількостей у запиті немає — колонки з того, що назвали постачальники
        seen = sorted({t["qty"] for r in recips for t in _tiers(r, [])})
        qtys = seen[:6]
    rows = []
    for r in recips:
        tiers = _tiers(r, qtys)
        items = clean_one_time(r.one_time)
        priced = bool(tiers) and r.state != R.DECLINED
        rate = fx.rate(r.currency) if priced else None
        cells, missing, approx = [], [], []
        for q in qtys:
            t = _tier_for(tiers, q) if priced else None
            if t is None:
                cells.append({"qty": q, "missing": True})
                missing.append(q)
                continue
            ot = _one_time_sum(items, q)
            total = (t["unit"] * q + ot).quantize(CENT, ROUND_HALF_UP)
            eff = total / q
            cell = {"qty": q, "tier_qty": t["qty"], "exact": t["qty"] == q, "unit": t["unit"], "one_time": ot,
                    "total": total, "eff": eff, "lead": t["lead"] if t["lead"] is not None else r.lead_days,
                    "unit_text": price(t["unit"]), "total_text": money(total), "one_time_text": money(ot) if ot else "",
                    "eff_text": price(eff)}
            if rate is not None:
                cell["eff_home"] = eff * rate
                cell["total_home"] = (total * rate).quantize(CENT, ROUND_HALF_UP)
                cell["eff_home_text"] = price(cell["eff_home"])
                cell["total_home_text"] = money(cell["total_home"])
            if not cell["exact"]:
                approx.append(f"ціна на {t['qty']} шт замість {q}")
            cells.append(cell)
        includes = clean_includes(r.includes)
        lacks = [n for k, _i, n in SCOPE if k in need and includes and k not in includes]
        gaps, notes = [], []
        if priced:
            if missing and len(missing) < len(qtys):
                gaps.append("немає ціни на " + ", ".join(f"{q:,}".replace(",", " ") for q in missing) + " шт")
            if lacks:
                gaps.append("у ціні немає: " + ", ".join(lacks))
            elif need and not includes:      # не знаємо, чи це те саме — у 🥇 не беремо (голі плати проти PCBA)
                gaps.append("не сказано, що входить у ціну (просили: " + scope_text(need) + ")")
            if rate is None:
                gaps.append(f"немає курсу {(r.currency or home).upper()} → {home}")
            notes += approx
            if r.lead_days is None and not any(c.get("lead") is not None for c in cells):
                notes.append("строк")
            if r.shipping is None:
                notes.append("доставка")
            if r.valid_until is None:
                notes.append("термін дії")
        unknown_scope = bool(need) and not includes
        comparable = priced and not lacks and not unknown_scope and rate is not None and len(missing) < len(qtys)
        ship_home = (r.shipping * rate).quantize(CENT) if r.shipping is not None and rate is not None else None
        first = next((c for c in cells if not c.get("missing")), None)
        try:
            sc = score(purchases(r.supplier), None, requests_stats(r.supplier)).get("value")
        except Exception:  # noqa: BLE001
            sc = None
        ai = r.ai or {}
        rows.append({
            "recipient": r, "home": home, "cells": cells,
            "one_time": [{**it, "amount": money(dec(it["amount"]))} for it in items],
            "one_time_note": "; ".join(f"{it['name']} {money(dec(it['amount']))}" + (f" (лише {it['qty']} шт)"
                                                                                     if it.get("qty") else "")
                                       for it in items),
            "includes": includes, "includes_text": scope_text(includes), "lacks": lacks, "gaps": gaps,
            "notes": notes, "priced": priced, "comparable": comparable,
            "unparsed": (not priced and r.state in (R.REPLIED, R.QUOTED) and not ai.get("summary")
                         and bool(r.thread_id or r.reply_message_id)),
            "no_price_read": not priced and bool(ai.get("summary")) and r.state != R.DECLINED,
            "rate": rate, "no_rate": priced and rate is None,
            "shipping_text": money(r.shipping) if r.shipping is not None else "",
            "score": sc,
            # старі ключі (панель, проєкт, AI-порада)
            "price_home": first["eff_home"].quantize(CENT) if first and first.get("eff_home") is not None else None,
            "total_home": first.get("total_home") if first else None,
            "all_in": ((first["total_home"] + ship_home) if ship_home is not None else first["total_home"])
            if first and first.get("total_home") is not None else None,
            "price_text": price(r.price) if r.price is not None else "",
        })
    # 🥇 по кожній кількості — серед порівнюваних
    for i, q in enumerate(qtys):
        ok = [row for row in rows if row["comparable"] and row["cells"][i].get("total_home") is not None]
        if not ok:
            continue
        best = min(row["cells"][i]["total_home"] for row in ok)
        for row in ok:
            c = row["cells"][i]
            c["best"] = c["total_home"] == best
            c["over"] = None if c["best"] or not best else int(((c["total_home"] / best - 1) * 100)
                                                              .quantize(Decimal(1), ROUND_HALF_UP))
            c["over_text"] = over_text(c["over"])
    if qtys:
        for row in rows:
            if row["cells"] and row["cells"][0].get("best"):
                row["cheapest"] = True
    # найшвидше — курс не потрібен, але те, що просили, має бути в ціні
    timed = [row for row in rows if row["priced"] and not row["lacks"] and row["recipient"].lead_days is not None]
    if timed:
        min(timed, key=lambda x: x["recipient"].lead_days)["fastest"] = True

    def order(row):
        r = row["recipient"]
        if row["comparable"]:
            first = next((c for c in row["cells"] if c.get("total_home") is not None), None)
            return (0, first["eff_home"] if first else Decimal(0), r.pk)
        if row["priced"]:
            return (1, Decimal(0), r.pk)
        if row["unparsed"] or row["no_price_read"]:
            return (2, Decimal(0), r.pk)
        return (3 if r.state != R.DECLINED else 4, Decimal(0), r.pk)
    rows.sort(key=order)
    counts = {"priced": sum(1 for x in rows if x["priced"]), "comparable": sum(1 for x in rows if x["comparable"]),
              "incomplete": sum(1 for x in rows if x["priced"] and x["gaps"]),
              "unparsed": sum(1 for x in rows if x["unparsed"]),
              "no_price": sum(1 for x in rows if x["no_price_read"]),
              "waiting": sum(1 for x in rows if x["recipient"].state in (R.SENT, R.SILENT))}
    return {"rows": rows, "qtys": qtys, "home": home, "fx": fx.notes(), "missing_fx": fx.missing(),
            "needed": scope_text(need), "counts": counts,
            "unparsed": [x["recipient"].pk for x in rows if x["unparsed"]]}


def over_text(over) -> str:
    """+17 % · +250 % · ×12 — у скільки разів дорожче, коли різниця велика."""
    if over is None:
        return ""
    if over < 300:
        return f"+{over} %"
    times = Decimal(over + 100) / 100
    return "×" + (number_format(times.quantize(Decimal("0.1"), ROUND_HALF_UP), decimal_pos=1, use_l10n=True)
                  if times < 10 else str(int(times.quantize(Decimal(1), ROUND_HALF_UP))))


def comparison(req) -> list:
    return table(req)["rows"]


def fingerprint(req) -> str:
    data = [(r.pk, r.state, str(r.price), str(r.total), r.currency, r.tiers, r.one_time, r.includes, r.lead_days,
             str(r.shipping), str(r.valid_until)) for r in req.recipients.order_by("pk")]
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def stale(req) -> bool:
    """«🏆 Порада AI» складена до теперішніх цін: інший відбиток, а для старих порад — нова відповідь після неї."""
    a = req.analysis or {}
    if not a.get("summary"):
        return False
    if a.get("fp"):
        return a["fp"] != fingerprint(req)
    try:
        at = datetime.datetime.fromisoformat(str(a.get("at") or ""))
    except ValueError:
        return False
    return req.recipients.filter(replied_at__gt=at).exists()


def ai_lines(req) -> tuple:
    """Рядки для 🏆 AI-поради: лише з таблиці (ціни в одній валюті з разовими, що неповне)."""
    t = table(req)
    home, lines = t["home"], []
    for row in t["rows"]:
        r = row["recipient"]
        head = f"- {r.supplier.name}: {r.get_state_display()}"
        if row["unparsed"]:
            lines.append(head + " — ціни ще не розібрано (треба 🧠 прочитати відповідь), не вважати «без ціни»")
            continue
        if not row["priced"]:
            lines.append(head + (f" — цін немає: {(r.ai or {}).get('summary', '')[:300]}" if row["no_price_read"] else ""))
            continue
        cells = []
        for c in row["cells"]:
            if c.get("missing"):
                cells.append(f"{c['qty']} шт: немає ціни")
            else:
                cells.append(f"{c['qty']} шт: {c.get('eff_home_text', '?')} {home}/шт з разовими "
                             f"(ціна {c['unit_text']} {r.currency}/шт" + (f" + разово {c['one_time_text']}" if
                                                                           c["one_time_text"] else "") +
                             (f", ≈ на {c['tier_qty']} шт" if not c["exact"] else "") +
                             (f", строк {c['lead']} дн" if c.get("lead") is not None else "") + ")")
        lines.append(head + f"; у ціні: {row['includes_text'] or 'не сказано'}; " + "; ".join(cells) +
                     (f"; доставка {row['shipping_text']} {r.currency}" if row["shipping_text"] else "") +
                     (f"; діє до {r.valid_until}" if r.valid_until else "") +
                     (f"; умови: {r.quote_note[:300]}" if r.quote_note else "") +
                     (f"; ⚠️ НЕПОВНА: {'; '.join(row['gaps'])}" if row["gaps"] else "") +
                     (f"; оцінка постачальника в Minerva {row['score']}/100" if row["score"] is not None else ""))
    return t, lines
