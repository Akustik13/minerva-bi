"""Що в замовленні JLCPCB — усі позиції (файли), а не лише перша.

Замовлення JLCPCB (batch) часто містить кілька файлів: різні плати й кілька частин одного виробу
(part_1 / part_2, board_1 / board_2). Поля JLCOrder.product / quantity / description — лише перша
позиція; повний склад — JLCOrderLine, а поки їх немає — raw_data['orderItem'].

Цим модулем користуються сповіщення (Telegram, лист, зведення активних замовлень), список і картка
замовлення в адмінці.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape

MAX_TG_ITEMS = 15        # позицій у повідомленні Telegram (решта — «… ще N»)
COMPACT_GROUPS = 4       # товарів у короткому рядку (зведення, список)
NAME_MAX = 40            # довжина назви файлу без прив'язаного товару

# Частини назви файлу Gerber, однакові для всіх плат — не те, чим позиції відрізняються.
_NOISE = (
    re.compile(r'^\d{1,2}L$', re.I),                                # шари: 2L
    re.compile(r'^FR-?4$', re.I),                                   # матеріал
    re.compile(r'^\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?){1,2}(?:mm)?$', re.I),  # розмір: 1x180.6x180.6, 140x140x1
    re.compile(r'^Y\d+$', re.I),                                    # код виробництва JLCPCB
)
_SKU_GUESS = re.compile(r'^[A-Za-z]{1,6}[\w.-]*\d[\w.-]*$')

STATUS_ICONS = {
    'ordered': '📋', 'reviewed': '🔍', 'in_production': '🏭', 'manufactured': '✅',
    'shipped': '📦', 'delivered': '🎉', 'cancelled': '❌',
}
STATUS_LABELS = {
    'ordered': 'Замовлено', 'reviewed': 'Перевірено JLC', 'in_production': 'У виробництві',
    'manufactured': 'Виготовлено', 'shipped': 'Відправлено', 'delivered': 'Доставлено',
    'cancelled': 'Скасовано',
}


@dataclass
class Item:
    """Одна позиція (файл) замовлення JLCPCB."""
    name: str                   # артикул товару; не прив'язано — артикул з назви файлу або сама назва
    qty: int
    file_name: str = ''
    produce_code: str = ''
    status_key: str = ''
    product_id: int | None = None
    linked: bool = False        # прив'язано до товару складу
    variant: str = ''           # чим відрізняється від інших позицій того самого товару (part 2, board 1)


@dataclass
class Group:
    """Позиції одного товару разом."""
    name: str
    product_id: int | None
    linked: bool
    qtys: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.qtys)

    @property
    def qty_text(self) -> str:
        """«5» або «10+10» — кожна позиція окремо, бо частини виробу не додаються в штуки виробу."""
        return '+'.join(str(q) for q in self.qtys)


def short(text: str, limit: int = NAME_MAX) -> str:
    text = (text or '').strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _guess_sku(file_name: str) -> str:
    """Артикул з назви файлу Gerber («AN220203-02A_2L_FR4_…» → «AN220203-02A»), якщо схоже на артикул."""
    head = (file_name or '').split('_')[0].strip()
    return head if head and len(head) <= NAME_MAX and _SKU_GUESS.match(head) else ''


def variant_of(file_name: str, sku: str = '') -> str:
    """Чим ця плата відрізняється: назва файлу без артикула, шарів, матеріалу, розміру й коду JLCPCB.

    «AN220203-02A_2L_FR4_1x180.6x180.6_part_2_Y437» → «part 2».
    """
    sku_low = (sku or '').lower()
    tokens = [t for t in re.split(r'[_\s]+', file_name or '') if t]
    if tokens and (tokens[0].lower() == sku_low or (not sku_low and _guess_sku(tokens[0]))):
        tokens = tokens[1:]
    tokens = [t for t in tokens if t.lower() != sku_low and not any(p.match(t) for p in _NOISE)]
    return ' '.join(tokens)


def _natural(text: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', text or '')]


def _from_lines(order) -> list[Item]:
    cache = getattr(order, '_prefetched_objects_cache', {}) or {}
    lines = list(order.lines.all()) if 'lines' in cache else list(order.lines.select_related('product'))
    out = []
    for ln in lines:
        sku = ln.product.sku if ln.product_id and ln.product else ''
        out.append(Item(
            name=sku or _guess_sku(ln.file_name) or short(ln.file_name),
            qty=int(ln.quantity or 0),
            file_name=ln.file_name,
            produce_code=ln.produce_code or '',
            status_key=ln.status_key or '',
            product_id=ln.product_id if sku else None,
            linked=bool(sku),
        ))
    return out


def _from_raw(order) -> list[Item]:
    raw = order.raw_data if isinstance(getattr(order, 'raw_data', None), dict) else {}
    pcbs = [it.get('pcbItem') or {} for it in raw.get('orderItem', []) if isinstance(it, dict)]
    pcbs = [p for p in pcbs if p.get('fileName')]
    if not pcbs:
        return []
    from .services.api import map_jlc_status
    only = len(pcbs) == 1 and order.product_id and order.product
    out = []
    for p in pcbs:
        fname = str(p.get('fileName') or '')
        sku = order.product.sku if only else ''
        try:
            qty = int(p.get('count') or 0)
        except (TypeError, ValueError):
            qty = 0
        out.append(Item(
            name=sku or _guess_sku(fname) or short(fname),
            qty=qty,
            file_name=fname,
            produce_code=str(p.get('produceCode') or ''),
            status_key=map_jlc_status(p.get('orderStatus')) if p.get('orderStatus') is not None else '',
            product_id=order.product_id if sku else None,
            linked=bool(sku),
        ))
    return out


def _mark_variants(items: list[Item]) -> None:
    by_name: dict[str, list[Item]] = {}
    for it in items:
        by_name.setdefault(it.name, []).append(it)
    for group in by_name.values():
        if len(group) < 2:
            continue
        for it in group:
            it.variant = variant_of(it.file_name, it.name) or it.produce_code
        if len({it.variant for it in group}) < len(group):
            for it in group:
                it.variant = ' · '.join(x for x in (it.variant, it.produce_code) if x) or it.file_name


def items(order) -> list[Item]:
    """Усі позиції замовлення: позиції з бази → дані API → саме замовлення (одна позиція)."""
    found = _from_lines(order) if order.pk else []
    if not found:
        found = _from_raw(order)
    if not found:
        sku = order.product.sku if order.product_id and order.product else ''
        found = [Item(
            name=sku or _guess_sku(order.description) or short(order.description) or order.jlc_order_id,
            qty=int(order.quantity or 0),
            file_name=order.description or '',
            status_key=order.local_status or '',
            product_id=order.product_id if sku else None,
            linked=bool(sku),
        )]
    _mark_variants(found)
    first: dict[str, int] = {}
    for i, it in enumerate(found):
        first.setdefault(it.name, i)
    found.sort(key=lambda it: (first[it.name], _natural(it.variant)))
    return found


def groups(found: list[Item]) -> list[Group]:
    out: dict[str, Group] = {}
    for it in found:
        g = out.setdefault(it.name, Group(name=it.name, product_id=it.product_id, linked=it.linked))
        g.qtys.append(it.qty)
    return list(out.values())


def total_qty(found: list[Item]) -> int:
    return sum(it.qty for it in found)


def compact(found: list[Item], limit: int = COMPACT_GROUPS) -> str:
    """Коротко одним рядком: «AN220210-01C ×5 · AN220203-02A ×10+10 · AN220215-01A ×25+25»."""
    gs = groups(found)
    parts = [f'{g.name} ×{g.qty_text}' for g in gs[:limit]]
    if len(gs) > limit:
        parts.append(f'ще {len(gs) - limit}')
    return ' · '.join(parts)


def status_note(it: Item, order_status: str) -> str:
    """Стан позиції, якщо він не такий, як у всього замовлення («🏭 У виробництві»)."""
    if not it.status_key or it.status_key == order_status:
        return ''
    return f'{STATUS_ICONS.get(it.status_key, "")} {STATUS_LABELS.get(it.status_key, it.status_key)}'.strip()


def telegram_block(found: list[Item], order_status: str, limit: int = MAX_TG_ITEMS) -> str:
    """Склад замовлення для повідомлення Telegram (parse_mode HTML), рядки з «\\n» на початку."""
    if len(found) == 1:
        it = found[0]
        text = f'\nКількість: <b>{it.qty}</b> шт.'
        if it.linked:
            text += f'\n🏷 Товар: <b>{escape(it.name)}</b>'
        elif it.file_name:
            text += f'\n📄 {escape(short(it.file_name, 80))}'
        return text
    text = f'\n📋 Позицій: <b>{len(found)}</b> · разом <b>{total_qty(found)}</b> шт.'
    for it in found[:limit]:
        mark = '🏷' if it.linked else '⬜'
        line = f'\n{mark} <b>{escape(it.name)}</b> × {it.qty}'
        if it.variant:
            line += f' · {escape(it.variant)}'
        note = status_note(it, order_status)
        if note:
            line += f' — {note}'
        text += line
    rest = found[limit:]
    if rest:
        text += f'\n… ще {len(rest)} поз. ({total_qty(rest)} шт.)'
    if any(not it.linked for it in found):
        text += '\n<i>⬜ — файл не прив’язано до товару складу</i>'
    return text


def plain_block(found: list[Item], order_status: str) -> str:
    """Те саме для текстової частини листа."""
    if len(found) == 1:
        it = found[0]
        return f'Кількість: {it.qty} шт.\n' + (f'Товар: {it.name}\n' if it.linked else '')
    out = f'Позицій: {len(found)}, разом {total_qty(found)} шт.\n'
    for it in found:
        line = f'  - {it.name} × {it.qty}'
        if it.variant:
            line += f' · {it.variant}'
        if not it.linked:
            line += ' (не прив’язано до складу)'
        note = status_note(it, order_status)
        if note:
            line += f' — {note}'
        out += line + '\n'
    return out
