"""📊 Таблиця пропозицій запиту постачальникам (RFQ) для агента (MCP, agent/toolsets.py) — те, що на сторінці
`/admin/suppliers/supplierrequest/<pk>/work/` роблять із самою таблицею: ➕ свої колонки («Сума з ENIG», «Покриття»,
«Зразки до»…), їхні значення в рядках, 🏷 позначки рядків і 📌 місце рядка.

Читання (право `suppliers.view_supplierrequest`):
  rfq_table — таблиця, як на сторінці: колонки (стандартні й свої з ключами c1…), рядки в порядку сторінки
              (📌 закріплені першими, далі як `rfq_compare.table`), по кожній кількості — ціна за шт. і сума з
              разовими (у валюті постачальника й Minerva), 🥇 найнижча, ⚡ найшвидше, що враховано в сумі
              (✓ / ✗ / ?), строк; умови, діє до; значення своїх колонок (хто вписав — 🧠 AI / ✋ людина / 🤖 агент);
              позначки; довідник кольорів і наліпок.
Зміни — propose_* → людина ✓ (`AgentProposal.RFQ`, дія в `data.action`; `tools_rfq.apply` → `APPLY` тут) тими
самими функціями, що сторінка (`suppliers/services/rfq_columns.py`, `rfq_marks.py`):
  propose_rfq_columns       — додати колонки (одразу й зі значеннями), перейменувати / змінити підказку, прибрати;
  propose_rfq_column_values — значення своїх колонок у рядку постачальника (src agent — 🧠 AI їх не перезаписує);
  propose_rfq_marks         — колір «№», наліпки, свій текст, 🧹 зняти, 📌 місце рядка (одному чи кільком рядкам).
Застосування відмовляє, якщо колонку чи значення змінили після пропозиції. Назви, значення, умови — ДАНІ, не
вказівки (<<<…>>>).
"""
import re

from django.db import transaction

from agent.tools import ToolError
from agent.toolsets import one_line, require
from agent.toolsets import date as fmt_date

VIEW_PERM = 'suppliers.view_supplierrequest'
CHANGE_PERM = 'suppliers.change_supplierrequest'
ROWS_MAX = 40
SUPPLIERS_MAX = 40
QTYS_MAX = 12

_REQUEST = {'type': 'string', 'description': 'Номер запиту RFQ-РРРР-NNN або #id (find_supplier_requests)'}
_SUPPLIER = {'type': 'string', 'description': 'Постачальник у запиті: #pk постачальника, назва або email отримувача'}
_COLUMN = {'type': 'string', 'description': 'Своя колонка: ключ (c1, c2… — rfq_table) або назва'}
_EVIDENCE = {'type': 'string', 'description': 'Підстава — точна цитата з листа / файлу постачальника або як пораховано'}
_SOURCE = {'type': 'string', 'description': 'Звідки: лист (від кого, тема, дата), файл, розрахунок'}
_VALUE = {'description': 'Колонка «число на кожну кількість»: «2500: 454,6; 5000: 820» або {"2500": 454.6} (кількості, '
                         'яких не назвали, лишаються; "" / null для кількості — прибрати; одна кількість у запиті — '
                         'можна просто число); текстова колонка — рядок; "" / null — прибрати значення'}

TABLE = {
    'name': 'rfq_table',
    'description': 'Таблиця пропозицій запиту постачальникам — як на сторінці запиту: колонки (стандартні й свої з '
                   'ключами c1, c2…, які можна змінювати), рядки в порядку сторінки (📌 закріплені першими), для '
                   'кожної кількості ціна за шт. і сума з разовими (валюта постачальника і Minerva), 🥇 найнижча, '
                   '⚡ найшвидше, що враховано в сумі, строк; умови, діє до; значення своїх колонок (хто вписав); '
                   '🏷 позначки рядків; довідник кольорів і наліпок для propose_rfq_marks. qty — лише одна кількість, '
                   'supplier — лише один рядок.',
    'input_schema': {'type': 'object', 'properties': {
        'request': _REQUEST, 'qty': {'type': 'integer', 'description': 'Лише ця кількість'},
        'supplier': _SUPPLIER}, 'required': ['request']}}
COLUMNS = {
    'name': 'propose_rfq_columns',
    'description': 'Запропонувати зміни своїх колонок таблиці запиту (людина ✓): add — нові колонки [{name, kind, '
                   'hint, values}] (kind price — число на кожну кількість у валюті пропозиції постачальника: сума чи '
                   'ціна, як назвете; text — короткий текст; hint — що саме в ній, щоб 🧠 AI Minerva знав, що '
                   'шукати в листах; values — одразу значення {постачальник: значення}); edit — [{column, name, '
                   'hint}] перейменувати / змінити підказку; delete — [column] прибрати колонку разом зі значеннями '
                   'в усіх рядках. Колонки спільні для всіх, хто бачить запит; щонайбільше 8.',
    'input_schema': {'type': 'object', 'properties': {
        'request': _REQUEST,
        'add': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['price', 'text']},
            'hint': {'type': 'string'},
            'values': {'type': 'object', 'additionalProperties': _VALUE,
                       'description': '{постачальник (#pk / назва / email): значення}'}},
            'required': ['name']}},
        'edit': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'column': _COLUMN, 'name': {'type': 'string'}, 'hint': {'type': 'string'}}, 'required': ['column']}},
        'delete': {'type': 'array', 'items': _COLUMN},
        'evidence': _EVIDENCE, 'source': _SOURCE}, 'required': ['request']}}
