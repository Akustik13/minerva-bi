"""Агент і Angebote: «підготуй Angebot» → чернетка Angebot у легкій формі (offers.services.save_quick). Агент лише
пропонує клієнта, контакт, проєкт, мову й позиції (артикул складу і кількості, ступенями «35; 100»); ціни й строки
Minerva рахує сама за «Ціни з» і «Строки з» Angebot (offers/pricing.py) — агент їх не вигадує. Застосовує людина (✓):
створюється чернетка Angebot, далі — легка форма «⚡» (перевірити, згенерувати документ, надіслати).

Інструмент (agent/tools.py бере звідси): propose_offer — лише людині з `offers.add_offer`.
Застосування (agent/decide.py `apply_one`): `apply_offer`.
"""
import re

from agent.models import AgentProposal

LINES_MAX = 40
_EVIDENCE = {'type': 'string', 'description': 'Чому: цитата з листа чи даних проєкту (що просив клієнт)'}
_SOURCE = {'type': 'string', 'description': 'Звідки: тема й дата листа або проєкт'}
LANGS = ('de', 'en', 'de_en', 'uk')

PROPOSE_OFFER = {
    'name': 'propose_offer',
    'description': 'Запропонувати ЧЕРНЕТКУ Angebot (комерційної пропозиції) клієнту — людина підтвердить ✓ і '
                   'перевірить у легкій формі. Клієнт CRM (#pk, компанія або email) або контакт з листа, проєкт (код '
                   'P-…), мова документа, позиції — артикул складу й кількості (кілька кількостей ступенями «35; 100»); '
                   'артикула немає в каталозі — опис позиції без артикула. Ціни й строки НЕ вказуй: Minerva порахує '
                   'сама за своїми правилами.',
    'input_schema': {'type': 'object', 'properties': {
        'customer': {'type': 'string', 'description': 'Клієнт CRM: #pk, назва фірми або email; порожньо — лише контакт'},
        'contact_name': {'type': 'string'}, 'contact_email': {'type': 'string'},
        'project': {'type': 'string', 'description': 'Код проєкту P-РРРР-NNN (необов’язково)'},
        'language': {'type': 'string', 'description': 'de | en | de_en | uk — як у листуванні з клієнтом'},
        'lines': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'sku': {'type': 'string', 'description': 'Артикул складу; порожньо — позиція лише з описом'},
            'qty': {'type': 'string', 'description': 'Кількість або ступені «35; 100»'},
            'description': {'type': 'string'}}, 'required': ['qty']}},
        'notes': {'type': 'string', 'description': 'Що людині перевірити (необов’язково)'},
        'evidence': _EVIDENCE, 'source': _SOURCE},
        'required': ['lines', 'evidence', 'source']}}
TOOLS = [PROPOSE_OFFER]


def _error(text):
    from agent.tools import ToolError
    return ToolError(text)


def can_read(user) -> bool:
    return bool(user is not None and user.has_perm('offers.add_offer'))


def _customer(text):
    from crm.models import Customer
    text = str(text or '').strip()
    if not text:
        return None
    if re.fullmatch(r'#?\d+', text):
        c = Customer.objects.filter(pk=int(text.lstrip('#'))).first()
    elif '@' in text:
        c = Customer.objects.filter(email__iexact=text).first()
    else:
        found = list(Customer.objects.filter(company__iexact=text)[:2]) or list(Customer.objects.filter(
            name__iexact=text)[:2])
        if len(found) > 1:
            raise _error(f'клієнтів «{text}» кілька — вкажи #pk або email')
        c = found[0] if found else None
    if c is None:
        raise _error(f'клієнта «{text}» немає в CRM — вкажи contact_name / contact_email без customer')
    return c


