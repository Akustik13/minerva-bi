"""🤖 Агент відправки — перевірка замовлення перед відправкою й вибір перевізника (без AI, за правилами).

`checks(order)` — що завадить відправити: block (скасовано, уже відправлено, немає адреси / країни) і warn
(уже є відправлення, телефон, нестача складу, позиція без товару, HS-код поза ЄС, вага 1 шт); `problems` — лише тексти
(ним користується агент, agent/shipments.py).

`recommend(order, results)` — з тарифів «Порівняти тарифи» (`ShipmentAdmin.compare_rates_view`, усі активні
перевізники) вибирає один варіант і пояснює чому:
1. перевізник, якого вибрав клієнт (`SalesOrder.shipping_courier` — DigiKey пише його сам);
2. як відправляли цьому клієнту, потім — у цю країну (успішні відправлення за `HISTORY_DAYS`, той самий перевізник і
   послуга);
3. найдешевший з доставкою ≤ `FAST_DAYS` днів; 4. найдешевший.
Ціни порівнюються у валюті Minerva (`offers.pricing.fx`); варіант без курсу в «найдешевший» не потрапляє. Оформлює
людина — та сама кнопка «🚀 …» на сторінці нового відправлення, нічого не надсилається звідси.

`after_booking(shipment, user)` — що далі після оформлення (панель на сторінці відправлення): друк (PDF перевізника з
документами, етикетки DYMO — `order_labels`), «✅ Відправлено», лист клієнту (`mail_state`), наступне замовлення
(`next_order`).
"""
from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

BLOCKING = ('draft', 'submitted', 'label_ready', 'in_transit', 'delivered')   # такі відправлення — нове не потрібне
SHIPPED = ('submitted', 'label_ready', 'in_transit', 'delivered')             # для історії «як відправляли»
HISTORY_DAYS = 365
FAST_DAYS = 5
COURIER_TYPES = {'ups': 'ups', 'dhl': 'dhl', 'fedex': 'fedex'}


# ── Замовлення ───────────────────────────────────────────────────────────────

def blocking(order) -> list:
    """Відправлення, через які друге робити не треба. Посилки частини замовлення (рядки ShipmentLine) — ні: поки є
    залишок, наступну посилку створюють свідомо (shipping/services/parcels.py)."""
    qs = order.shipments.filter(status__in=BLOCKING)
    from shipping.services import parcels
    if parcels.has_parcels(order) and parcels.progress(order).to_pack > 0:
        qs = qs.filter(lines__isnull=True)
    return list(qs.select_related('carrier').distinct().order_by('-pk'))


def parcel_remainder(order) -> str:
    """«AN… × 50» — що з замовлення, відправленого частинами, ще не відправлено ('' — посилок немає / усе)."""
    from shipping.services import parcels
    if not parcels.has_parcels(order):
        return ''
    return parcels.progress(order).remainder_text()


def eu() -> set:
    from shipping.services.jumingo import JumingoService
    return set(JumingoService._EU_COUNTRIES)


def country(order) -> str:
    from labels_app.order_label import country as order_country
    return order_country(order)


def hs(product) -> str:
    if product.hs_code:
        return product.hs_code
    from inventory.models import ProductCategory
    cat = ProductCategory.objects.filter(slug=product.category).only('customs_hs_code').first() \
        if product.category else None
    return getattr(cat, 'customs_hs_code', '') or ''


def cover(order) -> dict:
    from inventory.services.stock import order_lines_cover
    try:
        return order_lines_cover(order)
    except Exception:  # noqa: BLE001 — без складу перевірка однаково корисна
        return {}