VALUES = {
    'name': 'propose_rfq_column_values',
    'description': 'Запропонувати значення своїх колонок таблиці запиту (людина ✓): supplier + values {колонка: '
                   'значення} — один рядок; або rows [{supplier, values}] — кілька рядків одним викликом (кожен — '
                   'окрема пропозиція). Число на кожну кількість — у валюті пропозиції постачальника («2500: 454,6; '
                   '5000: 820»); кількості, яких не назвали, лишаються. Вписане так 🧠 AI Minerva вже не '
                   'перезаписує. Нову колонку зі значеннями — propose_rfq_columns add[].values.',
    'input_schema': {'type': 'object', 'properties': {
        'request': _REQUEST, 'supplier': _SUPPLIER,
        'values': {'type': 'object', 'additionalProperties': _VALUE, 'description': '{колонка (c1 / назва): значення}'},
        'rows': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'supplier': _SUPPLIER, 'values': {'type': 'object', 'additionalProperties': _VALUE},
            'evidence': _EVIDENCE}, 'required': ['supplier', 'values']}},
        'evidence': _EVIDENCE, 'source': _SOURCE}, 'required': ['request']}}
MARKS = {
    'name': 'propose_rfq_marks',
    'description': 'Запропонувати 🏷 позначку рядків таблиці запиту (людина ✓), як правий клік на сторінці: suppliers '
                   '— один чи кілька рядків; color — колір клітинки «№» (red, orange, yellow, green, blue, purple; "" — '
                   'без кольору); stickers_on / stickers_off — наліпки (star ⭐ важливо, like 👍, ask ❓ уточнити, '
                   'wait ⏳ чекаємо, talk 💬 обговорити, sample 📦 зразки, no 🚫 не підходить); text — свій короткий '
                   'текст (до 60, "" — прибрати); clear — зняти колір, наліпки й текст (📌 місце лишається); rank — '
                   '📌 закріпити рядок на місці 1, 2, 3… вгорі таблиці (0 — відкріпити; лише для одного рядка).',
    'input_schema': {'type': 'object', 'properties': {
        'request': _REQUEST, 'suppliers': {'type': 'array', 'items': _SUPPLIER},
        'color': {'type': 'string', 'enum': ['', 'red', 'orange', 'yellow', 'green', 'blue', 'purple']},
        'stickers_on': {'type': 'array', 'items': {'type': 'string'}},
        'stickers_off': {'type': 'array', 'items': {'type': 'string'}},
        'text': {'type': 'string'}, 'clear': {'type': 'boolean'},
        'rank': {'type': 'integer', 'minimum': 0, 'maximum': 99},
        'evidence': _EVIDENCE, 'source': _SOURCE}, 'required': ['request', 'suppliers']}}

READ_TOOLS = [TABLE]
CHANGE_TOOLS = [COLUMNS, VALUES, MARKS]
TOOLS = READ_TOOLS + CHANGE_TOOLS


def _base():
    from agent import tools_rfq
    return tools_rfq


def _can(user, perm) -> bool:
    return bool(user is not None and getattr(user, 'is_active', False) and user.has_perm(perm))


def tools_for(user) -> list:
    if not _base()._installed() or not _can(user, VIEW_PERM):
        return []
    return READ_TOOLS + (CHANGE_TOOLS if _can(user, CHANGE_PERM) else [])


# ── спільне ──────────────────────────────────────────────────────────────────

SRC = {'ai': '🧠 AI', 'user': '✋ людина', 'agent': '🤖 агент'}


def get_column(req, value) -> dict:
    from suppliers.services import rfq_columns
    cols = rfq_columns.columns(req)
    v = ' '.join(str(value or '').split())
    if not v:
        raise ToolError('потрібна колонка: ключ (c1, c2…) або назва')
    col = (next((c for c in cols if c['key'] == v.lower()), None)
           or next((c for c in cols if c['name'].lower() == v.lower()), None))
    if col is None:
        have = ', '.join(f'{c["key"]} «{c["name"]}»' for c in cols) or 'ще немає — додайте propose_rfq_columns'
        raise ToolError(f'колонки «{v}» немає в запиті {req.code}; свої колонки: {have}')
    return col


def _qty_list(req) -> list:
    from suppliers.services import rfq
    return rfq.parse_quantities(req.quantities)


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == {} or v == []


