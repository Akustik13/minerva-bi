"""📦 Коробки (пакувальні матеріали) для агента й MCP (agent/toolsets.py): каталог, використання, схожі розміри,
пропозиції створити / змінити / оприбуткувати / злити — застосовує людина ✓.

Логіка — shipping/services/packaging_usage.py (використання, схожі, злиття) і packaging_stock.py (прихід,
інвентаризація). Читання — `shipping.view_packagingmaterial`, пропозиції — add / change.
"""
from decimal import Decimal, InvalidOperation

from agent.models import AgentProposal
from agent.toolsets import clamp, one_line, propose, require

VIEW_PERM = 'shipping.view_packagingmaterial'
ADD_PERM = 'shipping.add_packagingmaterial'
CHANGE_PERM = 'shipping.change_packagingmaterial'
LIST_MAX = 80
FIELDS = ('name', 'code', 'box_type', 'size_class', 'length_cm', 'width_cm', 'height_cm', 'fold_heights',
          'tare_weight_kg', 'max_weight_kg', 'cost', 'min_stock', 'track_stock', 'is_active', 'notes', 'buy_url')
NUMBERS = ('length_cm', 'width_cm', 'height_cm', 'tare_weight_kg', 'max_weight_kg', 'cost')

_BOX = {'type': 'string', 'description': 'Коробка: #id або код (напр. BX-M-02)'}
_EVIDENCE = {'type': 'string', 'description': 'Чому: цифри використання, розміри, лист постачальника…'}

