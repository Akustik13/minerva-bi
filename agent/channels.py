"""Агент і канали продажу: інтернет-магазини (shop/) і DigiKey (bots/) — лише читає й пропонує, застосовує людина.

Інструменти (agent/tools.py бере звідси): list_shops, get_sales_channels (позиції магазинів і лістинг DigiKey, ціни
DigiKey зараз і «за правилами Minerva» — `dk_prices.plan` без мережі), propose_shop (показати / сховати / ціна нетто;
товару немає в магазині — додати, `shop.services.add_products`), propose_dk_prices (ЛИШЕ за правилами Minerva — той
самий розрахунок, що «💲 Ціни DigiKey ← Minerva», з його запобіжником маржі; власних цін агент не вигадує),
propose_dk_lead (строк відвантаження, днів).
Застосування (agent/decide.py `apply_one`): магазин — `set_visibility` / `set_price` (право shop.change_shoplisting;
значення змінилось після пропозиції — ні); DigiKey ціни — свіжий `row_for`, нові ціни мають збігтися з показаними, далі
`dk_prices.create_run` + `start` (надсилання, перевірка, сповіщення — як зі сторінки порівняння; право
bots.change_digikeylisting); строк — `dk_lead.set_days` + `start_push` (є офер).
"""
import json
from decimal import Decimal, InvalidOperation

from agent.models import AgentProposal

_SKU = {'type': 'string', 'description': 'Артикул товару (SKU) зі складу Minerva'}
_EVIDENCE = {'type': 'string', 'description': 'Чому: цитата з завдання, листа чи даних'}

LIST_SHOPS = {'name': 'list_shops', 'description': 'Інтернет-магазини Minerva: код, назва, валюта, чи увімкнено.',
              'input_schema': {'type': 'object', 'properties': {}}}
CHANNELS = {'name': 'get_sales_channels', 'description': 'Де продається товар: позиції в магазинах (показано, ціна, '
            'ступені, акція) і лістинг DigiKey (стан, ціни на DigiKey зараз, ціни за правилами Minerva, залишок, строк).',
            'input_schema': {'type': 'object', 'properties': {'sku': _SKU}, 'required': ['sku']}}
PROPOSE_SHOP = {'name': 'propose_shop', 'description': 'Запропонувати зміну в інтернет-магазині (людина підтвердить): '
                'visible — показати / сховати (товару немає в магазині й visible=true — додати), price — ціна нетто за '
                '1 шт у валюті магазину. Хоча б одне з двох.',
                'input_schema': {'type': 'object', 'properties': {
                    'sku': _SKU, 'shop': {'type': 'string', 'description': 'Код магазину (list_shops)'},
                    'visible': {'type': 'boolean'}, 'price': {'type': 'number', 'exclusiveMinimum': 0},
                    'evidence': _EVIDENCE}, 'required': ['sku', 'shop', 'evidence']}}
PROPOSE_DK_PRICES = {'name': 'propose_dk_prices', 'description': 'Запропонувати оновити ціни на DigiKey ЗА ПРАВИЛАМИ '
                     'Minerva (базові ціни × курс × надбавка, округлення, не нижче закупівлі) — як у '
                     'get_sales_channels → digikey.by_rules. Інших цін не буває. Людина підтвердить, тоді Minerva '
                     'надішле й перевірить.',
                     'input_schema': {'type': 'object', 'properties': {'sku': _SKU, 'evidence': _EVIDENCE},
                                      'required': ['sku', 'evidence']}}
PROPOSE_DK_LEAD = {'name': 'propose_dk_lead', 'description': 'Запропонувати строк відвантаження на DigiKey, днів '
                   '(людина підтвердить).',
                   'input_schema': {'type': 'object', 'properties': {
                       'sku': _SKU, 'days': {'type': 'integer', 'minimum': 0, 'maximum': 365}, 'evidence': _EVIDENCE},
                       'required': ['sku', 'days', 'evidence']}}