_PAIR = re.compile(r'^\s*(\d[\d\s.  ]*)\s*(?:шт\.?|pcs\.?|pc\.?|stk\.?)?\s*[:=]\s*(.*?)\s*$', re.I)


def parse_value(req, col, value):
    """Значення колонки з аргументу агента → text: рядок ('' — прибрати); price: {кількість: «454.6» | None}
    (None — прибрати цю кількість) або None — прибрати всю колонку в рядку."""
    from suppliers.services import rfq_columns
    if col['kind'] == 'text':
        if _empty(value):
            return ''
        if isinstance(value, (dict, list, bool)):
            raise ToolError(f'«{col["name"]}» — текстова колонка: значення рядком')
        return ' '.join(str(value).split())[:rfq_columns.TEXT_MAX]
    if _empty(value):
        return None
    qtys = _qty_list(req)
    pairs = []
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, list):
        for it in value:
            if not isinstance(it, dict) or 'qty' not in it:
                raise ToolError(f'«{col["name"]}»: список — [{{qty, value}}]')
            pairs.append((it.get('qty'), it.get('value', it.get('amount'))))
    elif isinstance(value, bool):
        raise ToolError(f'«{col["name"]}» — число на кожну кількість')
    elif isinstance(value, (int, float)):
        if len(qtys) != 1:
            raise ToolError(f'«{col["name"]}» — число на кожну кількість: «'
                            + '; '.join(f'{q}: …' for q in (qtys or [2500, 5000])[:4]) + '»')
        pairs = [(qtys[0], value)]
    else:
        parts = [p for p in re.split(r'[;\n]+', str(value)) if p.strip()]
        for part in parts:
            m = _PAIR.match(part)
            if m:
                pairs.append((m.group(1), m.group(2)))
            elif len(parts) == 1 and len(qtys) == 1:
                pairs.append((qtys[0], part))
            else:
                raise ToolError(f'«{col["name"]}»: «{one_line(part, 40)}» — пишіть «кількість: число», напр. '
                                f'«2500: 454,6; 5000: 820»')
    out = {}
    for q, v in pairs[:QTYS_MAX]:
        try:
            qi = int(re.sub(r'[\s.  ]', '', str(q)))
        except ValueError:
            raise ToolError(f'«{col["name"]}»: кількість «{q}» — ціле число') from None
        if qi <= 0:
            raise ToolError(f'«{col["name"]}»: кількість має бути більшою за 0')
        if qtys and qi not in qtys:
            raise ToolError(f'у запиті {req.code} кількості {", ".join(str(x) for x in qtys)} — {qi} шт немає')
        if _empty(v):
            out[str(qi)] = None
            continue
        d = rfq_columns.number(v) if not isinstance(v, bool) else None
        if d is None:
            raise ToolError(f'«{col["name"]}», {qi} шт: «{one_line(v, 30)}» — не схоже на число ≥ 0 (напр. 711,46)')
        out[str(qi)] = rfq_columns.plain(d)
    if not out:
        raise ToolError(f'«{col["name"]}»: немає жодної кількості зі значенням')
    return out


def current(r, col):
    """Що зараз у клітинці: text — рядок; price — {кількість: рядок}."""
    e = (r.extra or {}).get(col['key']) or {}
    v = e.get('v')
    if col['kind'] == 'text':
        return v if isinstance(v, str) else ''
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def _changes(col, cur, new):
    """Лише те, що справді зміниться: text → рядок або None; price → {кількість: значення | None}."""
    from suppliers.services import rfq_columns
    if col['kind'] == 'text':
        return None if new == cur else new
    if new is None:                                # прибрати всю колонку в рядку
        return {q: None for q in cur} or None
    out = {}
    for q, v in new.items():
        was = rfq_columns.number(cur.get(q)) if q in cur else None
        now = rfq_columns.number(v) if v is not None else None
        if was != now:
            out[q] = v
    return out or None


def _value_text(col, value, cur='') -> str:
    from suppliers.services import rfq_compare, rfq_columns
    if col['kind'] == 'text':
        return f'«{one_line(value, 120)}»' if value else '— прибрати'
    if value is None:
        return '— прибрати'
    bits = []
    for q, v in sorted(value.items(), key=lambda kv: int(kv[0])):
        bits.append(f'{q} шт — прибрати' if v is None else
                    f'{q} шт {rfq_compare.money(rfq_columns.number(v))}' + (f' {cur}' if cur else ''))
    return '; '.join(bits)


def _old_text(col, cur, keys, currency='') -> str:
    from suppliers.services import rfq_compare, rfq_columns
    if col['kind'] == 'text':
        return f'«{one_line(cur, 120)}»' if cur else '—'
    shown = [q for q in sorted(cur, key=int) if keys is None or q in keys]
    return '; '.join(f'{q} шт {rfq_compare.money(rfq_columns.number(cur[q]))}' + (f' {currency}' if currency else '')
                     for q in shown) or '—'


