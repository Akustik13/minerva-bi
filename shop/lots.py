"""Мінімальний лот і дозволені кількості в інтернет-магазинах.

Правило (`QtyRule`): мінімальне замовлення, крок від мінімуму або явний список кількостей.
- Картка товару (`Product.shop_min_qty`, `shop_qty_step`, `shop_qty_choices`) — за замовчуванням для всіх магазинів.
- Позиція магазину (`ShopListing.min_qty`, `qty_step`, `qty_choices`) — своє правило для цього магазину, якщо задано
  хоч одне з полів (тоді правило товару не діє зовсім; «1» у мінімумі — будь-яка кількість саме тут).

Крок рахується від мінімуму: мін. 1500 і крок 1000 → 1500, 2500, 3500… Список кількостей сильніший за мінімум і крок.
Ціни для покупця (`cut`): ступені нижче лоту не публікуються, перша ціна — та, що діє на лот;
зі списком — ціна кожної кількості списку. Замовлення сайту з іншою кількістю відхиляє `problem`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_CHOICES = 20        # кількостей у списку «Лише ці кількості»
MAX_QTY = 10_000_000    # розумна межа для лоту й кроку
EXAMPLES = 4            # скільки дозволених кількостей показувати в підказках

SOURCE_LISTING, SOURCE_PRODUCT, SOURCE_NONE = "listing", "product", "none"


@dataclass(frozen=True)
class QtyRule:
    min_qty: int = 1
    step: int = 1
    choices: tuple = field(default_factory=tuple)
    source: str = SOURCE_NONE

    @property
    def first(self) -> int:
        """Найменша кількість, яку можна замовити."""
        return self.choices[0] if self.choices else self.min_qty

    @property
    def any(self) -> bool:
        """Без обмежень: будь-яка кількість від 1 шт."""
        return not self.choices and self.min_qty <= 1 and self.step <= 1

    def allowed(self, qty) -> bool:
        return self.problem(qty) is None

    def next_allowed(self, qty) -> int | None:
        """Найменша дозволена кількість ≥ qty (None — більшої в списку немає)."""
        q = _dec(qty)
        if self.choices:
            return next((c for c in self.choices if c >= q), None)
        if q <= self.min_qty:
            return self.min_qty
        if self.step <= 1:
            return int(q) if q == int(q) else int(q) + 1
        k = (q - self.min_qty) / self.step
        k = int(k) if k == int(k) else int(k) + 1
        return self.min_qty + k * self.step

    def prev_allowed(self, qty) -> int | None:
        """Найбільша дозволена кількість ≤ qty (None — менших немає)."""
        q = _dec(qty)
        if self.choices:
            below = [c for c in self.choices if c <= q]
            return below[-1] if below else None
        if q < self.min_qty:
            return None
        if self.step <= 1:
            return int(q)
        return self.min_qty + int((q - self.min_qty) / self.step) * self.step

    def examples(self, n: int = EXAMPLES) -> list[int]:
        """Перші дозволені кількості: [1000, 2000, 3000, 4000]."""
        if self.choices:
            return list(self.choices[:n])
        step = max(1, self.step)
        return [self.min_qty + i * step for i in range(n)]

    def problem(self, qty) -> dict | None:
        """None — кількість дозволена; інакше {code, error, min_qty, qty_step, qty_choices, nearest} для API."""
        q = _dec(qty)
        if self.any:
            return None
        info = {"min_qty": self.first, "qty_step": self.step if not self.choices else None,
                "qty_choices": list(self.choices)}
        if self.choices:
            if q in self.choices:
                return None
            return {"code": "qty_not_in_choices",
                    "error": f"Можна замовити лише {_list(self.choices)} шт.",
                    **info, "nearest": self._nearest(q)}
        if q < self.min_qty:
            return {"code": "below_min_qty", "error": f"Мінімальне замовлення — {_n(self.min_qty)} шт.",
                    **info, "nearest": [self.min_qty]}
        if self.step > 1 and (q - self.min_qty) % self.step:
            return {"code": "qty_not_in_step",
                    "error": f"Кількість — від {_n(self.min_qty)} шт. кроком {_n(self.step)} "
                             f"({_list(self.examples(3))} …)",
                    **info, "nearest": self._nearest(q)}
        return None

    def _nearest(self, q) -> list[int]:
        out = [v for v in (self.prev_allowed(q), self.next_allowed(q)) if v is not None]
        return sorted(set(out))

    def text(self) -> str:
        """Людською мовою: «лише 1000 / 5000 / 10 000 шт.», «від 1000 шт. кроком 1000», «від 500 шт.», ''."""
        if self.choices:
            return f"лише {' / '.join(_n(c) for c in self.choices)} шт."
        if self.any:
            return ""
        if self.step > 1:
            return f"від {_n(self.min_qty)} шт. кроком {_n(self.step)}"
        return f"від {_n(self.min_qty)} шт."

    def short(self) -> str:
        """Для колонок і бейджів: «≥1000 ×1000», «1000 / 5000», ''."""
        if self.choices:
            return " / ".join(_n(c) for c in self.choices[:4]) + (" …" if len(self.choices) > 4 else "")
        if self.any:
            return ""
        return f"≥{_n(self.min_qty)}" + (f" ×{_n(self.step)}" if self.step > 1 else "")


NO_RULE = QtyRule()


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _n(v) -> str:
    """1000 → «1 000» (вузький нерозривний пробіл — як пише людина й не ламає рядок)."""
    return f"{int(v):,}".replace(",", " ")


def _list(values) -> str:
    return ", ".join(_n(v) for v in values)


def make(min_qty, step, choices, source: str) -> QtyRule:
    choices = tuple(sorted({int(c) for c in (choices or []) if _positive(c)}))
    return QtyRule(min_qty=max(1, int(min_qty or 1)), step=max(1, int(step or 1)), choices=choices, source=source)


def _positive(v) -> bool:
    try:
        return int(v) >= 1
    except (TypeError, ValueError):
        return False


def has_own(listing) -> bool:
    """У позиції своє правило (хоч одне поле задано)."""
    return listing.min_qty is not None or listing.qty_step is not None or bool(listing.qty_choices)


def product_rule(product) -> QtyRule:
    rule = make(getattr(product, "shop_min_qty", 1), getattr(product, "shop_qty_step", 1),
                getattr(product, "shop_qty_choices", None), SOURCE_PRODUCT)
    return NO_RULE if rule.any else rule


def rule_for(listing) -> QtyRule:
    """Правило позиції магазину: своє (якщо задано) → з картки товару → без обмежень."""
    if listing is None:
        return NO_RULE
    if has_own(listing):
        rule = make(listing.min_qty, listing.qty_step, listing.qty_choices, SOURCE_LISTING)
        return QtyRule(source=SOURCE_LISTING) if rule.any else rule
    return product_rule(listing.product)


# ── Ціни для покупця ─────────────────────────────────────────────────────────

def _price_at(rows, qty):
    price = None
    for r in rows:
        if qty >= r["min_qty"]:
            price = r["unit_price"]
    return price


def cut(rows: list[dict], rule: QtyRule) -> list[dict]:
    """Ступені [{min_qty, unit_price}] → як бачить покупець з лотом.

    Нижче лоту — нічого; перший рядок — ціна, що діє на лот. Ступінь між дозволеними кількостями
    (крок) — від найближчої дозволеної кількості. Зі списком — по рядку на кожну кількість списку."""
    if not rows or rule.any:
        return list(rows)
    rows = sorted(rows, key=lambda r: r["min_qty"])
    if rule.choices:
        out = []
        for c in rule.choices:
            p = _price_at(rows, c)
            if p is not None:
                out.append({"min_qty": c, "unit_price": p})
        return out
    by_qty: dict[int, object] = {}
    first = _price_at(rows, rule.min_qty)
    if first is not None:
        by_qty[rule.min_qty] = first
    for r in rows:
        if r["min_qty"] <= rule.min_qty:
            continue
        q = rule.next_allowed(r["min_qty"])
        by_qty[q] = _price_at(rows, q)      # кілька ступенів на ту саму кількість — діє найвищий
    return [{"min_qty": q, "unit_price": p} for q, p in sorted(by_qty.items()) if p is not None]


def hidden_rows(rows: list[dict], rule: QtyRule) -> list[dict]:
    """Ступені нижче лоту — їх покупець не побачить (для пояснення в адмінці). Ступінь між дозволеними
    кількостями не схований — він діє від наступної дозволеної кількості."""
    if not rows or rule.any:
        return []
    return [r for r in rows if r["min_qty"] < rule.first]


# ── Введення людиною ─────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"[;,\n/]+")


def parse_choices(raw) -> list[int]:
    """«1000; 5000; 10 000» / [1000, 5000] → [1000, 5000, 10000]; ValueError з поясненням."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = _SPLIT_RE.split(str(raw))
    out = set()
    for part in parts:
        text = re.sub(r"[\s  '.]", "", part)
        if not text:
            continue
        if not text.isdigit():
            raise ValueError(f"«{part.strip()}» — не ціле число штук. Пишіть через «;», напр. «1000; 5000; 10000».")
        value = int(text)
        if not 1 <= value <= MAX_QTY:
            raise ValueError(f"Кількість {value} — від 1 до {_n(MAX_QTY)} шт.")
        out.add(value)
    if len(out) > MAX_CHOICES:
        raise ValueError(f"Не більше {MAX_CHOICES} кількостей у списку.")
    return sorted(out)


def format_choices(values) -> str:
    return "; ".join(str(int(v)) for v in (values or []))


def check(min_qty, step) -> None:
    """ValueError, якщо мінімум або крок поза межами."""
    for label, v in (("Мін. замовлення", min_qty), ("Крок", step)):
        if v is not None and not 1 <= int(v) <= MAX_QTY:
            raise ValueError(f"{label} — від 1 до {_n(MAX_QTY)} шт.")


def payload(rule: QtyRule) -> dict:
    """Поля для сайту (GET /shop/products/): min_qty, qty_step, qty_choices."""
    return {"min_qty": rule.first, "qty_step": 1 if rule.choices else rule.step, "qty_choices": list(rule.choices)}
