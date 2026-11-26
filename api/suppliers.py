"""🏭 Постачальники, 🪪 візитки й 📨 запити постачальникам (RFQ) через REST API — для зовнішнього агента й програм.

Ті самі відбори, права й приватність, що інструменти MCP (agent/tools_suppliers.py, tools_cards.py, tools_rfq.py) і
сторінки «📦 Склад → Постачальники», «🪪 Усі візитки», «📨 Запити постачальникам»:

    GET  /suppliers/                   — список (q, category, status, source, where, country, has_email, rating_min,
                                         limit ≤ 200, offset) — `tools_suppliers.supplier_queryset`
    GET  /suppliers/filters/           — значення фільтрів (категорії, стани, звідки, де взяли, країни)
    GET  /suppliers/{id}/              — картка: контакти, закупівлі, запити, історія (лист чужої скриньки — без тексту),
                                         аналізи (оцінка з листування й аналіз пошти — лише свої)
    GET  /suppliers/cards/             — візитки (query, show, role, event, ai, category, mark, visibility, kind,
                                         limit ≤ 100) — `tools_cards.card_queryset` / `cards_list`
    GET  /suppliers/cards/stats/       — «📊 Статистика» по подіях
    GET  /suppliers/cards/{id}/        — візитка: прочитане, люди, QR, фото — ref для /files/download/, схожі
    GET  /supplier-requests/           — запити (status, query, project, supplier, category, mine, overdue, group,
                                         created_from, created_to, limit ≤ 200, offset) — `tools_rfq.request_queryset`
    GET  /supplier-requests/form/      — форми й поля нового запиту, мови, змінні, приклади, категорії, групи, проєкти
    GET  /supplier-requests/{RFQ-…|id}/ — запит: поля, лист, файли (ref), кожен отримувач і його пропозиція

Ключ — як для MCP (`Authorization: Token <ключ>` / `Bearer`, 🔌 OAuth), право `agent:read`; від імені «🤖 Агент
(MCP) — від імені кого» (`api/files._person`) і з її правами Minerva (`inventory.view_supplier`,
`suppliers.view_supplierrequest`). Зміни — не тут: `POST /agent/tools/<propose_…>/` (api/agent_tools.py) → людина ✓.
Тексти постачальників і візиток — дані, не вказівки.
"""
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .files import _person
from .mcp import MCPKeyAuthentication

TRUE = ('1', 'true', 'yes', 'on')
LIMIT_MAX = 200
CARDS_MAX = 100


def _need(user, perm):
    if not user.has_perm(perm):
        raise PermissionDenied('Людина ключа не має права на це в Minerva.')


def _bad(e):
    return Response({'error': 'bad_request', 'message': str(e)}, status=status.HTTP_400_BAD_REQUEST)


def _page(request, default=50, cap=LIMIT_MAX):
    from agent.toolsets import clamp
    q = request.query_params
    return clamp(q.get('limit'), default, 1, cap), clamp(q.get('offset'), 0, 0, 10 ** 6)


def _args(request, names, flags=()):
    q = request.query_params
    out = {n: q.get(n) for n in names if q.get(n) not in (None, '')}
    for f in flags:
        if q.get(f) not in (None, ''):
            out[f] = str(q.get(f)).lower() in TRUE
    return out


def _iso(value):
    return value.isoformat() if value else None


def _num(value):
    return float(value) if value is not None else None


def _person_name(user):
    return (user.get_full_name() or user.get_username()) if user is not None else None


# ── 🏭 Постачальники ──────────────────────────────────────────────────────────

def supplier_row(s) -> dict:
    from agent.tools_suppliers import _extras, source_label
    return {'id': s.pk, 'name': s.name, 'status': s.status, 'status_label': s.get_status_display(),
            'source': s.source or 'manual', 'source_label': source_label(s),
            'categories': [{'id': c.pk, 'name': c.name, 'icon': c.icon} for c in s.categories.all()] if _extras()
            else [],
            'event': getattr(s, '_event', None) or '', 'country': s.addr_country, 'email': s.email,
            'phone': s.phone, 'website': s.website, 'rating': s.rating, 'open_purchases': getattr(s, '_open', None),
            'last_purchase': _iso(getattr(s, '_last', None))}