# ── rfq_table ────────────────────────────────────────────────────────────────

def _order(rows):
    from suppliers.services import rfq_marks
    pinned = sorted((x for x in rows if rfq_marks.view(x['recipient'].label)['rank']),
                    key=lambda x: rfq_marks.view(x['recipient'].label)['rank'])
    return pinned + [x for x in rows if not rfq_marks.view(x['recipient'].label)['rank']]


def _label_text(lab) -> str:
    bits = []
    if lab['rank']:
        bits.append(f'📌 {lab["rank"]} місце')
    if lab['color']:
        bits.append(f'колір {lab["color"]} ({lab["color_name"]})')
    bits += [f'{s["icon"]} {s["key"]}' for s in lab['stickers']]
    if lab['text']:
        bits.append(f'«{lab["text"]}»')
    if bits and lab['by']:
        bits.append(f'{lab["by"]}, {lab["at"]}')
    return ' · '.join(bits)


FLAG = {'y': '✓', 'n': '✗', 'u': '?'}


def _cell_text(c, home, cur) -> str:
    if c.get('missing'):
        return 'ціни немає'
    unit = f'{c["unit_text"]} {cur}/шт'
    if c.get('unit_home_text') and cur != home:
        unit += f' ({c["unit_home_text"]} {home})'
    bits = [unit]
    total = f'сума з разовими {c["total_text"]} {cur}'
    if c.get('total_home_text') and cur != home:
        total += f' ({c["total_home_text"]} {home})'
    bits.append(total)
    if c.get('sup_total_text'):
        bits.append(f'у постачальника {c["sup_total_text"]} {cur}'
                    + (' з разовими' if c.get('sup_total_ot') else ' без разових')
                    + (' ⚠️ не сходиться' if c.get('sum_mismatch') else ''))
    if not c.get('exact'):
        bits.append(f'ціна на {c["tier_qty"]} шт')
    if c.get('best'):
        bits.append('🥇 найнижча сума')
    elif c.get('over_text'):
        bits.append(c['over_text'])
    if c.get('unit_best'):
        bits.append('найнижча ціна за шт.')
    bits.append('у сумі: ' + ' '.join(f'{FLAG[f["state"]]}{f["label"]}' for f in c.get('flags') or []))
    if c.get('parts_text'):
        bits.append(f'складові {c["parts_text"]}')
    if c.get('lacks'):
        bits.append('⚠️ у ціні немає: ' + ', '.join(c['lacks']))
    elif c.get('unknown'):
        bits.append('⚠️ не сказано, що в ціні')
    if c.get('lead') is not None:
        bits.append(f'строк {c["lead"]} дн.' + (' ⚡ найшвидше' if c.get('fast') else ''))
    return ' · '.join(bits)


