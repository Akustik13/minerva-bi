"""🧩 «Це може зробити агент» — підказка у віконці «Minerva» (rag_assistant) під повідомленням людини.

Без AI, за словами: прохання ЗРОБИТИ роботу (знайди / внеси / додай / підготуй / надішли / постав …) над товарами,
даташитами, складом, листом, відправленнями, магазином, DigiKey чи календарем → кнопка «🧩 Підготувати завдання
агенту»: форма нового завдання (`/admin/agent/agenttask/add/?instruction=…&ids=…`) з цим текстом і товарами, чиї
артикули є в тексті; запускає людина кнопкою «Зберегти». «Відповідай на такі листи …» → «🤖 Доручення агента»
(правий клік на листі в пошті). Питання «як / де / чи …» — не завдання: на них відповідає гід. Кнопку бачить лише
людина з правом давати завдання / доручення (`agent.add_agenttask` / `agent.add_agentrule`).
«Відправ замовлення 102497269» / «що відправити сьогодні?» — першими (`ship`): картка агента відправки й кнопка на
сторінку відправлення чи черги «📦 Відправка сьогодні» (права — замовлення й відправлення).
"""
import re
from urllib.parse import urlencode

INSTRUCTION_MAX = 3000
PRODUCTS_MAX = 200

_ASK_RE = re.compile(r'^\W*(як|де|чи|що|навіщо|нащо|чому|коли|скільки|який|яка|яке|які|how|where|what|why|wie|wo|'
                     r'was|warum)(?!\w)', re.I)
_HOWTO_RE = re.compile(r'(?<!\w)(як|де|how|where)\s+(\w+\s+){0,2}(знайти|внести|додати|заповнити|оновити|перенести|'
                       r'підготувати|надіслати|поставити|find|add)(?!\w)', re.I)
_DO_RE = re.compile(
    r'(?<!\w)(знайди|знайдіть|знайти|внеси|внесіть|внести|додай|додайте|додати|заповни|заповніть|заповнити|онови|'
    r'оновіть|оновити|перенеси|перенесіть|перенести|перевір|перевірте|прочитай|прочитайте|перебери|переберіть|'
    r'витягни|підготуй|підготуйте|підготувати|надішли|надішліть|надіслати|відправ|відправте|постав|поставте|'
    r'поставити|сховай|сховайте|створи|створіть|оприбуткуй|оприбуткуйте|'
    r'найди|добавь|заполни|обнови|подготовь|отправь)(?!\w)', re.I)
_OBJECT_RE = re.compile(
    r'(даташит|datasheet|datenblatt|параметр|характеристик|ваг[аиуі]|\bhs\b|країн|склад|товар|артикул|лист|e-?mail|'
    r'імейл|lieferschein|накладн|прихід|відправлен|замовлен|трек|магазин|\bshop|digikey|календар|зустріч|дедлайн|'
    r'ціни|ціну|строк)', re.I)
_RULE_RE = re.compile(r'(на (такі|ці|подібні|всі|усі) лист|на листи (від|з|типу|про)|завжди відповідай|'
                      r'відповідай (сам|сама|автоматично)|автовідповід|відповідати (на такі|самому|самій|автоматично))',
                      re.I)
_SKU_RE = re.compile(r'(?<![\w./-])([A-Za-z0-9][A-Za-z0-9._/-]{2,62}[A-Za-z0-9])(?![\w-])')


def is_rule(text) -> bool:
    return bool(_RULE_RE.search(str(text or '')))


def is_task(text) -> bool:
    """Прохання зробити роботу, а не запитання «як …»."""
    text = str(text or '').strip()
    if not text or _ASK_RE.match(text) or _HOWTO_RE.search(text):
        return False
    return bool(_DO_RE.search(text) and _OBJECT_RE.search(text))