def num(value) -> str:
    try:
        return format(Decimal(value).normalize(), 'f')
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def checks(order, stock=None, shipments=None) -> list:
    """[{level block | warn, text}] — що завадить відправити це замовлення."""
    out = []

    def add(level, text):
        out.append({'level': level, 'text': text})

    if order.status == 'cancelled':
        add('block', 'замовлення скасовано')
    rest = parcel_remainder(order)
    if rest and order.status not in ('shipped', 'delivered', 'cancelled'):
        add('warn', f'відправляється частинами — ще не відправлено: {rest}')
    elif order.status in ('shipped', 'delivered') or order.shipped_at:
        add('block', 'вже відправлено')
    for s in shipments if shipments is not None else blocking(order):
        add('warn', f'вже є відправлення #{s.pk} ({s.get_status_display()})')
    if not (order.addr_street or order.addr_city or (order.shipping_address or '').strip()):
        add('block', 'немає адреси доставки')
    code = country(order)
    if not code:
        add('block', 'не визначено країну доставки')
    if not (order.ship_phone or order.phone):
        add('warn', 'немає телефону отримувача (UPS / DHL для міжнародних відправлень вимагають)')
    lines = list(order.lines.select_related('product'))
    stock = stock if stock is not None else cover(order)
    for line in lines:
        c = stock.get(line.pk)
        if line.product_id is None:
            add('warn', f'позиція {line.sku_raw or "?"} без товару складу')
        elif c is not None and c['free'] < (line.qty or 0):
            add('warn', f'бракує {line.product.sku}: потрібно {num(line.qty)}, вільно {num(c["free"])}')
    if code and code not in eu():
        no_hs = [l.product.sku for l in lines if l.product_id and not hs(l.product)]
        if no_hs:
            add('warn', 'поза ЄС — немає HS-коду для митниці: ' + ', '.join(no_hs[:10]))
    no_weight = [l.product.sku for l in lines if l.product_id and not l.product.net_weight_g]
    if no_weight:
        add('warn', 'немає ваги 1 шт (вага відправлення буде приблизна): ' + ', '.join(no_weight[:10]))
    return out


def problems(order, stock=None, shipments=None) -> list:
    return [c['text'] for c in checks(order, stock, shipments)]


# ── Вибір перевізника ────────────────────────────────────────────────────────

def options(results) -> list:
    """Рядки тарифів «Порівняти тарифи» → плоский список варіантів (лише з ціною й без помилки перевізника)."""
    out = []
    for entry in results or []:
        if not isinstance(entry, dict) or entry.get('error') or entry.get('note'):
            continue
        for p in entry.get('products') or []:
            if not isinstance(p, dict) or p.get('price') in (None, ''):
                continue
            try:
                price = Decimal(str(p['price']))
            except (InvalidOperation, ValueError):
                continue
            days = p.get('transit_days')
            out.append({'carrier_id': entry.get('carrier_id'), 'carrier_type': str(entry.get('carrier_type') or ''),
                        'carrier_name': str(entry.get('carrier_name') or ''), 'code': str(p.get('code') or ''),
                        'name': str(p.get('name') or ''), 'price': price,
                        'currency': str(p.get('currency') or 'EUR').upper()[:3],
                        'transit_days': int(days) if str(days or '').isdigit() else None,
                        'delivery_date': str(p.get('delivery_date') or '')})
    return out


def _home(price, currency):
    from offers.pricing import fx, home_currency
    rate = fx(currency, home_currency())
    return None if rate is None else (price * rate).quantize(Decimal('0.01'))


def _cheapest(opts):
    priced = [o for o in opts if o.get('home') is not None]
    return min(priced, key=lambda o: (o['home'], o['transit_days'] or 99)) if priced else None


def _fastest(opts):
    timed = [o for o in opts if o['transit_days'] is not None]
    return min(timed, key=lambda o: (o['transit_days'], o['home'] if o.get('home') is not None else 10**9)) \
        if timed else None


def _same_service(opt, service) -> bool:
    service = (service or '').strip().lower()
    return bool(service) and (opt['name'].lower() == service or service in opt['name'].lower()
                              or opt['name'].lower() in service)


def history(order) -> dict:
    """{customer: Counter, country: Counter} — (тип перевізника, послуга) успішних відправлень за HISTORY_DAYS."""
    from shipping.models import Shipment
    since = timezone.now() - timedelta(days=HISTORY_DAYS)
    base = Shipment.objects.filter(status__in=SHIPPED, created_at__gte=since, carrier__isnull=False,
                                   direction=Shipment.OUT).exclude(order=order).select_related('carrier')
    who = None
    if order.customer_key:
        who = base.filter(order__customer_key=order.customer_key)
    elif order.email:
        who = base.filter(order__email__iexact=order.email)
    code = country(order)
    count = lambda qs: Counter((s.carrier.carrier_type, (s.carrier_service or '').strip()) for s in qs[:300])  # noqa: E731
    return {'customer': count(who) if who is not None else Counter(),
            'country': count(base.filter(recipient_country__iexact=code)) if code else Counter()}