def rfq_table(ctx, args) -> str:
    from suppliers.services import rfq_columns, rfq_compare, rfq_marks
    require(ctx.task.owner, VIEW_PERM)
    base = _base()
    req = base.get_request(args.get('request'))
    only = base.get_recipient(req, args['supplier']) if str(args.get('supplier') or '').strip() else None
    cmp = rfq_compare.table(req)
    qtys, home = cmp['qtys'], cmp['home']
    want = None
    if args.get('qty') not in (None, ''):
        try:
            want = int(args['qty'])
        except (TypeError, ValueError):
            raise ToolError('qty — ціле число') from None
        if want not in qtys:
            raise ToolError(f'у таблиці кількості {", ".join(str(q) for q in qtys) or "—"} — {want} шт немає')
    cols = rfq_columns.columns(req)
    xcells = rfq_columns.cells(cmp, req)
    lines = [f'{req.code} «{one_line(req.title, 160)}» · {req.get_status_display()} · кількості: '
             + (', '.join(str(q) for q in qtys) or '—') + f' · валюта Minerva {home}'
             + (f' · просили в ціні: {cmp["needed"]}' if cmp.get('needed') else '')]
    lines.append('Стандартні колонки: № (📌 місце, 🏷 позначка), Постачальник, Ціна / шт., Сума, Враховано в сумі, '
                 'Строк, дн., Умови, Діє до.')
    if cols:
        lines.append(f'Свої колонки ({len(cols)} з {rfq_columns.MAX_COLUMNS}):')
        for c in cols:
            lines.append(f'- {c["key"]} «{c["name"]}» — '
                         + ('число на кожну кількість (у валюті пропозиції постачальника)' if c['kind'] == 'price'
                            else 'текст') + (f' · підказка: {c["hint"]}' if c['hint'] else ''))
    else:
        lines.append(f'Своїх колонок ще немає (додати — propose_rfq_columns, до {rfq_columns.MAX_COLUMNS}).')
    rows = _order(cmp['rows'])
    if only is not None:
        rows = [x for x in rows if x['recipient'].pk == only.pk]
    lines.append(f'\nРядки ({len(rows)}' + (', у порядку сторінки: 📌 закріплені першими' if only is None else '')
                 + '):' if rows else '\nРядків ще немає.')
    for n, row in enumerate(rows[:ROWS_MAX], 1):
        r = row['recipient']
        cur = (r.currency or home).upper()
        head = [f'{n}. #{r.supplier_id} {one_line(r.supplier.name, 80)}', r.get_state_display()]
        lab = _label_text(rfq_marks.view(r.label))
        if lab:
            head.append(f'🏷 {lab}')
        if r.chosen:
            head.append('🏆 вибрано')
        if r.award:
            head.append(f'✅ доручено: {one_line(r.award, 80)}')
        if r.reject_reason:
            head.append(f'✖ не беремо: {one_line(r.reject_reason, 80)}')
        lines.append(' · '.join(head))
        if row['priced']:
            for i, q in enumerate(qtys):
                if want is not None and q != want:
                    continue
                lines.append(f'   {q} шт: ' + _cell_text(row['cells'][i], home, cur))
            if row['one_time_note']:
                lines.append(f'   разово: {row["one_time_note"]} {cur}')
            if row['shipping_text']:
                lines.append(f'   доставка окремо: {row["shipping_text"]} {cur}')
            if row['gaps']:
                lines.append('   ⚠️ ' + '; '.join(row['gaps']))
        elif row['unparsed']:
            lines.append('   відповідь ще не розібрано (🧠 «Розібрати відповіді» на сторінці або propose_rfq_quote)')
        elif row['no_price_read']:
            lines.append('   відповів без цін')
        if row['terms']:
            lines.append(f'   умови: {one_line(row["terms"], 300)}')
        if r.valid_until:
            lines.append(f'   діє до {fmt_date(r.valid_until)}')
        for x in xcells.get(r.pk, []):
            src = f' ({SRC[x["src"]]})' if x.get('src') in SRC else ''
            if x['kind'] == 'text':
                if x.get('text'):
                    lines.append(f'   {x["key"]} «{x["name"]}»: «{one_line(x["text"], 200)}»{src}')
                continue
            vals = [f'{q["qty"]} шт {q["text"]}' + (f' ({q["home_text"]})' if q.get('home_text') else '')
                    + (' — найнижча' if q.get('best') else '')
                    for q in x['qs'] if q.get('text') and (want is None or q['qty'] == want)]
            if vals:
                lines.append(f'   {x["key"]} «{x["name"]}»: ' + '; '.join(vals) + src)
    if len(rows) > ROWS_MAX:
        lines.append(f'… ще {len(rows) - ROWS_MAX} рядків (supplier — один рядок)')
    if cmp.get('missing_fx'):
        lines.append('Немає курсу: ' + ', '.join(cmp['missing_fx']) + ' — у 🥇 не беруть участі.')
    lines.append('\nДля propose_rfq_marks — кольори: ' + ', '.join(f'{k} {i}' for k, i, _n, _h in rfq_marks.COLORS)
                 + '; наліпки: ' + ', '.join(f'{k} {i} {n}' for k, i, n in rfq_marks.STICKERS) + '.')
    return base._wrap('ТАБЛИЦЯ ПРОПОЗИЦІЙ', lines)


# ── propose_rfq_columns ──────────────────────────────────────────────────────

def _recipient_values(req, col, values) -> list:
    """{постачальник: значення} → [{recipient_id, supplier, value}] (порожні — пропущено)."""
    if values in (None, '', {}):
        return []
    if not isinstance(values, dict):
        raise ToolError(f'«{col["name"]}»: values — {{постачальник: значення}}')
    base, out, seen = _base(), [], set()
    for who, v in list(values.items())[:SUPPLIERS_MAX]:
        r = base.get_recipient(req, who)
        if r.pk in seen:
            raise ToolError(f'{r.supplier.name} двічі в values')
        seen.add(r.pk)
        value = parse_value(req, col, v)
        if value in (None, ''):
            continue
        if col['kind'] == 'price':
            value = {q: x for q, x in value.items() if x is not None}
            if not value:
                continue
        out.append({'recipient_id': r.pk, 'supplier': r.supplier.name, 'value': value,
                    'currency': (r.currency or '').upper()})
    return out