def _lines(items) -> list:
    from inventory.services.stock import resolve_sku
    from offers.services import parse_tiers
    if not isinstance(items, list) or not items:
        raise _error('потрібні позиції (lines)')
    if len(items) > LINES_MAX:
        raise _error(f'забагато позицій (до {LINES_MAX})')
    out = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise _error(f'позиція {i}: очікується об’єкт')
        sku = str(item.get('sku') or '').strip()[:255]
        description = str(item.get('description') or '').strip()[:500]
        qtys = [q for q, _p in parse_tiers(str(item.get('qty') or ''))]
        if not qtys or any(q <= 0 for q in qtys):
            raise _error(f'позиція {i}: кількість — число більше за 0 або ступені «35; 100»')
        if sku:
            product = resolve_sku(sku)
            if product is None and not description:
                raise _error(f'артикула «{sku}» немає на складі — додай опис позиції (тоді це «виробництво / '
                             f'розробка»)')
            sku = product.sku if product is not None else sku
        elif not description:
            raise _error(f'позиція {i}: потрібен артикул або опис')
        out.append({'sku': sku, 'description': description,
                    'qtys': '; '.join(f'{q.normalize():f}' for q in qtys)})
    return out


def propose_offer(ctx, args):
    from projects.models import Project
    if not can_read(getattr(ctx.task, 'owner', None)):
        raise _error('немає права створювати Angebote')
    customer = _customer(args.get('customer'))
    name = str(args.get('contact_name') or '').strip()[:255]
    email = str(args.get('contact_email') or '').strip()[:254]
    if customer is None and not (name or email):
        raise _error('потрібен клієнт CRM або контакт (ім’я чи email)')
    project = None
    code = str(args.get('project') or '').strip()
    if code:
        project = Project.objects.filter(code__iexact=code).first()
        if project is None:
            raise _error(f'проєкту «{code}» немає')
    lang = str(args.get('language') or '').strip().lower()
    lines = _lines(args.get('lines'))
    if not str(args.get('evidence') or '').strip():
        raise _error('потрібна підстава (цитата)')
    data = {'customer': customer.pk if customer else None, 'contact_name': name, 'contact_email': email,
            'project': project.pk if project else None, 'language': lang if lang in LANGS else '',
            'lines': lines, 'notes': str(args.get('notes') or '').strip()[:1000]}
    who = (customer.company or customer.name) if customer else (name or email)
    new = f'Angebot для {who}: позицій {len(lines)}' + (f' · проєкт {project.code}' if project else '')
    AgentProposal.objects.update_or_create(
        task=ctx.task, product=None, kind=AgentProposal.OFFER, field=f'Angebot · {who}'[:200],
        status=AgentProposal.PENDING,
        defaults={'old_value': '', 'new_value': new[:2000], 'data': data, 'evidence': str(args['evidence'])[:2000],
                  'source': str(args.get('source') or '')[:500]})
    ctx.proposed += 1
    return f'Записано на перевірку: чернетка Angebot для {who} — {len(lines)} позицій.'


HANDLERS = {'propose_offer': propose_offer}


def apply_offer(p, user) -> str:
    """✓ — чернетка Angebot (ціни й строки — з Minerva); посилання на легку форму — у результаті пропозиції.
    → '' або причина, чому не вийшло (як інші apply_* агента)."""
    from crm.models import Customer
    from offers import services
    from projects.models import Project
    if not user.has_perm('offers.add_offer'):
        return 'немає права створювати Angebote'
    d = p.data or {}
    customer = Customer.objects.filter(pk=d.get('customer')).first() if d.get('customer') else None
    fields = {'customer': customer, 'contact_name': d.get('contact_name') or '',
              'contact_email': d.get('contact_email') or ''}
    if customer is not None:
        fields['contact_name'] = fields['contact_name'] or customer.name
        fields['contact_email'] = fields['contact_email'] or customer.email or ''
    if d.get('project'):
        fields['project'] = Project.objects.filter(pk=d['project']).first()
    if d.get('language') in LANGS:
        fields['language'] = d['language']
    try:
        offer = services.save_quick(fields, d.get('lines') or [], user=user)
    except services.OfferError as e:
        return str(e)
    p.result_url = f'/offers/{offer.pk}/quick/'
    return ''


def describe(p) -> tuple:
    """(що, було, стане) — рядок таблиці на сторінці завдання."""
    d = p.data or {}
    rows = '; '.join(f"{l.get('sku') or l.get('description', '')[:40]} × {l.get('qtys')}"
                     for l in (d.get('lines') or [])[:6])
    return '📝 Angebot', '', f'{p.new_value} — {rows}' + (f' · {d["notes"]}' if d.get('notes') else '')