def _from_history(opts, counter):
    """Найчастіший (перевізник, послуга) з історії, який є серед тарифів → (варіант, скільки разів)."""
    for (ctype, service), times in counter.most_common():
        same = [o for o in opts if o['carrier_type'] == ctype]
        exact = [o for o in same if _same_service(o, service)]
        pick = _cheapest(exact) or (exact[0] if exact else None) or (_cheapest(same) if not service else None)
        if pick is not None:
            return pick, times, bool(exact)
    return None, 0, False


def _customer_choice(order, opts):
    from sales.utils import normalize_courier
    wanted = normalize_courier(order.shipping_courier or '').lower()
    ctype = next((t for key, t in COURIER_TYPES.items() if key in wanted), '')
    if not ctype:
        return None, ''
    same = [o for o in opts if o['carrier_type'] == ctype] or \
        [o for o in opts if o['carrier_type'] == 'jumingo' and o['name'].lower().startswith(ctype)]
    if not same:
        return None, ''
    past = history(order)                                 # послуга — як цьому клієнту / в цю країну цим перевізником
    for scope in ('customer', 'country'):
        found, _times, exact = _from_history(same, Counter({k: v for k, v in past[scope].items() if k[0] == ctype}))
        if found is not None and exact:
            return found, f'{order.shipping_courier}», послуга — як минулого разу'
    return (_cheapest(same) or same[0]), f'{order.shipping_courier}», найдешевша послуга — яку саме оплатив ' \
                                         f'клієнт, у замовленні не сказано'


def recommend(order, results) -> dict | None:
    """{pick, reason, rule, cheapest, fastest, extra} або None, коли жодного тарифу немає."""
    opts = options(results)
    if not opts:
        return None
    for o in opts:
        o['home'] = _home(o['price'], o['currency'])
    cheapest, fastest = _cheapest(opts), _fastest(opts)
    pick, reason, rule = None, '', ''
    chosen, why = _customer_choice(order, opts)
    if chosen is not None:
        pick, rule, reason = chosen, 'customer', f'клієнт вибрав «{why}'
    if pick is None:
        past = history(order)
        found, times, exact = _from_history(opts, past['customer'])
        if found is not None:
            pick, rule = found, 'history_customer'
            reason = f'так відправляли цьому клієнту ({times} раз.)' if exact else \
                f'цьому клієнту відправляли {found["carrier_name"]} ({times} раз.)'
        else:
            found, times, exact = _from_history(opts, past['country'])
            if found is not None:
                pick, rule = found, 'history_country'
                where = country(order)
                reason = f'так відправляли в {where} ({times} раз.)' if exact else \
                    f'в {where} відправляли {found["carrier_name"]} ({times} раз.)'
    if pick is None:
        fast = [o for o in opts if o['transit_days'] is not None and o['transit_days'] <= FAST_DAYS]
        pick = _cheapest(fast)
        if pick is not None:
            rule, reason = 'fast_cheap', f'найдешевший з доставкою до {FAST_DAYS} днів'
    if pick is None:
        pick = cheapest or opts[0]
        rule, reason = 'cheap', 'найдешевший'
    extra = ''
    if cheapest is not None and pick is not cheapest and pick.get('home') is not None:
        diff = pick['home'] - cheapest['home']
        if diff > 0:
            from offers.pricing import home_currency
            extra = f'на {num(diff)} {home_currency()} дорожче за найдешевший ({cheapest["carrier_name"]} · ' \
                    f'{cheapest["name"]})'
    clean = lambda o: o and {k: (str(v) if isinstance(v, Decimal) else v) for k, v in o.items()}  # noqa: E731
    return {'pick': clean(pick), 'reason': reason, 'rule': rule, 'extra': extra,
            'cheapest': clean(cheapest), 'fastest': clean(fastest)}


# ── Після оформлення: друк, «Відправлено», лист клієнту, наступне замовлення ───────────────

NEXT_STATUSES = ('submitted', 'label_ready', 'in_transit')   # відправлення оформлено, посилка ще не доїхала
DASHES = str.maketrans({'–': '-', '—': '-', '‐': '-', '‑': '-'})