def propose_rfq_columns(ctx, args) -> str:
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services import rfq_columns
    require(ctx.task.owner, CHANGE_PERM)
    base = _base()
    req = base.get_request(args.get('request'))
    cols = rfq_columns.columns(req)
    names = {c['name'].lower(): c for c in cols}
    adds, edits, dels, texts = [], [], [], []
    deleted = set()
    for item in args.get('delete') or []:
        col = get_column(req, item)
        if col['key'] in deleted:
            continue
        deleted.add(col['key'])
        n = sum(1 for e in R.objects.filter(request=req).values_list('extra', flat=True) if col['key'] in (e or {}))
        dels.append({'key': col['key'], 'name': col['name'], 'kind': col['kind'], 'rows': n})
        texts.append(f'✖ прибрати «{col["name"]}»' + (f' (зі значеннями в {n} рядках)' if n else ''))
    for item in args.get('edit') or []:
        if not isinstance(item, dict):
            raise ToolError('edit — [{column, name, hint}]')
        col = get_column(req, item.get('column'))
        if col['key'] in deleted:
            raise ToolError(f'«{col["name"]}» — або змінити, або прибрати, не разом')
        change = {}
        if item.get('name') is not None:
            name = ' '.join(str(item['name']).split())[:rfq_columns.NAME_MAX]
            if not name:
                raise ToolError('назва колонки не може бути порожньою')
            other = names.get(name.lower())
            if other is not None and other['key'] != col['key']:
                raise ToolError(f'колонка «{name}» уже є')
            if name != col['name']:
                change['name'] = name
        if item.get('hint') is not None:
            hint = ' '.join(str(item['hint']).split())[:rfq_columns.HINT_MAX]
            if hint != col['hint']:
                change['hint'] = hint
        if not change:
            continue
        edits.append({'key': col['key'], 'old_name': col['name'], 'old_hint': col['hint'], **change})
        texts.append(f'✏️ «{col["name"]}»' + (f' → «{change["name"]}»' if 'name' in change else '')
                     + (f' · підказка: {change["hint"] or "—"}' if 'hint' in change else ''))
    taken = {c['name'].lower() for c in cols if c['key'] not in deleted} | {e['name'].lower() for e in edits
                                                                            if 'name' in e}
    for item in args.get('add') or []:
        if not isinstance(item, dict):
            raise ToolError('add — [{name, kind, hint, values}]')
        name = ' '.join(str(item.get('name') or '').split())[:rfq_columns.NAME_MAX]
        if not name:
            raise ToolError('назвіть колонку — напр. «Сума з ENIG»')
        if name.lower() in taken:
            raise ToolError(f'колонка «{name}» уже є')
        taken.add(name.lower())
        kind = item.get('kind') or 'price'
        if kind not in rfq_columns.KINDS:
            raise ToolError('kind — price (число на кожну кількість) або text')
        hint = ' '.join(str(item.get('hint') or '').split())[:rfq_columns.HINT_MAX]
        col = {'key': '', 'name': name, 'kind': kind, 'hint': hint}
        values = _recipient_values(req, col, item.get('values'))
        adds.append({'name': name, 'kind': kind, 'hint': hint, 'values': values})
        texts.append(f'➕ «{name}» ({"число на кожну кількість" if kind == "price" else "текст"})'
                     + (f' · підказка: {hint}' if hint else '')
                     + (' · значення: ' + '; '.join(f'{v["supplier"]}: {_value_text(col, v["value"], v["currency"])}'
                                                   for v in values) if values else ''))
    if not (adds or edits or dels):
        return f'Колонок запиту {req.code} це не змінює — нічого пропонувати.'
    left = len(cols) - len(dels) + len(adds)
    if left > rfq_columns.MAX_COLUMNS:
        raise ToolError(f'своїх колонок — щонайбільше {rfq_columns.MAX_COLUMNS} (було б {left}); прибрати непотрібні '
                        f'— delete')
    key = 'columns:' + req.code + ':' + '|'.join(sorted([f'+{a["name"].lower()}' for a in adds]
                                                       + [f'~{e["key"]}' for e in edits]
                                                       + [f'-{d["key"]}' for d in dels]))
    text = ' · '.join(texts)
    base._propose(ctx, 'columns', req, {'add': adds, 'edit': edits, 'delete': dels}, new=text,
                  old=', '.join(f'{c["key"]} «{c["name"]}»' for c in cols) or 'своїх колонок немає',
                  evidence=args.get('evidence'), source=args.get('source'), key=key)
    return f'Запропоновано колонки таблиці запиту {req.code}: {text} — людина підтвердить ✓.'


# ── propose_rfq_column_values ────────────────────────────────────────────────

def _row_proposal(ctx, req, who, values, evidence, source) -> str:
    base = _base()
    r = base.get_recipient(req, who)
    if not isinstance(values, dict) or not values:
        raise ToolError('values — {колонка (c1 / назва): значення}')
    cur = (r.currency or '').upper()
    out, texts, olds = {}, [], []
    for name, v in values.items():
        col = get_column(req, name)
        if col['key'] in out:
            raise ToolError(f'«{col["name"]}» двічі')
        was = current(r, col)
        change = _changes(col, was, parse_value(req, col, v))
        if change is None:
            continue
        out[col['key']] = {'name': col['name'], 'kind': col['kind'], 'new': change, 'old': was}
        texts.append(f'«{col["name"]}»: {_value_text(col, change, cur)}')
        olds.append(f'«{col["name"]}»: {_old_text(col, was, None if col["kind"] == "text" else set(change), cur)}')
    if not out:
        return f'{r.supplier.name}: уже так — змінювати нічого.'
    text = f'{r.supplier.name} · ' + ' · '.join(texts)
    base._propose(ctx, 'xvalues', req, {'recipient_id': r.pk, 'supplier': r.supplier.name, 'values': out},
                  new=text, old=' · '.join(olds), evidence=evidence, source=source,
                  key=f'xvalues:{req.code}:{r.pk}:{",".join(sorted(out))}')
    return f'запропоновано {text}'