TOOLS = [LIST_SHOPS, CHANNELS, PROPOSE_SHOP, PROPOSE_DK_PRICES, PROPOSE_DK_LEAD]


def _tiers(rows) -> list:
    return [[int(q), format(Decimal(p).normalize(), 'f')] for q, p in rows or []]


def _money(value) -> str:
    return '' if value is None else format(Decimal(value).normalize(), 'f')


def _listing(product):
    from bots.models import DigiKeyListing
    return DigiKeyListing.objects.filter(product=product).first()


def dk_row(listing):
    """Рядок «як стане за правилами Minerva» — без мережі, з останніх прочитаних цін DigiKey."""
    from bots.services import dk_prices
    return dk_prices.plan([listing])[0]


def _shop(code):
    from shop.models import Shop
    code = str(code or '').strip()
    shop = Shop.objects.filter(slug__iexact=code).first() or Shop.objects.filter(name__iexact=code).first()
    if shop is None:
        from agent.tools import ToolError
        raise ToolError(f'магазину «{code}» немає — див. list_shops')
    return shop


# ── Читання ──────────────────────────────────────────────────────────────────

def list_shops(ctx, args):
    from shop.models import Shop
    rows = [{'shop': s.slug, 'name': s.name, 'currency': s.currency, 'active': s.is_active}
            for s in Shop.objects.order_by('name')]
    return json.dumps(rows, ensure_ascii=False) if rows else 'Магазинів немає'


def sales_channels(ctx, args):
    from shop.models import Shop, ShopListing
    from shop.services import offer_info, price_breaks
    product = ctx.product(args.get('sku'))
    listings = {l.shop_id: l for l in ShopListing.objects.filter(product=product).select_related('shop')}
    shops = []
    for shop in Shop.objects.order_by('name'):
        l = listings.get(shop.pk)
        row = {'shop': shop.slug, 'name': shop.name, 'currency': shop.currency, 'listed': l is not None}
        if l is not None:
            row.update({'visible': l.is_visible, 'price': _money(l.price), 'price_source': l.price_source,
                        'tiers': [{'qty': t.get('min_qty', t.get('qty')), 'price': str(t.get('unit_price', t.get('price')))}
                                  for t in price_breaks(l)][:9],
                        'offer': offer_info(l)})
        shops.append(row)
    dk = {'listed': False}
    listing = _listing(product)
    if listing is not None:
        r = dk_row(listing)
        dk = {'listed': True, 'status': listing.sync_status, 'offer_id': listing.dk_offer_id,
              'prices_now': _tiers(r.dk), 'quantity': listing.dk_quantity_available,
              'lead_time_days': listing.dk_lead_time_days,
              'prices_checked': listing.price_checked_at.isoformat() if listing.price_checked_at else '',
              'by_rules': {'state': r.state, 'note': r.state_text or '; '.join(r.notes),
                           'prices': _tiers(r.new) if r.new else [], 'differs': r.differs,
                           'change_pct': _money(r.change_pct), 'max_change_pct': _money(r.max_change_pct)}}
    return json.dumps({'sku': product.sku, 'shops': shops, 'digikey': dk}, ensure_ascii=False, default=str)


# ── Пропозиції ───────────────────────────────────────────────────────────────

def _save(ctx, kind, product, field, old, new, data, evidence):
    from agent.tools import ToolError
    if not str(evidence or '').strip():
        raise ToolError('потрібна підстава')
    AgentProposal.objects.update_or_create(
        task=ctx.task, product=product, kind=kind, field=field, status=AgentProposal.PENDING,
        defaults={'old_value': old, 'new_value': new, 'data': data, 'evidence': str(evidence)[:2000]})
    ctx.proposed += 1