def order_labels(order, shipment=None) -> list:
    """Етикетки DYMO замовлення — як кнопки 🖨️ у картці замовлення (`label_buttons_detail`): етикетка замовлення
    і по одній на позицію (артикул з рядка замовлення, кількість — на етикетці). Посилка частини замовлення
    (shipment з рядками) — лише її позиції й кількості. [{kind, sku, qty, order, file, title}]"""
    from labels_app.views import get_label_path, label_kinds
    out = [{'kind': 'order', 'sku': order.order_number or str(order.pk), 'qty': 1, 'order': order.pk, 'file': False,
            'title': f'🏷️ Етикетка замовлення {order.order_number or order.pk}'}]
    here = None
    if shipment is not None and shipment.lines.exists():
        from shipping.services.parcels import contents
        here = contents(shipment)
    lines = []
    for line in order.lines.select_related('product').order_by('pk'):
        sku = ((line.sku_raw or '').strip() or (line.product.sku if line.product else '')).translate(DASHES).strip()
        qty = here.get(line.pk, 0) if here is not None else (line.qty or 1)
        if sku and qty:
            lines.append((sku, int(qty)))
    kinds = label_kinds([sku for sku, _ in lines]) if lines else {}
    for sku, qty in lines:
        if not kinds.get(sku):
            continue
        try:
            has_file = bool(get_label_path(sku))
        except OSError:
            has_file = False
        out.append({'kind': kinds[sku], 'sku': sku, 'qty': qty, 'order': order.pk, 'file': has_file,
                    'title': f'🏷️ {sku} × {qty}'})
    return out


def next_order(exclude=None):
    """(замовлення, скільки ще) — наступне до відправки: `sales.revenue.to_ship()` без оформлених відправлень
    (`SHIPPED`), спершу найближчий дедлайн відправки, потім найстаріше."""
    from django.db.models import Exists, F, OuterRef
    from sales.revenue import to_ship
    from shipping.models import Shipment
    # посилки частини замовлення (з рядками) не роблять його «вже відправленим» — залишок ще в черзі
    busy = Shipment.objects.filter(order=OuterRef('pk'), status__in=SHIPPED, lines__isnull=True)
    qs = to_ship().filter(~Exists(busy))
    if exclude:
        qs = qs.exclude(pk=exclude)
    qs = qs.order_by(F('shipping_deadline').asc(nulls_last=True), 'order_date', 'pk')
    return qs.first(), qs.count()


QUEUE_MAX = 100
FINISH_DAYS = 60
GROUPS = (('overdue', '⏰ Прострочено'), ('today', '📅 Сьогодні'), ('tomorrow', '🌅 Завтра'),
          ('week', '🗓 Цього тижня'), ('later', '⏳ Пізніше'), ('none', '📭 Без строку відправки'))


def _group(deadline, today) -> str:
    if deadline is None:
        return 'none'
    days = (deadline - today).days
    return ('overdue' if days < 0 else 'today' if days == 0 else 'tomorrow' if days == 1
            else 'week' if days <= 7 else 'later')


def queue(user) -> dict:
    """📦 Відправка сьогодні: {finish — оформлені, але замовлення ще не «Відправлено» (крок «що далі»),
    groups — [(id, назва, [рядок])] черги `next_order`, total, first}. Лише читання, без AI;
    побажання з пошти — лише збережені раніше (`ship_wishes.cached`)."""
    from datetime import timedelta as _td

    from django.db.models import Count, Exists, F, OuterRef, Prefetch
    from django.urls import reverse

    from sales.revenue import to_ship
    from shipping.models import Shipment
    from shipping.services import ship_wishes
    today = timezone.localdate()
    finish = []
    for s in (Shipment.objects.filter(status__in=NEXT_STATUSES, order__isnull=False, order__shipped_at__isnull=True,
                                      created_at__gte=timezone.now() - _td(days=FINISH_DAYS))
              .exclude(order__status__in=('shipped', 'delivered', 'cancelled'))
              .select_related('order', 'carrier').order_by('-pk')[:QUEUE_MAX]):
        finish.append({'shipment': s, 'order': s.order, 'url': reverse('admin:shipping_shipment_detail', args=[s.pk])})
    # посилки частини замовлення (з рядками) не роблять його «вже відправленим» — залишок ще в черзі
    busy = Shipment.objects.filter(order=OuterRef('pk'), status__in=SHIPPED, lines__isnull=True)
    qs = (to_ship().filter(~Exists(busy))
          .order_by(F('shipping_deadline').asc(nulls_last=True), 'order_date', 'pk')
          .prefetch_related(Prefetch('shipments', queryset=Shipment.objects.filter(status='draft').order_by('-pk'),
                                     to_attr='drafts'), 'lines__product'))
    total = qs.count()
    orders = list(qs[:QUEUE_MAX])
    mails = {}
    try:
        from crm.views import _own_messages
        mails = dict(_own_messages(user).filter(crm_events__related_order_id__in=[o.pk for o in orders])
                     .values_list('crm_events__related_order_id').annotate(n=Count('pk', distinct=True)))
    except Exception:  # noqa: BLE001 — без лічильника листів
        pass
    can_add = user.has_perm('shipping.add_shipment')
    groups = {g: [] for g, _ in GROUPS}
    for o in orders:
        found = checks(o, shipments=[])
        draft = o.drafts[0] if o.drafts else None
        wishes = ship_wishes.cached(o, user)
        groups[_group(o.shipping_deadline, today)].append({
            'order': o, 'country': country(o),
            'items': ', '.join(f'{(l.sku_raw or (l.product.sku if l.product else "")).strip()} × {num(l.qty)}'
                               for l in list(o.lines.all())[:4]) + (' …' if len(o.lines.all()) > 4 else ''),
            'blocks': [c['text'] for c in found if c['level'] == 'block'],
            'warns': [c['text'] for c in found if c['level'] == 'warn'],
            'late': max(0, (today - o.shipping_deadline).days) if o.shipping_deadline else 0,
            'draft': draft and {'pk': draft.pk, 'url': reverse('admin:shipping_shipment_change', args=[draft.pk])},
            'url': reverse('admin:shipping_shipment_create', args=[o.pk]) if can_add else '',
            'order_url': reverse('admin:sales_salesorder_change', args=[o.pk]),
            'mails': mails.get(o.pk, 0), 'wishes': (wishes or {}).get('wishes') or []})
    first = next((r for g, _ in GROUPS for r in groups[g] if r['url'] and not r['blocks']), None)
    return {'finish': finish, 'groups': [(g, title, groups[g]) for g, title in GROUPS if groups[g]],
            'total': total, 'shown': len(orders), 'first': first, 'can_add': can_add}