def propose_rfq_column_values(ctx, args) -> str:
    require(ctx.task.owner, CHANGE_PERM)
    req = _base().get_request(args.get('request'))
    rows = args.get('rows') or []
    if args.get('supplier') or args.get('values'):
        rows = [{'supplier': args.get('supplier'), 'values': args.get('values')}] + list(rows)
    if not rows:
        raise ToolError('потрібні supplier + values або rows [{supplier, values}]')
    if len(rows) > SUPPLIERS_MAX:
        raise ToolError(f'щонайбільше {SUPPLIERS_MAX} рядків за раз')
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ToolError('rows — [{supplier, values}]')
        out.append(_row_proposal(ctx, req, row.get('supplier'), row.get('values'),
                                 row.get('evidence') or args.get('evidence'), args.get('source')))
    return f'Запит {req.code}: ' + '; '.join(out) + ' — людина підтвердить ✓.'


# ── propose_rfq_marks ────────────────────────────────────────────────────────

def propose_rfq_marks(ctx, args) -> str:
    from suppliers.services import rfq_marks
    require(ctx.task.owner, CHANGE_PERM)
    base = _base()
    req = base.get_request(args.get('request'))
    who = args.get('suppliers') or []
    if isinstance(who, str):
        who = [who]
    if not who:
        raise ToolError('suppliers — кому з постачальників запиту')
    rows = []
    for v in who[:SUPPLIERS_MAX]:
        r = base.get_recipient(req, v)
        if r.pk not in {x.pk for x in rows}:
            rows.append(r)
    colors = {k: (i, n) for k, i, n, _h in rfq_marks.COLORS}
    stickers = {k: (i, n) for k, i, n in rfq_marks.STICKERS}
    op, texts = {}, []
    if args.get('clear'):
        op['clear'] = True
        texts.append('🧹 зняти колір, наліпки й текст')
    if args.get('color') is not None:
        c = str(args['color']).strip().lower()
        if c and c not in colors:
            raise ToolError('color — ' + ', '.join(colors) + ' або "" (без кольору)')
        op['color'] = c
        texts.append(f'колір {colors[c][0]} {colors[c][1]}' if c else 'без кольору')
    on = [str(x).strip().lower() for x in args.get('stickers_on') or []]
    off = [str(x).strip().lower() for x in args.get('stickers_off') or []]
    bad = [x for x in on + off if x not in stickers]
    if bad:
        raise ToolError(f'наліпки {", ".join(bad)} немає; є: ' + ', '.join(f'{k} {i}' for k, (i, _n) in stickers.items()))
    if set(on) & set(off):
        raise ToolError('та сама наліпка і в stickers_on, і в stickers_off')
    if on or off:
        op['stickers'] = [[k, True] for k in dict.fromkeys(on)] + [[k, False] for k in dict.fromkeys(off)]
        texts += [f'+{stickers[k][0]} {stickers[k][1]}' for k in dict.fromkeys(on)]
        texts += [f'−{stickers[k][0]} {stickers[k][1]}' for k in dict.fromkeys(off)]
    if args.get('text') is not None:
        t = ' '.join(str(args['text']).split())
        if len(t) > rfq_marks.TEXT_MAX:
            raise ToolError(f'text — до {rfq_marks.TEXT_MAX} знаків')
        op['text'] = t
        texts.append(f'текст «{t}»' if t else 'без тексту')
    rank = None
    if args.get('rank') is not None:
        try:
            rank = int(args['rank'])
        except (TypeError, ValueError):
            raise ToolError('rank — ціле 0…99') from None
        if not 0 <= rank <= rfq_marks.RANK_MAX:
            raise ToolError('rank — ціле 0…99 (0 — відкріпити)')
        if len(rows) != 1:
            raise ToolError('📌 місце — лише для одного рядка')
        texts.append(f'📌 {rank} місце' if rank else '📌 відкріпити')
    if not op and rank is None:
        raise ToolError('що змінити: color, stickers_on / stickers_off, text, clear або rank')
    names = [r.supplier.name for r in rows]
    text = ', '.join(names) + ': ' + ' · '.join(texts)
    old = '; '.join(f'{r.supplier.name}: {_label_text(rfq_marks.view(r.label)) or "без позначки"}' for r in rows)
    base._propose(ctx, 'marks', req, {'recipients': [r.pk for r in rows], 'names': names, 'op': op, 'rank': rank},
                  new=f'🏷 {text}', old=old, evidence=args.get('evidence'), source=args.get('source'),
                  key=f'marks:{req.code}:{",".join(str(r.pk) for r in rows)}')
    return f'Запропоновано позначку в запиті {req.code}: {text} — людина підтвердить ✓.'