def propose_shop(ctx, args):
    from agent.tools import ToolError
    from shop.models import ShopListing
    product = ctx.product(args.get('sku'))
    shop = _shop(args.get('shop'))
    visible = args.get('visible')
    price = args.get('price')
    if visible is None and price is None:
        raise ToolError('що змінити: visible чи price')
    if price is not None:
        try:
            price = Decimal(str(price))
        except (InvalidOperation, ValueError):
            raise ToolError('ціна — число') from None
        if not (Decimal('0') < price < Decimal('1000000')):
            raise ToolError('ціна має бути більша за 0')
    listing = ShopListing.objects.filter(shop=shop, product=product).first()
    if listing is None and not visible:
        raise ToolError(f'товару {product.sku} немає в магазині {shop.slug} — сховувати нічого')
    old = {'listed': listing is not None, 'visible': listing.is_visible if listing else None,
           'price': _money(listing.price) if listing else ''}
    data = {'shop': shop.slug, 'visible': None if visible is None else bool(visible),
            'price': _money(price) if price is not None else None, 'old': old}
    if listing is not None and (data['visible'] is None or data['visible'] == listing.is_visible) and \
            (data['price'] is None or data['price'] == old['price']):
        return f'У магазині {shop.slug} уже так — пропозиція не потрібна'
    _save(ctx, AgentProposal.SHOP, product, shop.slug, json.dumps(old, ensure_ascii=False),
          json.dumps({k: data[k] for k in ('visible', 'price')}, ensure_ascii=False), data, args.get('evidence'))
    return f'Записано на перевірку: {product.sku} у магазині {shop.slug}'


def propose_dk_prices(ctx, args):
    from agent.tools import ToolError
    product = ctx.product(args.get('sku'))
    listing = _listing(product)
    if listing is None:
        raise ToolError(f'у {product.sku} немає лістингу DigiKey')
    r = dk_row(listing)
    if r.state != 'ok':
        raise ToolError(f'ціни DigiKey за правилами не розраховуються: {r.state_text or r.state}')
    if not r.differs:
        return f'Ціни {product.sku} на DigiKey уже як за правилами Minerva'
    data = {'listing': listing.pk, 'now': _tiers(r.dk), 'new': _tiers(r.new), 'change_pct': _money(r.change_pct),
            'max_change_pct': _money(r.max_change_pct), 'currency': _dk_currency(), 'notes': r.notes[:5]}
    _save(ctx, AgentProposal.DK_PRICES, product, 'dk_prices', _fmt(data['now'], data['currency']),
          _fmt(data['new'], data['currency']), data, args.get('evidence'))
    return f'Записано на перевірку: нові ціни DigiKey для {product.sku} (зміна {data["change_pct"] or "—"} %)'


def propose_dk_lead(ctx, args):
    from agent.tools import ToolError
    product = ctx.product(args.get('sku'))
    listing = _listing(product)
    if listing is None:
        raise ToolError(f'у {product.sku} немає лістингу DigiKey')
    try:
        days = int(args.get('days'))
    except (TypeError, ValueError):
        raise ToolError('строк — ціле число днів') from None
    if not (0 <= days <= 365):
        raise ToolError('строк — від 0 до 365 днів')
    if days == listing.dk_lead_time_days:
        return f'Строк {product.sku} на DigiKey уже {days} дн.'
    _save(ctx, AgentProposal.DK_LEAD, product, 'dk_lead', str(listing.dk_lead_time_days), str(days),
          {'listing': listing.pk, 'days': days, 'old': listing.dk_lead_time_days}, args.get('evidence'))
    return f'Записано на перевірку: строк {product.sku} на DigiKey {listing.dk_lead_time_days} → {days} дн.'


def _dk_currency() -> str:
    from bots.services import dk_prices
    return dk_prices.currency()


def _fmt(tiers, cur) -> str:
    return ' · '.join(f'{q} шт: {p} {cur}' for q, p in tiers) or '—'


HANDLERS = {'list_shops': list_shops, 'get_sales_channels': sales_channels, 'propose_shop': propose_shop,
            'propose_dk_prices': propose_dk_prices, 'propose_dk_lead': propose_dk_lead}


# ── Застосування (✓ людини) ──────────────────────────────────────────────────