def product_ids(text) -> list:
    """Товари, чиї артикули (з цифрами й літерами) є в тексті — за порядком згадки, без повторів."""
    from inventory.models import Product
    from inventory.services.stock import resolve_sku
    out = []
    for token in _SKU_RE.findall(str(text or '')):
        if not (any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)):
            continue
        product = Product.objects.filter(sku__iexact=token).only('pk').first() or resolve_sku(token)
        if product is not None and product.pk not in out:
            out.append(product.pk)
        if len(out) >= PRODUCTS_MAX:
            break
    return out


_SHIP_VERB_RE = re.compile(r'(?<!\w)(відправ\w*|оформи\w*|надішли|надіслати|ship\w*|send|versend\w*|verschick\w*|'
                           r'отправ\w*)(?!\w)', re.I)
_SHIP_OBJ_RE = re.compile(r'(?<!\w)(замовлен\w*|посилк\w*|order\w*|bestellung\w*|auftr[aä]g\w*|заказ\w*)(?!\w)', re.I)
_SHIP_QUEUE_RE = re.compile(r'(відправ\w*|відвантаж\w*|ship\w*|versand|versend\w*)\W+(\w+\W+){0,3}'
                            r'(сьогодні|today|heute|черг\w*)|(черг\w*|queue)\W+(\w+\W+){0,2}(відправ\w*|ship\w*|versand)',
                            re.I)
_NUMBER_RE = re.compile(r'(?<![\w-])#?([A-Za-z0-9][A-Za-z0-9_-]{2,40})(?![\w-])')


def _ship_order(text):
    """Замовлення, номер якого є в тексті (з цифрою): спершу те, що чекає відправки, інакше найновіше."""
    from sales.models import SalesOrder
    from sales.revenue import to_ship
    for token in _NUMBER_RE.findall(text):
        if not any(ch.isdigit() for ch in token):
            continue
        qs = SalesOrder.objects.filter(order_number__iexact=token)
        order = to_ship(qs).order_by('-pk').first() or qs.order_by('-pk').first()
        if order is not None:
            return order
    return None