HANDLERS = {'rfq_table': rfq_table, 'propose_rfq_columns': propose_rfq_columns,
            'propose_rfq_column_values': propose_rfq_column_values, 'propose_rfq_marks': propose_rfq_marks}


# ── ✓ людини (викликає tools_rfq.apply) ──────────────────────────────────────

class _Refused(Exception):
    pass


def _apply_columns(p, req, d, user):
    from suppliers.models import SupplierRequest, SupplierRequestRecipient as R
    from suppliers.services import rfq_columns
    try:
        with transaction.atomic():
            req = SupplierRequest.objects.select_for_update().get(pk=req.pk)
            cols = {c['key']: c for c in rfq_columns.columns(req)}
            for x in d.get('delete') or []:
                if x['key'] not in cols or cols[x['key']]['name'] != x['name']:
                    raise _Refused(f'колонку «{x["name"]}» уже змінили чи прибрали — запропонуйте заново')
                rfq_columns.delete(req, x['key'])
            for x in d.get('edit') or []:
                col = cols.get(x['key'])
                if col is None or col['name'] != x['old_name'] or col['hint'] != x['old_hint']:
                    raise _Refused(f'колонку «{x["old_name"]}» уже змінили — запропонуйте заново')
                rfq_columns.edit(req, x['key'], x.get('name'), x.get('hint'))
            added = []
            for x in d.get('add') or []:
                col = rfq_columns.add(req, x['name'], x['kind'], x.get('hint', ''))
                added.append(col['name'])
                for v in x.get('values') or []:
                    r = R.objects.filter(request=req, pk=v['recipient_id']).select_related('request').first()
                    if r is None:
                        raise _Refused(f'{v["supplier"]} уже немає серед отримувачів запиту')
                    _write(r, col, v['value'], user)
    except (_Refused, rfq_columns.ColumnError) as e:
        return str(e)
    return ''


def _write(r, col, value, user):
    from suppliers.services import rfq_columns
    if col['kind'] == 'text':
        rfq_columns.set_value(r, col['key'], None, value or '', user, src='agent')
        return
    for q, v in sorted((value or {}).items(), key=lambda kv: int(kv[0])):
        rfq_columns.set_value(r, col['key'], q, '' if v is None else v, user, src='agent')


def _apply_values(p, req, d, user):
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services import rfq_columns
    try:
        with transaction.atomic():
            r = R.objects.select_for_update().select_related('request').filter(
                request=req, pk=d.get('recipient_id') or 0).first()
            if r is None:
                raise _Refused(f'{d.get("supplier") or "постачальника"} уже немає серед отримувачів запиту')
            cols = {c['key']: c for c in rfq_columns.columns(req)}
            for key, x in (d.get('values') or {}).items():
                col = cols.get(key)
                if col is None or col['name'] != x['name'] or col['kind'] != x['kind']:
                    raise _Refused(f'колонку «{x["name"]}» уже змінили чи прибрали — запропонуйте заново')
                now, old, new = current(r, col), x.get('old'), x.get('new')
                if col['kind'] == 'text':
                    moved = now != (old or '')
                else:                     # лише кількості, які змінюємо: інші могли вписати тим часом — не заважає
                    moved = any(rfq_columns.number(now.get(q)) != rfq_columns.number((old or {}).get(q))
                                for q in (new or {}))
                if moved:
                    raise _Refused(f'«{col["name"]}» у {r.supplier.name} уже змінили — запропонуйте заново')
                _write(r, col, new, user)
    except (_Refused, rfq_columns.ColumnError) as e:
        return str(e)
    return ''


def _apply_marks(p, req, d, user):
    from suppliers.services import rfq_marks
    rids = [str(x) for x in d.get('recipients') or []]
    have = set(str(x) for x in req.recipients.filter(pk__in=[int(x) for x in rids if x.isdigit()])
               .values_list('pk', flat=True))
    if not have:
        return 'цих постачальників уже немає серед отримувачів запиту'
    op = dict(d.get('op') or {})
    stickers = op.pop('stickers', [])
    if op:
        rfq_marks.apply(req, list(have), user, op)
    for key, on in stickers:
        rfq_marks.apply(req, list(have), user, {'sticker': key, 'on': bool(on)})
    if d.get('rank') is not None and len(have) == 1:
        rfq_marks.set_rank(req, next(iter(have)), d['rank'], user)
    return ''


APPLY = {'columns': _apply_columns, 'xvalues': _apply_values, 'marks': _apply_marks}
LABELS = {'columns': '➕ Свої колонки таблиці', 'xvalues': '➕ Значення своїх колонок', 'marks': '🏷 Позначка рядків'}