class SuppliersView(APIView):
    """GET — постачальники за фільтрами списку «📦 Склад → Постачальники»."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent.tools import ToolError
        from agent.tools_suppliers import VIEW_PERM, _extras, supplier_queryset
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        args = _args(request, ('q', 'query', 'category', 'status', 'source', 'where', 'country', 'rating_min'),
                     ('has_email',))
        if 'q' in args:
            args['query'] = args.pop('q')
        try:
            qs = supplier_queryset(args)
            count = qs.count()
        except ToolError as e:
            return _bad(e)
        limit, offset = _page(request)
        rows = qs.prefetch_related('categories') if _extras() else qs
        return Response({'count': count, 'limit': limit, 'offset': offset,
                         'results': [supplier_row(s) for s in rows[offset:offset + limit]]})


class SupplierFiltersView(APIView):
    """GET — значення фільтрів постачальників з кількостями."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent.tools_suppliers import VIEW_PERM, _extras, filter_values
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        if not _extras():
            return Response({'categories': [], 'statuses': [], 'sources': [], 'events': [], 'countries': []})
        return Response(filter_values(user))


def _purchases(s) -> dict:
    from inventory.models import PurchaseOrder
    from agent.tools_suppliers import OPEN_PO
    pos = list(PurchaseOrder.objects.filter(supplier=s).order_by('-order_date', '-pk')[:30])
    rows = [{'id': po.pk, 'code': po.code or f'PO-{po.pk}', 'status': po.status, 'order_date': _iso(po.order_date),
             'expected_date': _iso(po.expected_date), 'received_date': _iso(po.received_date),
             'total': _num(po.total_price), 'currency': po.currency, 'tracking_number': po.tracking_number}
            for po in pos]
    out = {'count': PurchaseOrder.objects.filter(supplier=s).count(),
           'open': sum(1 for r in rows if r['status'] in OPEN_PO), 'recent': rows}
    try:
        from suppliers.services.overview import purchases
        p = purchases(s)
        out.update(median_days=p['median_days'], on_time=p['on_time'], late=p['late'], cancelled=p['cancelled'],
                   spend={'total': _num(p['spend'].total), 'currency': p['spend'].home,
                          'without_rate': {k: _num(v) for k, v in p['spend'].missing.items()}})
    except Exception:  # noqa: BLE001 — без застосунку suppliers / курсу — лише список
        pass
    return out


def supplier_detail(s, user, personal) -> dict:
    from agent.tools_suppliers import RECENT, _extras
    out = {**supplier_row(s), 'address': {'street': s.addr_street, 'zip': s.addr_zip, 'city': s.addr_city,
                                          'country': s.addr_country},
           'domains': s.domains, 'vat_id': s.vat_id, 'payment_terms': s.payment_terms, 'currency': s.currency,
           'description': s.description, 'notes': s.notes, 'contact_person': s.contact_person,
           'created_at': _iso(s.created_at), 'purchases': _purchases(s),
           'url': f'/admin/inventory/supplier/{s.pk}/change/'}
    if not _extras():
        return out
    from suppliers.models import SupplierAnalysis as A, SupplierEvent, SupplierRequestRecipient as R
    out['contacts'] = [{'id': c.pk, 'name': c.name, 'position': c.position, 'email': c.email, 'phone': c.phone,
                        'mobile': c.mobile, 'notes': c.notes, 'is_primary': c.is_primary, 'is_active': c.is_active,
                        'source': c.source}
                       for c in s.contacts.order_by('-is_active', '-is_primary', 'name')[:50]]
    out['requests'] = [{'code': r.request.code, 'title': r.request.title, 'state': r.state,
                        'state_label': r.get_state_display(), 'sent_at': _iso(r.sent_at),
                        'price': _num(r.price), 'total': _num(r.total), 'currency': r.currency,
                        'lead_days': r.lead_days, 'chosen': r.chosen}
                       for r in R.objects.filter(supplier=s).exclude(state=R.PLANNED).select_related('request')
                       .order_by('-sent_at', '-pk')[:RECENT]]
    viewer = user if personal else None
    events = []
    for e in SupplierEvent.objects.filter(supplier=s).select_related('user', 'message__account')[:30]:
        text = e.text
        if e.message_id and (e.message is None or e.message.account.user_id != getattr(viewer, 'pk', None)):
            text = 'лист з іншої скриньки'
        events.append({'at': _iso(e.created_at), 'kind': e.kind, 'kind_label': e.get_kind_display(), 'text': text,
                       'user': _person_name(e.user)})
    out['events'] = events
    analyses = {}
    web = A.objects.filter(supplier=s, kind=A.WEB).first()
    if web is not None:
        analyses['web'] = {'at': _iso(web.created_at), 'result': web.result or {}, 'text': web.text}
    if viewer is not None:
        for kind, key in ((A.AI, 'ai'), (A.MAIL, 'mail')):
            own = A.objects.filter(supplier=s, kind=kind, owner=viewer).first()
            if own is not None:
                analyses[key] = {'at': _iso(own.created_at), 'result': own.result or {}, 'text': own.text}
    out['analyses'] = analyses
    return out