def mail_state(order, user) -> dict:
    """Лист клієнту про відправку («📧 Надіслати клієнту» картки замовлення): {sent, url, to} або {sent, blocked}."""
    from django.urls import reverse
    sent = order.ship_notify_sent_at
    if not user.has_perm('sales.change_salesorder'):
        return {'sent': sent, 'blocked': 'немає права змінювати замовлення'}
    try:
        from config.models import NotificationSettings
        from sales.admin import _notify_blocked
        why = _notify_blocked(NotificationSettings.get(), order, 'customer')
    except Exception:  # noqa: BLE001 — без налаштувань сповіщень листа немає
        why = 'Налаштування сповіщень недоступні'
    if why:
        return {'sent': sent, 'blocked': why}
    to = (order.ship_email or order.email or '').strip()
    if not to:
        return {'sent': sent, 'blocked': 'у замовленні немає email клієнта'}
    return {'sent': sent, 'to': to, 'url': reverse('admin:sales_salesorder_notify_customer', args=[order.pk])}


def after_booking(shipment, user) -> dict | None:
    """Що зробити після оформлення в перевізника — панель «🤖 Агент відправки: що далі» на сторінці відправлення.
    Нічого не робить сам: друк — у браузері (PDF і служба DYMO), «Відправлено» — та сама дія, що «✅ Підтвердити»
    (`ShipmentAdmin.shop_confirm_view`), лист — той самий, що в картці замовлення."""
    from django.urls import reverse
    order = shipment.order
    if not order or shipment.status not in NEXT_STATUSES:
        return None
    shipped = order.status in ('shipped', 'delivered') or bool(order.shipped_at)
    if shipment.lines.exists():
        # посилка частини замовлення: «Відправлено» — коли саме її підтвердили в магазині
        shipped = order.status in ('shipped', 'delivered') or bool(shipment.shop_confirmed_at)
    nxt, left = next_order(exclude=order.pk)
    return {
        'pdf_url': reverse('admin:shipping_shipment_print_all', args=[shipment.pk]) if shipment.label_url else '',
        'is_eu': country(order) in eu(),
        'labels': order_labels(order, shipment),
        'shipped': shipped,
        'shipped_at': order.shipped_at,
        'order_status': order.get_status_display(),
        'can_ship': user.has_perm('sales.change_salesorder'),
        'ship_url': reverse('admin:shipping_shipment_shop_confirm', args=[shipment.pk]),
        'is_digikey': order.source == 'digikey',
        'tracking': shipment.tracking_number or order.tracking_number or '',
        'mail': mail_state(order, user),
        'next': nxt and {'order': nxt, 'url': reverse('admin:shipping_shipment_create', args=[nxt.pk])},
        'left': left,
    }