def apply_shop(p, user) -> str:
    from shop import services
    from shop.models import ShopListing
    d = p.data or {}
    if not user.has_perm('shop.change_shoplisting'):
        return 'немає права змінювати асортимент магазину'
    try:
        shop = _shop(d.get('shop'))
    except Exception as e:  # noqa: BLE001
        return str(e)
    listing = ShopListing.objects.filter(shop=shop, product_id=p.product_id).first()
    old = d.get('old') or {}
    now = {'listed': listing is not None, 'visible': listing.is_visible if listing else None,
           'price': _money(listing.price) if listing else ''}
    if now != old:
        return 'у магазині змінилось після пропозиції — запустіть завдання ще раз'
    if listing is None:
        if not user.has_perm('shop.add_shoplisting'):
            return 'немає права додавати товари в магазин'
        services.add_products(shop, [p.product], visible=True)
        listing = ShopListing.objects.get(shop=shop, product_id=p.product_id)
    elif d.get('visible') is not None:
        services.set_visibility([listing], bool(d['visible']))
    if d.get('price') is not None:
        services.set_price([listing], Decimal(d['price']))
    return ''


def apply_dk_prices(p, user) -> str:
    from bots.models import DigiKeyListing
    from bots.services import dk_prices
    if not user.has_perm('bots.change_digikeylisting'):
        return 'немає права змінювати ціни на DigiKey'
    d = p.data or {}
    listing = DigiKeyListing.objects.filter(pk=d.get('listing')).select_related('product').first()
    if listing is None:
        return 'лістингу DigiKey вже немає'
    row = dk_row(listing)
    if not row.differs:
        return 'ціни на DigiKey уже такі'
    if _tiers(row.new) != d.get('new') or _tiers(row.dk) != d.get('now'):
        return 'ціни змінились після пропозиції (на DigiKey чи в Minerva) — запустіть завдання ще раз'
    try:
        run = dk_prices.create_run([row], user=user, kind='manual')
    except dk_prices.PriceError as e:
        return str(e)
    dk_prices.start(run)
    from django.urls import reverse
    p.result_url = reverse('admin:bots_digikeypricerun_change', args=[run.pk])
    return ''


def apply_dk_lead(p, user) -> str:
    from bots.models import DigiKeyListing
    from bots.services import dk_lead
    if not user.has_perm('bots.change_digikeylisting'):
        return 'немає права змінювати лістинги DigiKey'
    d = p.data or {}
    listing = DigiKeyListing.objects.filter(pk=d.get('listing')).first()
    if listing is None:
        return 'лістингу DigiKey вже немає'
    if listing.dk_lead_time_days != d.get('old'):
        return f'строк змінився після пропозиції (зараз {listing.dk_lead_time_days} дн.)'
    dk_lead.set_days([listing.pk], int(d['days']))
    if listing.dk_offer_id and not dk_lead.start_push([listing.pk], user=getattr(user, 'username', '')):
        return 'строк записано в Minerva, але DigiKey зараз оновлюється іншим запуском — надішліть зі сторінки «⏱ Строки»'
    return ''


def describe(p) -> tuple:
    d = p.data or {}
    if p.kind == AgentProposal.SHOP:
        old = d.get('old') or {}
        was = ('немає в магазині' if not old.get('listed') else
               ('показано' if old.get('visible') else 'сховано') + (f' · {old["price"]}' if old.get('price') else ''))
        parts = []
        if not old.get('listed'):
            parts.append('додати й показати')
        elif d.get('visible') is not None:
            parts.append('показати' if d['visible'] else 'сховати')
        if d.get('price') is not None:
            parts.append(f'ціна {d["price"]}')
        return f'🏪 Магазин {d.get("shop", "")}', was, ' · '.join(parts)
    if p.kind == AgentProposal.DK_PRICES:
        pct = f' ({d["change_pct"]} %)' if d.get('change_pct') else ''
        return '💲 Ціни на DigiKey за правилами', p.old_value, p.new_value + pct
    return '⏱ Строк на DigiKey, днів', p.old_value, p.new_value