class SupplierDetailView(APIView):
    """GET — картка постачальника."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request, pk):
        from inventory.models import Supplier
        from agent.tools_suppliers import VIEW_PERM
        _key, user, personal = _person(request)
        _need(user, VIEW_PERM)
        s = Supplier.objects.filter(pk=pk).first()
        if s is None:
            raise NotFound('Такого постачальника немає.')
        return Response(supplier_detail(s, user, personal))


# ── 🪪 Візитки ────────────────────────────────────────────────────────────────

def card_row(m) -> dict:
    from suppliers.services import cards
    d, info = cards.card_data(m), m.info
    base = info.get('base')
    return {'id': m.pk, 'kind': m.kind, 'kind_label': m.get_kind_display(), 'role': m.role,
            'role_label': info['role_label'], 'title': m.title, 'company': info['company'], 'person': info['person'],
            'position': info['position'], 'email': d.get('contact_email') or d.get('email') or '',
            'phone': d.get('contact_mobile') or d.get('contact_phone') or d.get('phone') or '',
            'website': d.get('website') or '', 'country': (d.get('addr_country') or '').upper(),
            'categories': [{'id': c.pk, 'name': c.name} for c in info['cats']],
            'categories_guessed': info['cats_guessed'], 'event': m.event, 'marks': list(m.marks or []),
            'ai': info['ai']['key'], 'state': m.state, 'visibility': m.visibility,
            'supplier': {'id': m.supplier_id, 'name': m.supplier.name} if m.supplier_id else None,
            'customer': {'id': m.customer_id, 'name': m.customer.company or m.customer.name} if m.customer_id
            else None,
            'in_base': {'id': base['supplier'].pk, 'name': base['supplier'].name, 'why': base['why'],
                        'strong': base['strong']} if base else None,
            'added_by': info['who'], 'created_at': _iso(m.created_at)}


class CardsView(APIView):
    """GET — візитки й флаєри за фільтрами сторінки «🪪 Усі візитки» (лише ті, які людина бачить)."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent.tools import ToolError
        from agent.tools_cards import VIEW_PERM, _installed, cards_list
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        if not _installed():
            return Response({'count': 0, 'results': []})
        args = _args(request, ('q', 'query', 'show', 'role', 'event', 'ai', 'category', 'mark', 'visibility', 'kind'))
        if 'q' in args:
            args['query'] = args.pop('q')
        limit, _offset = _page(request, 50, CARDS_MAX)
        try:
            items, count = cards_list(user, args, limit)
        except ToolError as e:
            return _bad(e)
        return Response({'count': count, 'limit': limit, 'results': [card_row(m) for m in items]})