def ship(text, user) -> dict | None:
    """🚚 Агент відправки: «відправ замовлення 102497269» → картка з тим, що агент знає, і кнопкою на сторінку
    відправлення (нове / чернетка / «що далі» / замовлення); «що відправити сьогодні?» → черга «📦 Відправка сьогодні».
    Оформлює людина на сторінці. None — не про відправку або немає права."""
    from django.urls import reverse
    if not user.has_perm('sales.view_salesorder') or not user.has_perm('shipping.view_shipment'):
        return None
    if _SHIP_QUEUE_RE.search(text) and not _ship_order(text):
        from shipping.services.ship_advice import next_order
        nxt, left = next_order()
        head = (f'Чекають відправки: {left}. Найтерміновіше — {nxt.order_number}'
                + (f' (до {nxt.shipping_deadline:%d.%m.%Y})' if nxt.shipping_deadline else '') + '. ') if nxt \
            else 'Замовлень, що чекають відправки, немає. '
        return {'kind': 'ship', 'title': '🚚 Агент відправки',
                'text': head + 'У черзі — що оформити й що завершити, від найтерміновішого.',
                'label': '📦 Відправка сьогодні', 'url': reverse('admin:shipping_shipment_ship_today')}
    if not (_SHIP_VERB_RE.search(text) and _SHIP_OBJ_RE.search(text)):
        return None
    order = _ship_order(text)
    if order is None:
        return None
    from shipping.services import ship_advice
    name = f'{order.order_number} ({order.client or order.email or "—"}, {ship_advice.country(order) or "—"})'
    title = '🚚 Агент відправки'
    if order.status == 'cancelled':
        return {'kind': 'ship', 'title': title, 'text': f'Замовлення {name} скасовано — відправляти нічого.',
                'label': '📋 Відкрити замовлення', 'url': reverse('admin:sales_salesorder_change', args=[order.pk])}
    booked = order.shipments.filter(status__in=ship_advice.SHIPPED).select_related('carrier').order_by('-pk').first()
    if order.status in ('shipped', 'delivered') or order.shipped_at:
        url = (reverse('admin:shipping_shipment_detail', args=[booked.pk]) if booked
               else reverse('admin:sales_salesorder_change', args=[order.pk]))
        when = f' {order.shipped_at:%d.%m.%Y}' if order.shipped_at else ''
        return {'kind': 'ship', 'title': title, 'text': f'Замовлення {name} уже відправлено{when}.',
                'label': '📦 Відкрити відправлення' if booked else '📋 Відкрити замовлення', 'url': url}
    if booked:
        carrier = booked.carrier.name if booked.carrier else '—'
        tracking = f', {booked.tracking_number}' if booked.tracking_number else ''
        return {'kind': 'ship', 'title': title,
                'text': f'Замовлення {name} уже оформлено в перевізника ({carrier}{tracking}). Залишилось: друк, '
                        f'«✅ Відправлено», лист клієнту.',
                'label': '➡️ Що далі', 'url': reverse('admin:shipping_shipment_detail', args=[booked.pk])}
    draft = order.shipments.filter(status='draft').order_by('-pk').first()
    if draft is not None:
        return {'kind': 'ship', 'title': title, 'text': f'Для замовлення {name} уже є чернетка відправлення #{draft.pk}.',
                'label': f'✏️ Продовжити чернетку #{draft.pk}',
                'url': reverse('admin:shipping_shipment_change', args=[draft.pk])}
    if not user.has_perm('shipping.add_shipment'):
        return None
    found = ship_advice.checks(order, shipments=[])
    blocks = [c['text'] for c in found if c['level'] == 'block']
    warns = [c['text'] for c in found if c['level'] == 'warn']
    state = ('⛔ ' + '; '.join(blocks) if blocks else '⚠️ ' + '; '.join(warns[:3]) if warns
             else '✓ замовлення готове до відправки')
    return {'kind': 'ship', 'title': title,
            'text': f'Замовлення {name}: {state}. На сторінці відправлення агент запитає тарифи всіх перевізників і '
                    f'порадить одного — оформлюєте ви; далі — друк, «Відправлено» й лист клієнту.',
            'label': f'🚚 Відправити {order.order_number}',
            'url': reverse('admin:shipping_shipment_create', args=[order.pk])}


def suggest(text, user) -> dict | None:
    """{kind ship | task | rule, title, text, label, url} або None — що показати під повідомленням людини."""
    text = str(text or '').strip()
    if not text or user is None or not getattr(user, 'is_active', False):
        return None
    shipping = ship(text, user)
    if shipping is not None:
        return shipping
    if is_rule(text):
        if not user.has_perm('agent.add_agentrule'):
            return None
        return {'kind': 'rule', 'title': '🤖 Це — доручення агентові в пошті',
                'text': 'Відкрийте лист такого типу в пошті → правий клік → «🤖 Доручити агенту: відповідати на такі '
                        'листи…»: умови, як відповідати, «🧪 Спробувати» покаже відповідь на цей лист. Спершу агент '
                        'готує чернетки — надсилаєте ви.',
                'label': '📥 Відкрити пошту', 'url': '/email/'}
    if not is_task(text) or not user.has_perm('agent.add_agenttask'):
        return None
    from django.urls import reverse
    ids = product_ids(text)
    query = {'instruction': text[:INSTRUCTION_MAX]}
    if ids:
        query['ids'] = ','.join(str(pk) for pk in ids)
    found = f', вибрано товарів з артикулів у тексті: {len(ids)}' if ids else ''
    return {'kind': 'task', 'title': '🧩 Це може зробити агент Minerva',
            'text': 'Він прочитає картки товарів, даташити чи лист і запропонує зміни таблицею «було → стане» — '
                    f'застосовуєте ви ✓. Форма завдання вже заповнена вашим текстом{found}. Перевірте й натисніть '
                    '«Зберегти» — агент почне працювати, а коли закінчить, повідомить вас.',
            'label': '🧩 Підготувати завдання агенту', 'url': reverse('admin:agent_agenttask_add') + '?' + urlencode(query)}
