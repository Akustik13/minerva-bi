"""Що агент може запропонувати змінити в товарі — і як людина це застосовує (✓).

Лише поля з `FIELDS` (вага, HS-код, країна, виробник, назва для документів, строк поставки, посилання на даташит /
фото) і технічні параметри (`tech_attributes`, назвою людини, не службові коди). Ціни, залишки, видимість у магазині
й на DigiKey — ні (`risk`: це назовні). Значення перевіряється двічі: коли агент пропонує (`clean`) і перед записом.
Запис — `apply` з правом `inventory.change_product`: значення в базі змінилось після пропозиції — не перезаписуємо;
кожна зміна — AuditLog «Зміна» з «було → стало» й номером завдання.
"""
import re
from decimal import Decimal, InvalidOperation

from django.utils import timezone

FIELDS = {
    'net_weight_g':      ('Вага нетто, г/шт', 'grams'),
    'hs_code':           ('HS-код (митний)', 'hs'),
    'country_of_origin': ('Країна виробника (ISO-2)', 'country'),
    'manufacturer':      ('Виробник', 'text'),
    'name_export':       ('Назва (EN/DE) для документів', 'text'),
    'lead_time_days':    ('Термін поставки, днів', 'days'),
    'datasheet_url':     ('Посилання на даташит', 'url'),
    'image_url':         ('Посилання на фото', 'url'),
}
ATTR_NAME_MAX, ATTR_VALUE_MAX = 80, 300


class ValueError_(ValueError):
    pass


def current(product, field) -> str:
    value = getattr(product, field)
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f')
    return str(value)


def clean(field, value) -> str:
    """Значення від агента → рядок для запису; ValueError_ — пояснення агенту, що не так."""
    if field not in FIELDS:
        raise ValueError_(f'поле «{field}» змінювати не можна; можна: {", ".join(FIELDS)}')
    kind = FIELDS[field][1]
    raw = str(value if value is not None else '').strip()
    if not raw:
        raise ValueError_('порожнє значення')
    if kind == 'grams':
        try:
            num = Decimal(raw.replace(',', '.'))
        except InvalidOperation:
            raise ValueError_('вага — лише число в грамах (напр. 45 або 0.5)') from None
        if not (Decimal('0') < num < Decimal('1000000')):
            raise ValueError_('вага має бути більша за 0 і менша за 1 000 000 г')
        return format(num.quantize(Decimal('0.0001')).normalize(), 'f')
    if kind == 'hs':
        digits = re.sub(r'[\s.]', '', raw)
        if not re.fullmatch(r'\d{6,10}', digits):
            raise ValueError_('HS-код — 6…10 цифр (напр. 85177100)')
        return digits
    if kind == 'country':
        code = raw.upper()
        if not re.fullmatch(r'[A-Z]{2}', code):
            raise ValueError_('країна — двобуквений код ISO 3166 (DE, CN, US…)')
        return code
    if kind == 'days':
        if not raw.isdigit() or not (0 <= int(raw) <= 3650):
            raise ValueError_('строк — ціле число днів від 0 до 3650')
        return str(int(raw))
    if kind == 'url':
        if not re.match(r'https?://', raw) or len(raw) > 200 or any(c.isspace() for c in raw):
            raise ValueError_('посилання — http(s)://…, без пробілів, до 200 символів')
        return raw
    from inventory.models import Product
    limit = Product._meta.get_field(field).max_length or 255
    return ' '.join(raw.split())[:limit]


def clean_attribute(name, value) -> tuple:
    name = ' '.join(str(name or '').split())[:ATTR_NAME_MAX]
    value = ' '.join(str(value or '').split())[:ATTR_VALUE_MAX]
    if not name or not value:
        raise ValueError_('потрібні назва параметра й значення')
    if name[:1].islower() and ' ' not in name:
        raise ValueError_('службові коди DigiKey (camelCase) змінювати не можна — назвіть параметр по-людськи, '
                          'як у даташиті («Operating Temperature»)')
    return name, value


def label(proposal) -> str:
    if proposal.kind == proposal.ATTRIBUTE:
        return f'Параметр «{proposal.field}»'
    return FIELDS.get(proposal.field, (proposal.field,))[0]


def _audit(user, product, field, before, after, task):
    from core.models import AuditLog
    AuditLog.log(AuditLog.Action.UPDATE, module='inventory', model_name='Product', object_id=product.pk,
                 object_repr=product.sku, user=user,
                 extra={'field': field, 'before': before, 'after': after, 'via': 'agent', 'task': task.pk})


def apply(proposal, user) -> str:
    """✓ людини: записати поле / параметр. '' — готово, інакше — чому ні (стан FAILED з поясненням)."""
    from inventory.models import Product
    if not user.has_perm('inventory.change_product'):
        return 'немає права змінювати товари'
    product = Product.objects.filter(pk=proposal.product_id).first()
    if product is None:
        return 'товару вже немає'
    try:
        if proposal.kind == proposal.FIELD:
            value = clean(proposal.field, proposal.new_value)
            before = current(product, proposal.field)
            if before != proposal.old_value:
                return f'значення змінилось після пропозиції (зараз «{before or "—"}») — запустіть завдання ще раз'
            setattr(product, proposal.field, Decimal(value) if FIELDS[proposal.field][1] == 'grams'
                    else int(value) if FIELDS[proposal.field][1] == 'days' else value)
            product.save(update_fields=[proposal.field])
            _audit(user, product, proposal.field, before, value, proposal.task)
        else:
            name, value = clean_attribute(proposal.field, proposal.new_value)
            attrs = dict(product.tech_attributes or {})
            before = str(attrs.get(name, '') or '')
            if before != proposal.old_value:
                return f'параметр змінився після пропозиції (зараз «{before or "—"}»)'
            attrs[name] = value
            product.tech_attributes = attrs
            product.save(update_fields=['tech_attributes'])
            _audit(user, product, f'tech_attributes.{name}', before, value, proposal.task)
    except ValueError_ as e:
        return str(e)
    return ''


def decided(proposal, user, status, error=''):
    proposal.status, proposal.error = status, (error or '')[:500]
    proposal.decided_by, proposal.decided_at = user, timezone.now()
    proposal.save(update_fields=['status', 'error', 'decided_by', 'decided_at', 'result_url'])