class CardStatsView(APIView):
    """GET — «📊 Статистика» візиток по подіях (де взяли)."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent.tools_cards import VIEW_PERM
        from suppliers.services import cards
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        st = cards.stats(user)

        def clean(g):
            return {'event': g['event'], 'cards': g['cards'], 'people': g['people'], 'companies': g['companies'],
                    'suppliers': g['suppliers'], 'customers': g['customers'], 'not_entered': g['new'], 'ai': g['ai'],
                    'countries': [{'name': c, 'cards': n} for c, n in g['countries']],
                    'categories': [{'name': c, 'cards': n} for c, n in g['cats']],
                    'marks': [{'name': c, 'cards': n} for c, n in g['marks']],
                    'first': _iso(g['first']), 'last': _iso(g['last'])}
        return Response({'total': clean(st['total']), 'events': [clean(g) for g in st['events']]})


class CardDetailView(APIView):
    """GET — візитка повністю (фото — ref для /files/download/?ref=)."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request, pk):
        from agent.tools import ToolError
        from agent.tools_cards import CRM_VIEW, DATA_KEYS, VIEW_PERM, file_refs, get_card
        from suppliers.models import SupplierCategory
        from suppliers.services import card_photos, cards, draft
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        try:
            m = get_card(user, pk)
        except ToolError as e:
            raise NotFound(str(e)) from None
        m.info = cards.info(m, list(SupplierCategory.objects.filter(is_active=True)))
        m.info['base'] = cards.base_match(m)
        d = cards.card_data(m)
        out = {**card_row(m), 'note': m.note, 'qr_text': m.qr_text, 'error': m.error,
               'cost_usd': _num(m.cost_usd), 'source': m.info['source'],
               'data': {k: d.get(k) for k in DATA_KEYS if d.get(k)},
               'ai_categories': d.get('categories') or [], 'new_category': m.info.get('new_category') or None,
               'people': card_photos.people(m), 'web': ((m.extracted or {}).get('web') or {}).get('summary', ''),
               'files': [{'ref': ref, 'what': what} for ref, what in file_refs(m)],
               'url': f'/admin/inventory/supplier/cards/{m.pk}/'}
        if not m.supplier_id and cards.has_data(d):
            out['similar_suppliers'] = [{'id': s.pk, 'name': s.name, 'why': why} for s, why in draft.existing(d)[:5]]
        if not m.customer_id and user.has_perm(CRM_VIEW):
            out['similar_customers'] = [{'id': c.pk, 'name': c.company or c.name, 'why': why}
                                        for c, why in cards.customer_matches(d)]
        return Response(out)


# ── 📨 Запити постачальникам ──────────────────────────────────────────────────

def request_row(req) -> dict:
    return {'id': req.pk, 'code': req.code, 'title': req.title, 'status': req.status,
            'status_label': req.get_status_display(), 'template': req.template or 'generic',
            'project': req.project.code if req.project_id else None,
            'category': {'id': req.category_id, 'name': req.category.name} if req.category_id else None,
            'group': req.group, 'quantities': req.quantities, 'language': req.language,
            'reply_by': _iso(req.reply_by), 'owner': _person_name(req.owner) if req.owner_id else None,
            'created_at': _iso(req.created_at), 'sent_at': _iso(req.sent_at), 'closed_at': _iso(req.closed_at),
            'copied_from': req.copied_from.code if req.copied_from_id and req.copied_from else None,
            'recipients': {'sent': getattr(req, '_sent', None), 'not_sent': getattr(req, '_planned', None),
                           'answered': getattr(req, '_ans', None), 'quoted': getattr(req, '_quoted', None),
                           'waiting': getattr(req, '_waiting', None)}}


class RequestsView(APIView):
    """GET — запити постачальникам за фільтрами списку «📨 Запити постачальникам»."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent.tools import ToolError
        from agent.tools_rfq import VIEW_PERM, _installed, request_queryset
        from suppliers.models import SupplierRequestRecipient as R
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        if not _installed():
            return Response({'count': 0, 'results': []})
        args = _args(request, ('q', 'query', 'status', 'project', 'supplier', 'category', 'group', 'created_from',
                               'created_to'), ('mine', 'overdue'))
        if 'q' in args:
            args['query'] = args.pop('q')
        try:
            qs = request_queryset(user, args)
            count = qs.count()
        except ToolError as e:
            return _bad(e)
        limit, offset = _page(request)
        rows = list(qs[offset:offset + limit])
        chosen = {}
        for rid, sid, name in (R.objects.filter(request__in=rows, chosen=True)
                               .values_list('request_id', 'supplier_id', 'supplier__name')):
            chosen.setdefault(rid, []).append({'id': sid, 'name': name})
        return Response({'count': count, 'limit': limit, 'offset': offset,
                         'results': [{**request_row(r), 'chosen': chosen.get(r.pk, [])} for r in rows]})


class RequestFormView(APIView):
    """GET — що є у формі «📨 Новий запит»: форми й поля, мови, змінні, приклади, категорії, групи, проєкти."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        import datetime

        from django.db.models import Count
        from django.utils import timezone
        from agent.tools_rfq import VIEW_PERM
        from projects.models import Project
        from suppliers.models import SupplierCategory, SupplierRequest
        from suppliers.services import rfq
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        counts = dict(SupplierCategory.objects.values_list('pk').annotate(n=Count('suppliers', distinct=True))
                      .values_list('pk', 'n'))
        groups = list(dict.fromkeys(SupplierRequest.objects.exclude(group='').order_by('-created_at')
                                    .values_list('group', flat=True)[:200]))[:30]
        return Response({
            'templates': [{'key': k, 'label': v[0], 'fields': [{'key': f[0], 'label': f[1], 'label_en': f[2],
                                                                'label_de': f[3], 'hint': f[4]} for f in v[1]]}
                          for k, v in rfq.TEMPLATES.items()],
            'languages': [{'key': k, 'label': v} for k, v in SupplierRequest.LANGS],
            'placeholders': [{'key': k, 'meaning': v} for k, v in rfq.PLACEHOLDERS],
            'examples': {k: {'subject': v[0], 'body': v[1]} for k, v in rfq.EXAMPLES.items()},
            'categories': [{'id': c.pk, 'name': c.name, 'icon': c.icon, 'request_template': c.request_template or '',
                            'suppliers': counts.get(c.pk, 0)}
                           for c in SupplierCategory.objects.filter(is_active=True).order_by('order', 'name')],
            'groups': groups,
            'projects': [{'code': p.code, 'name': p.name}
                         for p in Project.objects.filter(status__in=Project.OPEN_STATUSES).order_by('-pk')[:40]],
            'default_reply_by': _iso(timezone.localdate() + datetime.timedelta(days=7))})