LIST = {'name': 'list_packaging',
        'description': 'Каталог коробок / конвертів / тубусів: #id, код, назва, тип, розміри (Д × Ш × В, складається до '
                       'висот), вага тари, макс. вага вмісту, ціна, запас на складі й мінімум, і скільки використали '
                       'за останній рік. Фільтри: слова, лише активні / архів / усі, лише «мало на складі».',
        'input_schema': {'type': 'object', 'properties': {
            'query': {'type': 'string'}, 'active': {'type': 'string', 'enum': ['active', 'archived', 'all']},
            'low_stock': {'type': 'boolean'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': LIST_MAX}}}}
USAGE = {'name': 'packaging_usage',
         'description': 'Скільки коробок кожного виду використали за days днів (оформлені відправлення від нас): штук, '
                        'у скількох відправленнях, коли востаннє, скільки товарів рекомендують цю коробку; невживані; '
                        'розміри посилок, яким не відповідає жодна коробка (кандидати в каталог).',
         'input_schema': {'type': 'object', 'properties': {'days': {'type': 'integer', 'minimum': 1,
                                                                    'maximum': 3650}}}}
SIMILAR = {'name': 'similar_packaging',
           'description': 'Групи схожих коробок одного типу, де кожен вимір відрізняється не більше ніж на ±tolerance_cm '
                          '(у будь-якій орієнтації) — кандидати на об\'єднання; у кожній групі — у яку зливати '
                          '(найчастіше вживана). Об\'єднати — propose_packaging_merge.',
           'input_schema': {'type': 'object', 'properties': {
               'tolerance_cm': {'type': 'number', 'minimum': 0, 'maximum': 20},
               'include_archived': {'type': 'boolean'}}}}
MERGE = {'name': 'propose_packaging_merge',
         'description': 'Запропонувати злити коробки в одну (людина підтвердить ✓): рекомендації товарів і записи '
                        '«упаковано» переходять на ціль, запас — теж, злиті — в архів (нічого не видаляється). Лише '
                        'один тип (коробка з коробкою). Злити можна й архівну коробку, ціль — лише активна. Після '
                        'злиття відправлення з розмірами злитої коробки рахуються й списуються як ціль. Злиття '
                        'записується в історію (packaging_usage) — людина може його відкотити.',
         'input_schema': {'type': 'object', 'properties': {
             'target': _BOX, 'merge': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Які злити'},
             'evidence': _EVIDENCE}, 'required': ['target', 'merge', 'evidence']}}
CHANGE = {'name': 'propose_packaging',
          'description': 'Запропонувати (людина підтвердить ✓): action create — нова коробка (розміри обов\'язкові), '
                         'update — змінити поля (name, code, box_type, size_class, length_cm, width_cm, height_cm, '
                         'fold_heights «22, 16», tare_weight_kg, max_weight_kg, cost, min_stock, track_stock, is_active '
                         '(false = в архів), notes, buy_url), receive — прийшло qty коробок (прихід на склад), count — '
                         'інвентаризація: фактично є qty.',
          'input_schema': {'type': 'object', 'properties': {
              'action': {'type': 'string', 'enum': ['create', 'update', 'receive', 'count']},
              'box': _BOX, 'fields': {'type': 'object', 'description': 'Поля для create / update'},
              'qty': {'type': 'integer', 'minimum': 0}, 'evidence': _EVIDENCE},
              'required': ['action', 'evidence']}}

TOOLS = [LIST, USAGE, SIMILAR, MERGE, CHANGE]
KINDS = {AgentProposal.PACKAGING: '📦 Коробка', AgentProposal.PACK_MERGE: '📦 Злити коробки'}
APPLY_PERMS = (ADD_PERM, CHANGE_PERM)


def tools_for(user) -> list:
    if user is None or not user.has_perm(VIEW_PERM):
        return []
    out = [LIST, USAGE, SIMILAR]
    if user.has_perm(CHANGE_PERM):
        out.append(MERGE)
    if user.has_perm(CHANGE_PERM) or user.has_perm(ADD_PERM):
        out.append(CHANGE)
    return out


def _box(value, active_only=False):
    from agent.tools import ToolError
    from shipping.models import PackagingMaterial
    value = str(value or '').strip().lstrip('#')
    qs = PackagingMaterial.objects.all()
    m = (qs.filter(pk=int(value)).first() if value.isdigit() else None) or qs.filter(code__iexact=value).first()
    if m is None:
        raise ToolError(f'коробки «{value}» немає (list_packaging)')
    if active_only and not m.is_active:
        raise ToolError(f'коробка {m} в архіві')
    return m


def _line(m, st=None) -> str:
    from shipping.services.packaging_usage import dims_text, stats_line
    bits = [f'#{m.pk}', m.code or '—', str(m), m.get_box_type_display(), f'{dims_text(m)} см']
    if m.fold_heights:
        bits.append(f'складається до {m.fold_heights}')
    if m.max_weight_kg:
        bits.append(f'до {m.max_weight_kg:g} кг')
    if m.cost:
        bits.append(f'{m.cost:g} за шт')
    if m.track_stock:
        bits.append(f'на складі {m.stock_qty}' + (f' (мін. {m.effective_min})' if m.effective_min is not None else '')
                    + (' ⚠️ мало' if m.is_low else ''))
    if not m.is_active:
        bits.append('🗄 архів' + (f' → злито в #{m.merged_into_id}' if m.merged_into_id else ''))
    line = ' · '.join(bits)
    return line + (f'\n   використано за рік: {stats_line(st)}' if st is not None else '')


def list_packaging(ctx, args) -> str:
    from django.db.models import Q
    from shipping.models import PackagingMaterial
    from shipping.services.packaging_usage import usage_cached as usage
    require(ctx.task.owner, VIEW_PERM)
    qs = PackagingMaterial.objects.all()
    active = args.get('active') or 'active'
    if active == 'active':
        qs = qs.filter(is_active=True)
    elif active == 'archived':
        qs = qs.filter(is_active=False)
    for word in str(args.get('query') or '').split()[:5]:
        qs = qs.filter(Q(name__icontains=word) | Q(code__icontains=word) | Q(notes__icontains=word))
    items = list(qs.order_by('box_type', 'length_cm', 'width_cm', 'height_cm'))
    if args.get('low_stock'):
        items = [m for m in items if m.is_low]
    limit = clamp(args.get('limit'), 40, 1, LIST_MAX)
    stats = usage(365)['materials']
    lines = [f'Коробок: {len(items)}' + (f' — показано {limit}' if len(items) > limit else '')]
    lines += [_line(m, stats.get(m.pk)) for m in items[:limit]]
    return '\n'.join(lines)


def packaging_usage(ctx, args) -> str:
    from shipping.models import PackagingMaterial
    from shipping.services.packaging_usage import history, stats_line, usage
    require(ctx.task.owner, VIEW_PERM)
    data = usage(clamp(args.get('days'), 365, 1, 3650))
    boxes = {m.pk: m for m in PackagingMaterial.objects.all()}
    used = sorted(((pk, st) for pk, st in data['materials'].items() if st['boxes']), key=lambda x: -x[1]['boxes'])
    idle = [boxes[pk] for pk, st in data['materials'].items()
            if not st['boxes'] and pk in boxes and boxes[pk].is_active]
    lines = [f'За {data["days"]} дн. оформлених відправлень: {data["shipments"]}. Використано коробок:']
    lines += [f'- {_line(boxes[pk])}\n   {stats_line(st)}' for pk, st in used[:40]]
    if idle:
        lines.append(f'\nАктивні, але не використовувались за цей час ({len(idle)}):')
        lines += [f'- #{m.pk} {m.code or "—"} {m}' for m in idle[:30]]
    merges = history(limit=10)
    if merges:
        lines.append('\nОстанні злиття коробок (ціль ← злиті; до злиття разом → зараз у цілі):')
        for h in merges:
            done = ' · ↩ відкочено' if h['undone'] else ''
            lines.append(f"- #{h['merge'].pk} {h['at']:%d.%m.%Y}: #{h['merge'].target_id} ← "
                         + ', '.join(f"#{b.get('id')} {b.get('dims', '')}" for b in h['before'][1:])
                         + f"; {h['total'].get('boxes', 0)} шт → {h['now']} шт{done}")
    if data['unmatched']:
        lines.append('\nРозміри посилок без коробки в каталозі (Д × Ш × В см, кандидати додати — propose_packaging):')
        lines += [f'- {u["dims"]}: {u["boxes"]} шт у {u["shipments"]} відправл.' for u in data['unmatched']]
    return '\n'.join(lines)


def similar_packaging(ctx, args) -> str:
    from shipping.services.packaging_usage import dims_text, similar
    from shipping.services.packaging_usage import usage_cached as usage
    require(ctx.task.owner, VIEW_PERM)
    try:
        tol = Decimal(str(args.get('tolerance_cm', 1)))
    except (InvalidOperation, ValueError):
        tol = Decimal('1')
    stats = usage(365)['materials']
    groups = similar(tol, bool(args.get('include_archived')), stats)
    if not groups:
        return f'Схожих коробок (±{tol:g} см) немає.'
    lines = [f'Групи схожих коробок (±{tol:g} см кожен вимір): {len(groups)}']
    for g in groups[:30]:
        lines.append(f'\n▶ злити в #{g["target"].pk} {g["target"].code or ""} {g["target"]} · різниця Д/Ш/В: '
                     f'{" / ".join(g["spread"])} см')
        for m in g['members']:
            st = stats.get(m.pk, {})
            lines.append(f'  - #{m.pk} {m.code or "—"} {dims_text(m)} см · {st.get("boxes", 0)} шт за рік · '
                         f'рекомендацій {st.get("recs", 0)}' + ('' if m.is_active else ' · 🗄 архів'))
    return '\n'.join(lines)


def propose_merge(ctx, args):
    from agent.tools import ToolError
    from shipping.services.packaging_usage import dims_text
    require(ctx.task.owner, CHANGE_PERM)
    target = _box(args.get('target'), active_only=True)
    others = []
    for value in args.get('merge') or []:
        m = _box(value)
        if m.pk != target.pk and m.pk not in {o.pk for o in others}:
            others.append(m)
    if not others:
        raise ToolError('які коробки злити?')
    bad = [str(o) for o in others if o.box_type != target.box_type]
    if bad:
        raise ToolError('різні типи: ' + ', '.join(bad))
    propose(ctx, AgentProposal.PACK_MERGE, field=str(target), old=', '.join(f'{o} ({dims_text(o)})' for o in others),
            new=f'{target} ({dims_text(target)})', evidence=args.get('evidence', ''),
            data={'target': target.pk, 'merge': [o.pk for o in others]})
    return f'Записано на перевірку: злити {len(others)} коробки в {target} — людина підтвердить ✓'


def _clean_fields(fields) -> dict:
    from agent.tools import ToolError
    from shipping.models import PackagingMaterial
    out = {}
    for k, v in (fields or {}).items():
        if k not in FIELDS:
            raise ToolError(f'поле {k} не змінюється (можна: {", ".join(FIELDS)})')
        if k in NUMBERS:
            try:
                v = None if v in (None, '') else str(Decimal(str(v).replace(',', '.')))
            except InvalidOperation:
                raise ToolError(f'{k}: не число')
        elif k in ('track_stock', 'is_active'):
            v = bool(v)
        elif k == 'min_stock':
            v = None if v in (None, '') else clamp(v, 0, 0, 100000)
        elif k == 'box_type' and v not in PackagingMaterial.BoxType.values:
            raise ToolError(f'box_type — одне з {", ".join(PackagingMaterial.BoxType.values)}')
        elif k == 'size_class' and v not in ('', *PackagingMaterial.SizeClass.values):
            raise ToolError(f'size_class — одне з {", ".join(PackagingMaterial.SizeClass.values)}')
        else:
            v = str(v or '')[:500]
        out[k] = v
    return out


def propose_change(ctx, args):
    from agent.tools import ToolError
    action = args.get('action')
    user = ctx.task.owner
    if action == 'create':
        require(user, ADD_PERM)
        fields = _clean_fields(args.get('fields'))
        if not all(fields.get(k) for k in ('length_cm', 'width_cm', 'height_cm')):
            raise ToolError('для нової коробки потрібні length_cm, width_cm, height_cm')
        _validate(None, fields)
        p = propose(ctx, AgentProposal.PACKAGING, field='нова', new=_fields_text(fields),
                    evidence=args.get('evidence', ''), data={'action': 'create', 'fields': fields})
        return f'Записано на перевірку: нова коробка {_fields_text(fields)} (#{p.pk})'
    require(user, CHANGE_PERM)
    m = _box(args.get('box'))
    if action == 'update':
        fields = _clean_fields(args.get('fields'))
        if not fields:
            raise ToolError('що змінити? fields')
        _validate(m, fields)
        old = {k: _show(getattr(m, k)) for k in fields}
        propose(ctx, AgentProposal.PACKAGING, field=str(m), old=_fields_text(old), new=_fields_text(fields),
                evidence=args.get('evidence', ''), data={'action': 'update', 'box': m.pk, 'fields': fields,
                                                         'old': old})
        return f'Записано на перевірку: змінити {m} — {_fields_text(fields)}'
    if action in ('receive', 'count'):
        if not m.track_stock:
            raise ToolError(f'для {m} не ведеться облік на складі (track_stock)')
        qty = clamp(args.get('qty'), -1, -1, 1000000)
        if qty < (1 if action == 'receive' else 0):
            raise ToolError('qty — скільки прийшло (> 0) або фактично є (≥ 0)')
        what = f'прийшло +{qty}' if action == 'receive' else f'фактично є {qty} (зараз {m.stock_qty})'
        propose(ctx, AgentProposal.PACKAGING, field=str(m), old=str(m.stock_qty), new=what,
                evidence=args.get('evidence', ''), data={'action': action, 'box': m.pk, 'qty': qty})
        return f'Записано на перевірку: {m} — {what}'
    raise ToolError('action — create, update, receive або count')


def _show(v) -> str:
    return '' if v is None else (f'{v.normalize():f}' if isinstance(v, Decimal) else str(v))


def _fields_text(fields) -> str:
    return ', '.join(f'{k}={one_line(v, 60)}' for k, v in fields.items())


def _validate(m, fields):
    """Модель скаже, що не так (висоти складання, обов'язкові поля, унікальний код) — ще до пропозиції."""
    from django.core.exceptions import ValidationError
    from agent.tools import ToolError
    from shipping.models import PackagingMaterial
    obj = PackagingMaterial() if m is None else PackagingMaterial.objects.get(pk=m.pk)
    for k, v in fields.items():
        setattr(obj, k, Decimal(v) if k in NUMBERS and v is not None else v)
    try:
        obj.full_clean(exclude=['stock_qty', 'stock_since', 'low_notified_at'])
    except ValidationError as e:
        raise ToolError('; '.join(f'{k}: {" ".join(v)}' for k, v in e.message_dict.items()))
    return obj


# ── ✓ людини ──────────────────────────────────────────────────────────────────

def apply(p, user) -> str:
    from agent.tools import ToolError
    from shipping.models import PackagingMaterial
    from shipping.services import packaging_stock, packaging_usage
    d = p.data or {}
    try:
        if p.kind == AgentProposal.PACK_MERGE:
            if not user.has_perm(CHANGE_PERM):
                return 'немає права змінювати коробки'
            target = PackagingMaterial.objects.filter(pk=d.get('target')).first()
            others = list(PackagingMaterial.objects.filter(pk__in=d.get('merge') or []))     # архівні теж
            if target is None:
                return 'коробки-цілі вже немає'
            if not others:
                return 'коробок для злиття вже немає'
            out = packaging_usage.merge(target, others, user=user, via='agent')
            p.result_url = f'/admin/shipping/packagingmaterial/{target.pk}/change/'
            p.new_value = (f'{p.new_value} · рекомендацій перенесено {out["moved_recs"]}, '
                           f'«упаковано» {out["moved_orders"]}, запас +{out["stock"]}')[:2000]
            p.save(update_fields=['result_url', 'new_value'])
            return ''
        action = d.get('action')
        if action == 'create':
            if not user.has_perm(ADD_PERM):
                return 'немає права додавати коробки'
            m = _validate(None, d.get('fields') or {})
            m.save()
            p.result_url = f'/admin/shipping/packagingmaterial/{m.pk}/change/'
            p.save(update_fields=['result_url'])
            return ''
        if not user.has_perm(CHANGE_PERM):
            return 'немає права змінювати коробки'
        m = PackagingMaterial.objects.filter(pk=d.get('box')).first()
        if m is None:
            return 'коробки вже немає'
        if action == 'update':
            now = {k: _show(getattr(m, k)) for k in (d.get('fields') or {})}
            if now != (d.get('old') or now):
                return 'коробку змінили після пропозиції — запропонуйте заново'
            obj = _validate(m, d.get('fields') or {})
            obj.save()
        elif action == 'receive':
            packaging_stock.receive(m, int(d.get('qty') or 0), user=user, note='🤖 агент: прихід')
        elif action == 'count':
            packaging_stock.count(m, int(d.get('qty') or 0), user=user, note='🤖 агент: інвентаризація')
        else:
            return 'невідома дія'
        p.result_url = f'/admin/shipping/packagingmaterial/{m.pk}/change/'
        p.save(update_fields=['result_url'])
        return ''
    except (ToolError, packaging_usage.MergeError, ValueError) as e:
        return str(e)


def describe(p) -> tuple:
    if p.kind == AgentProposal.PACK_MERGE:
        return '📦 Злити коробки', p.old_value, f'→ {p.new_value}'
    action = (p.data or {}).get('action')
    what = {'create': '📦 Нова коробка', 'update': '📦 Змінити коробку', 'receive': '📦 Прихід коробок',
            'count': '📦 Інвентаризація коробок'}.get(action, '📦 Коробка')
    return what, f'{p.field}: {p.old_value}' if p.old_value else p.field, p.new_value


HANDLERS = {'list_packaging': list_packaging, 'packaging_usage': packaging_usage,
            'similar_packaging': similar_packaging, 'propose_packaging_merge': propose_merge,
            'propose_packaging': propose_change}