def _tiers(r) -> list:
    return [t for t in (r.tiers or []) if isinstance(t, dict)]


def request_detail(req) -> dict:
    from suppliers.models import SupplierReplyFile
    from suppliers.services import rfq, rfq_copy
    files = [{'id': f.pk, 'name': f.name, 'size': f.size, 'ref': f'rfq:{f.pk}'} for f in req.files.all()]
    reply_files = {}
    for f in SupplierReplyFile.objects.filter(recipient__request=req).order_by('received_at', 'pk'):
        reply_files.setdefault(f.recipient_id, []).append({'id': f.pk, 'name': f.name, 'size': f.size,
                                                          'ref': f'rfqreply:{f.pk}',
                                                          'received_at': _iso(f.received_at)})
    recips = []
    for r in req.recipients.select_related('supplier', 'contact').order_by('pk'):
        recips.append({
            'supplier': {'id': r.supplier_id, 'name': r.supplier.name},
            'contact': r.contact.name if r.contact_id and r.contact else '', 'email': r.email, 'cc': r.cc,
            'state': r.state, 'state_label': r.get_state_display(), 'error': r.error,
            'sent_at': _iso(r.sent_at), 'replied_at': _iso(r.replied_at), 'reminded_at': _iso(r.reminded_at),
            'quote': {'price': _num(r.price), 'total': _num(r.total), 'currency': r.currency, 'tiers': _tiers(r),
                      'one_time': r.one_time or [], 'includes': r.includes or [], 'lead_days': r.lead_days,
                      'shipping': _num(r.shipping), 'valid_until': _iso(r.valid_until), 'note': r.quote_note},
            'decision': {'chosen': r.chosen, 'award': r.award, 'reject_reason': r.reject_reason},
            'files': reply_files.get(r.pk, [])})
    return {**request_row(req), 'specs': {k: v for k, v in (req.specs or {}).items() if not str(k).startswith('_')},
            'notes': (req.specs or {}).get('_notes', ''), 'subject': req.subject or rfq.subject_for(req),
            'body': req.body, 'cc': req.cc, 'cc_contacts': req.cc_contacts, 'decision': req.decision,
            'analysis': req.analysis or {}, 'files': files, 'recipients_list': recips,
            'to_review': [x['label'] for x in rfq_copy.pending(req)],
            'url': f'/admin/suppliers/supplierrequest/{req.pk}/work/'}


class RequestDetailView(APIView):
    """GET — запит постачальникам повністю: поля, лист, файли, кожен отримувач і його пропозиція."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request, ref):
        from agent.tools import ToolError
        from agent.tools_rfq import VIEW_PERM, get_request
        _key, user, _personal = _person(request)
        _need(user, VIEW_PERM)
        try:
            req = get_request(ref)
        except ToolError as e:
            raise NotFound(str(e)) from None
        return Response(request_detail(req))
