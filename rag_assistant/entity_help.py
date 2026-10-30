"""
«🧭 Огляд» і «❓ Запитати» в картках проєкту, клієнта, замовлення й товару (модель EntityHelp).

Як «📜 Історія» в пошті (email_assistant/reply_brief.py), але для картки:
1. Збір БЕЗ AI — кожне джерело отримує номер [#N] і групу походження:
   - minerva — дані картки з бази Minerva:
       project  — шапка, Angebote, замовлення, терміни з листів, збережений AI-огляд (projects.overview);
       customer — картка CRM, замовлення з позиціями, Angebote, проєкти, нотатки й хронологія CRM;
       order    — замовлення з позиціями й складом, відправлення, документи в папці замовлення;
       product  — товар, параметри, склад по локаціях, очікується (закупівлі), продажі, Angebote;
   - mail — листи з ВЛАСНИХ скриньок користувача: проєкту / клієнта / з номером замовлення чи трек-номером /
     з артикулом (останній лист кожної гілки, без процитованої історії);
   - document — файли й посилання, прив'язані до картки (knowledge_items: «📥 Додати», етап 3 — автоматичні),
     текст Minerva бере сама (без RAG);
   - rag — база знань (reply_brief.rag_sources: без дублів, фрагменти одного листа — одне джерело):
     клієнт — лише його листування; проєкт — листування клієнта поза гілками проєкту (без клієнта — уся пошта);
     замовлення й товар — уся пошта користувача.
   Запит до RAG — те саме правило, що в пошті: стандартна частина картки (entity_query) + що хоче менеджер
   (запитання — першим, тоді стандартна частина коротша); нічого не вказали — лише стандартна.
2. AI (ai_helper.BRIEF_MODEL): answer — відповідь на запитання з [#N]; review — «що обіцяли / зробили / відкрите,
   наступна дія», формат і перевірка — projects.overview.validate. Для проєкту огляд — свій (projects.overview.generate,
   теж з RAG), тут лише «❓ Запитати».
Усе у фоні, браузер опитує /rag/api/entity/help/<id>/. Запис бачить лише його власник (RAG і листи — лише його скриньки).
Тексти листів і документів — дані, а не інструкції.
"""
import hashlib
import json
import logging
import re

from django.apps import apps
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("rag_assistant")

KINDS = {   # вид → (app_label, model) для прав view_<model> і пошуку об'єкта
    "project": ("projects", "project"),
    "customer": ("crm", "customer"),
    "order": ("sales", "salesorder"),
    "product": ("inventory", "product"),
}
KIND_LABELS = {"project": "проєкту", "customer": "клієнта", "order": "замовлення", "product": "товару"}
ORDERS, ORDER_LINES, OFFERS, OFFER_LINES, PROJECTS = 10, 15, 6, 10, 6
NOTES, TIMELINE, SALES, PURCHASES, DOCUMENTS = 10, 25, 15, 8, 15
MAIL_LETTERS, MAIL_CHARS, MAIL_SCAN = 8, 1500, 80
QUERY_STANDARD_WITH_WISH = 800

STAGES = [
    ("minerva", "📋 Дані Minerva"),
    ("documents", "📎 Документи й посилання картки"),
    ("mail", "✉️ Листи у ваших скриньках"),
    ("rag", "🔎 RAG: база знань"),
    ("answer", "❓ Відповідь на запитання (AI)"),
    ("review", "🧭 Огляд: що обіцяли / зробили / відкрите (AI)"),
]


# ── Об'єкт і доступ ──────────────────────────────────────────────────────────

def model_of(kind):
    app_label, model = KINDS[kind]
    return apps.get_model(app_label, model)


def can_view(user, kind) -> bool:
    if kind not in KINDS or not (user and user.is_authenticated and user.is_active and user.is_staff):
        return False
    app_label, model = KINDS[kind]
    return user.has_perm(f"{app_label}.view_{model}")


def get_object(kind, pk):
    return model_of(kind).objects.filter(pk=pk).first() if kind in KINDS else None


def title(kind, obj) -> str:
    if kind == "project":
        return f"Проєкт {obj.code} · {obj.name}"
    if kind == "customer":
        return f"Клієнт {obj.company or obj.name}" + (f" ({obj.name})" if obj.company and obj.name else "")
    if kind == "order":
        return f"Замовлення {obj.source}:{obj.order_number}" + (f" · {obj.client}" if obj.client else "")
    return f"Товар {obj.sku}" + (f" · {obj.name}" if obj.name else "")


def _fmt(value) -> str:
    if not value:
        return "?"
    if hasattr(value, "hour"):
        return timezone.localtime(value).strftime("%d.%m.%Y") if timezone.is_aware(value) else value.strftime("%d.%m.%Y")
    return value.strftime("%d.%m.%Y")


def _num(value) -> str:
    from email_assistant.reply_brief import _num as num
    return num(value)


# ── Запит до RAG ─────────────────────────────────────────────────────────────

def _offer_skus(offers) -> list:
    out = []
    for o in offers:
        for line in o.lines.all()[:OFFER_LINES]:
            sku = line.sku or (line.product.sku if line.product_id else "")
            if sku and sku not in out:
                out.append(sku)
    return out


def entity_query(kind, obj) -> str:
    """Стандартна частина запиту до RAG з картки: номери, назви, артикули — те, що є в листах про неї."""
    if kind == "project":
        offers = list(obj.offers.prefetch_related("lines__product")[:OFFERS]) if hasattr(obj, "offers") else []
        parts = [obj.code, obj.name, obj.customer.company or obj.customer.name if obj.customer_id else "",
                 " ".join((obj.description or "").split())[:600], " ".join(_offer_skus(offers)[:10])]
    elif kind == "customer":
        parts = [obj.company, obj.name, obj.email, "замовлення, ціни, строки поставки, проблеми, обіцянки"]
    elif kind == "order":
        skus = [l.product.sku if l.product_id else l.sku_raw for l in obj.lines.select_related("product")[:10]]
        tracks = [obj.tracking_number] + [s.tracking_number for s in obj.shipments.all()[:3]]
        parts = [obj.order_number, " ".join(t for t in tracks if t), obj.client, obj.contact_name,
                 " ".join(s for s in skus if s)]
    else:
        parts = [obj.sku, obj.sku_short, obj.name, obj.manufacturer]
    return " · ".join(" ".join(str(p).split()) for p in parts if p and str(p).strip())


def build_query(kind, obj, question="") -> str:
    """Правило, як у пошті (retrieval.build_query): що хоче менеджер — першим, стандартна частина — коротша."""
    from rag_assistant.retrieval import QUERY_MAX
    standard = entity_query(kind, obj)
    wish = " ".join(str(question or "").split())
    if not wish:
        return standard[:QUERY_MAX]
    return f"{wish[:QUERY_MAX // 2]} · {standard[:QUERY_STANDARD_WITH_WISH]}"[:QUERY_MAX]


# ── Дані Minerva ─────────────────────────────────────────────────────────────

def _order_text(order, lines=True) -> str:
    head = [f"Замовлення {order.source}:{order.order_number} — {order.get_status_display()}",
            f"від {_fmt(order.order_date)}" if order.order_date else "",
            f"клієнт {order.client}" if order.client else "",
            f"сума {_num(order.total_price)} {order.currency}" if order.total_price else "",
            f"відправити до {_fmt(order.shipping_deadline)}" if order.shipping_deadline else "",
            f"відправлено {_fmt(order.shipped_at)}" if order.shipped_at else "",
            f"доставлено {_fmt(order.delivered_at)}" if order.delivered_at else "",
            f"трек {order.tracking_number}" if order.tracking_number else "",
            f"нотатка: {order.internal_note}" if order.internal_note else ""]
    text = ", ".join(h for h in head if h)
    if lines:
        rows = [f"  · {l.product.sku if l.product_id else l.sku_raw} × {_num(l.qty)}"
                + (f" = {_num(l.total_price)} {l.currency or ''}".rstrip() if l.total_price else "")
                for l in order.lines.all()[:ORDER_LINES]]
        text += ("\n" + "\n".join(rows)) if rows else ""
    return text


def _offer_text(offer) -> str:
    from projects.overview import _offer_line
    return _offer_line(offer)


def _stock_rows(products) -> dict:
    from inventory.models import Product
    from inventory.services.stock import annotate_stock
    ids = [p.pk for p in products if p is not None]
    return {p.pk: p for p in annotate_stock(Product.objects.filter(pk__in=ids))} if ids else {}


def _stock_line(p) -> str:
    return (f"на складі {_num(p._on_hand)}, резерв {_num(p._reserved)}, вільно {_num(p._available)}"
            + (f", очікується {_num(p._incoming)}" if p._incoming else ""))


def _customer_orders(customer):
    from sales.models import SalesOrder
    cond = Q(customer_key=customer.external_key) if customer.external_key else Q(pk__in=[])
    if customer.email:
        cond |= Q(email__iexact=customer.email)
    return SalesOrder.objects.filter(cond)


def _minerva_project(src, project) -> list:
    from projects import overview
    facts = overview.collect(project)
    p = facts["project"]
    head = [f"ПРОЄКТ {p.code} · {p.name} · {p.get_status_display()}",
            f"Клієнт: {p.customer.company or p.customer.name}" if p.customer_id else "",
            f"Відповідальний: {p.owner.get_full_name() or p.owner.username}" if p.owner_id else "",
            f"Строк проєкту: {_fmt(p.due_date)}" if p.due_date else "",
            f"Наступна дія (вручну): {p.next_action}" if p.next_action else "",
            f"Опис / вимоги: {p.description[:2000]}" if p.description else "",
            "Потребує уваги: " + "; ".join(a["text"] for a in facts["attention"][:8]) if facts["attention"] else ""]
    src.add("minerva", f"Проєкт {p.code}: стан", "\n".join(h for h in head if h),
            f"/admin/projects/project/{p.pk}/change/")
    for o in facts["offers"][:OFFERS]:
        src.add("offer", f"Angebot {o.display_number} · {o.get_status_display()}", _offer_text(o),
                f"/admin/offers/offer/{o.pk}/change/")
    for order in facts["orders"][:ORDERS]:
        src.add("minerva", f"Замовлення {order.source}:{order.order_number}", _order_text(order),
                f"/admin/sales/salesorder/{order.pk}/change/")
    if facts["deadlines"]:
        src.add("minerva", "Терміни, знайдені в листах проєкту", "\n".join(
            f"{_fmt(e.start_at)} — {e.title}{' (виконано)' if e.is_done else ''}" for e in facts["deadlines"][:20]))
    saved, stale = overview.saved(project, facts)
    if saved:
        src.add("overview", f"AI-огляд проєкту від {_fmt(saved.created_at)}" + (" (застарів)" if stale else ""),
                overview.facts_text(project, saved, stale)[:3000], f"/admin/projects/project/{project.pk}/change/#overview")
    return [f"Angebote {len(facts['offers'])}", f"замовлень {len(facts['orders'])}",
            f"листів проєкту {len(facts['messages'])}"]


def _minerva_customer(src, customer) -> list:
    head = [f"КЛІЄНТ: {customer.company or '—'} · {customer.name}", f"email {customer.email}" if customer.email else "",
            f"телефон {customer.phone}" if customer.phone else "", f"країна {customer.country}" if customer.country else "",
            f"сегмент {customer.get_segment_display()}" if customer.segment else "",
            f"статус {customer.get_status_display()}" if customer.status else "",
            f"джерело {customer.source}" if customer.source else "",
            f"примітки: {customer.notes[:1500]}" if customer.notes else ""]
    src.add("minerva", "Картка клієнта CRM", "\n".join(h for h in head if h), f"/admin/crm/customer/{customer.pk}/change/")
    orders = list(_customer_orders(customer).prefetch_related("lines__product").order_by("-order_date", "-pk")[:ORDERS])
    for order in orders:
        src.add("minerva", f"Замовлення {order.source}:{order.order_number} від {_fmt(order.order_date)}",
                _order_text(order), f"/admin/sales/salesorder/{order.pk}/change/")
    detail = [f"замовлень {len(orders)}"]
    try:
        from offers.models import Offer
        offers = list(Offer.objects.filter(customer=customer).prefetch_related("lines__product")
                      .order_by("-created_at")[:OFFERS])
    except ImportError:
        offers = []
    for o in offers:
        src.add("offer", f"Angebot {o.display_number} · {o.get_status_display()}", _offer_text(o),
                f"/admin/offers/offer/{o.pk}/change/")
    if offers:
        detail.append(f"Angebote {len(offers)}")
    try:
        from projects.models import Project
        projects = list(Project.objects.filter(customer=customer).order_by("-updated_at")[:PROJECTS])
    except ImportError:
        projects = []
    if projects:
        src.add("minerva", "Проєкти клієнта", "\n".join(
            f"{p.code} · {p.name} · {p.get_status_display()}" + (f" · наступна дія: {p.next_action}" if p.next_action else "")
            for p in projects))
        detail.append(f"проєктів {len(projects)}")
    notes = list(customer.notes_crm.order_by("-created_at")[:NOTES])
    events = list(customer.timeline_events.order_by("-created_at")[:TIMELINE])
    if notes or events:
        lines = [f"{_fmt(n.created_at)} 📝 {n.subject}: {' '.join((n.body or '').split())[:300]}" for n in notes]
        lines += [f"{_fmt(e.created_at)} {e.get_event_type_display()}: {e.title}"
                  + (f" — {' '.join((e.ai_summary or e.body or '').split())[:200]}" if (e.ai_summary or e.body) else "")
                  for e in events]
        src.add("minerva", "Нотатки й хронологія CRM", "\n".join(lines))
        detail.append(f"подій CRM {len(notes) + len(events)}")
    return detail


def _minerva_order(src, order) -> list:
    from inventory.services.stock import annotate_line_held, line_held
    lines = list(annotate_line_held(order.lines.select_related("product", "order"))[:ORDER_LINES])
    stock = _stock_rows([l.product for l in lines if l.product_id])
    rows = []
    for l in lines:
        sku = l.product.sku if l.product_id else l.sku_raw
        row = f"{sku} × {_num(l.qty)}" + (f" = {_num(l.total_price)} {l.currency or ''}".rstrip() if l.total_price else "")
        p = stock.get(l.product_id)
        if p is not None:
            # «доступно» вже зменшено на те, що тримає це замовлення (списано / резерв / бронь до руху складу,
            # inventory/services/stock.line_held), — порівнюємо з тим, що було б вільно без нього
            done = line_held(l)
            short = max(l.qty - (p._available + done), 0) if l.qty else 0
            row += (f" · {_stock_line(p)}" + (f" · під це замовлення вже враховано {_num(done)}" if done else "")
                    + (f" → бракує {_num(short)}" if short else " → вистачає"))
        rows.append(row)
    address = ", ".join(x for x in [order.addr_street, order.addr_zip, order.addr_city, order.addr_country] if x)
    contact = " ".join(x for x in [order.contact_name, order.email, order.phone] if x)
    head = _order_text(order, lines=False) + (f"\nконтакт {contact}" if contact else "") \
        + (f"\nадреса {address}" if address else "")
    src.add("minerva", f"Замовлення {order.source}:{order.order_number}: стан і позиції зі складом",
            head + ("\nПОЗИЦІЇ:\n" + "\n".join(rows) if rows else ""), f"/admin/sales/salesorder/{order.pk}/change/")
    shipments = list(order.shipments.select_related("carrier").order_by("-pk")[:5])
    if shipments:
        src.add("minerva", "Відправлення", "\n".join(
            f"{s.carrier.name if s.carrier_id else '?'} · {s.tracking_number or 'без трек-номера'} · {s.get_status_display()}"
            for s in shipments))
    try:
        from email_assistant.attachments import order_documents
        docs = order_documents(order)[:DOCUMENTS]
    except Exception:  # noqa: BLE001 — папки може не бути
        docs = []
    if docs:
        src.add("minerva", "Документи в папці замовлення", "\n".join(d["name"] for d in docs))
    customer = _order_customer(order)
    if customer is not None:
        others = list(_customer_orders(customer).exclude(pk=order.pk).order_by("-order_date", "-pk")[:5])
        if others:
            src.add("minerva", f"Інші замовлення клієнта {customer.company or customer.name}",
                    "\n".join(_order_text(o, lines=False) for o in others), f"/admin/crm/customer/{customer.pk}/change/")
    return [f"позицій {len(lines)}", f"відправлень {len(shipments)}", f"документів {len(docs)}"]


def _order_customer(order):
    from crm.models import Customer
    if order.customer_key:
        c = Customer.objects.filter(external_key=order.customer_key).first()
        if c:
            return c
    return Customer.objects.filter(email__iexact=order.email).first() if order.email else None


def _minerva_product(src, product) -> list:
    from api.serializers import split_attributes
    from inventory.services.stock import location_breakdown
    p = _stock_rows([product]).get(product.pk, product)
    params, _codes = split_attributes(product.tech_attributes or {})
    head = [f"ТОВАР {product.sku}" + (f" ({product.sku_short})" if product.sku_short else "") + f" · {product.name}",
            f"виробник {product.manufacturer}" if product.manufacturer else "",
            f"категорія {product.category}" if product.category and product.category != "other" else "",
            f"ціна закупівлі {_num(product.purchase_price)}" if product.purchase_price else "",
            f"ціна продажу {_num(product.sale_price)}" if product.sale_price else "",
            f"точка дозамовлення {_num(product.reorder_point)}" if product.reorder_point else "",
            f"строк поставки {product.lead_time_days} дн." if product.lead_time_days else "",
            f"СКЛАД: {_stock_line(p)}" if hasattr(p, "_on_hand") else "",
            ("ПАРАМЕТРИ: " + "; ".join(f"{k}: {v}" for k, v in list(params.items())[:25])) if params else "",
            f"нотатки: {product.notes[:800]}" if product.notes else ""]
    src.add("minerva", f"Товар {product.sku}: картка і склад", "\n".join(h for h in head if h),
            f"/admin/inventory/product/{product.pk}/change/")
    locations = [r for r in location_breakdown(product) if r["on_hand"] or r["reserved"]]
    if locations:
        src.add("minerva", "Склад по локаціях", "\n".join(
            f"{r['location'] or '—'}: на складі {_num(r['on_hand'])}, резерв {_num(r['reserved'])}" for r in locations))
    from inventory.models import PurchaseOrderLine
    from inventory.services.stock import OPEN_PO_STATUSES
    pos = list(PurchaseOrderLine.objects.filter(product=product, purchase_order__status__in=OPEN_PO_STATUSES)
               .select_related("purchase_order__supplier").order_by("-purchase_order__order_date")[:PURCHASES])
    if pos:
        src.add("minerva", "Очікується (відкриті закупівлі)", "\n".join(
            f"{l.purchase_order.code or l.purchase_order.pk} · {l.purchase_order.supplier} · замовлено {_num(l.qty_ordered)}, "
            f"отримано {_num(l.qty_received)}" + (f" · очікується {_fmt(l.purchase_order.expected_date)}"
                                                  if l.purchase_order.expected_date else "") for l in pos))
    from sales.models import SalesOrderLine
    sales = list(SalesOrderLine.objects.filter(product=product).select_related("order")
                 .order_by("-order__order_date", "-pk")[:SALES])
    if sales:
        src.add("minerva", "Останні продажі", "\n".join(
            f"{_fmt(l.order.order_date)} · {l.order.source}:{l.order.order_number} · {l.order.client or l.order.email} · "
            f"× {_num(l.qty)} · {l.order.get_status_display()}" for l in sales))
    offer_lines = []
    try:
        from offers.models import OfferLine
        offer_lines = list(OfferLine.objects.filter(Q(product=product) | Q(sku__iexact=product.sku))
                           .select_related("offer__customer").order_by("-offer__created_at")[:OFFERS * 2])
    except ImportError:
        pass
    if offer_lines:
        src.add("offer", "Angebote з цим товаром", "\n".join(
            f"{l.offer.display_number} · {l.offer.get_status_display()} · "
            f"{(l.offer.customer.company or l.offer.customer.name) if l.offer.customer_id else '—'} · "
            f"× {_num(l.qty)} по {_num(l.unit_price)} {l.offer.currency}" + (f" · строк {l.lead_time}" if l.lead_time else "")
            for l in offer_lines))
    return [f"продажів {len(sales)}", f"закупівель {len(pos)}", f"Angebote {len(offer_lines)}"]


MINERVA = {"project": _minerva_project, "customer": _minerva_customer, "order": _minerva_order,
           "product": _minerva_product}


def minerva_sources(kind, obj):
    """Лише дані Minerva картки (без листів і RAG) → (Sources, деталь етапу)."""
    from email_assistant.reply_brief import Sources
    src = Sources()
    detail = MINERVA[kind](src, obj)
    return src, detail


def fingerprint(src) -> str:
    """Стан даних Minerva картки: інший — огляд застарів."""
    return hashlib.sha256(src.text().encode("utf-8")).hexdigest()


# ── Листи з власних скриньок ─────────────────────────────────────────────────

def _mail_scope(kind, obj):
    """→ (умова на листи, слова для показу фрагмента, гілки, які вже є в даних і RAG не повторює)."""
    if kind == "project":
        from projects import services
        ids = list(services.project_messages(obj).values_list("pk", flat=True)[:2000])
        threads = list(services.project_messages(obj).exclude(thread=None).values_list("thread_id", flat=True).distinct()[:100])
        return Q(pk__in=ids), [obj.code], threads
    if kind == "customer":
        from email_assistant.crm_link import FREE_MAIL_DOMAINS
        cond, needles = Q(thread__crm_customer=obj), []
        email = (obj.email or "").strip().lower()
        if email:   # і листи, які переслали колеги: адреса клієнта — лише в тексті
            cond |= Q(from_email__iexact=email) | Q(to_emails__icontains=email) | Q(body_text__icontains=email)
            needles.append(email)
            domain = email.split("@", 1)[1] if "@" in email else ""
            if domain and domain not in FREE_MAIL_DOMAINS:   # колеги клієнта з тієї ж фірми
                cond |= Q(from_email__iendswith=f"@{domain}")
        return cond, needles or [obj.company or obj.name], []
    if kind == "order":
        needles = [n for n in [obj.order_number, obj.tracking_number] + [s.tracking_number for s in obj.shipments.all()[:3]]
                   if n and len(n) >= 5]
        cond = Q(pk__in=[])
        for n in dict.fromkeys(needles):
            cond |= Q(subject__icontains=n) | Q(body_text__icontains=n)
        return cond, needles, []
    needles = [n for n in [obj.sku, obj.sku_short] if n and len(n) >= 4]
    cond = Q(pk__in=[])
    for n in dict.fromkeys(needles):
        cond |= Q(subject__icontains=n) | Q(body_text__icontains=n)
    return cond, needles, []


def _mail(src, user, kind, obj) -> tuple:
    """Останній лист кожної гілки (до MAIL_LETTERS) з власних скриньок користувача → (pk листів, гілки, деталь)."""
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.models import EmailMessage
    from email_assistant.quotes import around, new_text, strip_signature, tidy
    cond, needles, project_threads = _mail_scope(kind, obj)
    qs = (EmailMessage.objects.filter(cond, account__user=user, is_deleted=False).exclude(is_spam=True)
          .exclude(folder__in=("spam", "trash")).exclude(category="newsletter").order_by("-sent_at", "-pk"))
    own, seen_threads, pks = own_addresses(), set(), []
    for m in qs[:MAIL_SCAN]:
        key = m.thread_id or f"m{m.pk}"
        if key in seen_threads:
            continue
        seen_threads.add(key)
        if len(pks) >= MAIL_LETTERS:
            continue
        body = strip_signature(new_text(m.body_text or "")).strip()
        text = body[:MAIL_CHARS]
        if needles and not any(n.lower() in text.lower() for n in needles):
            piece = around(tidy(m.body_text or ""), needles, 600)   # згадка — у цитаті нижче
            text = (text[:MAIL_CHARS - 650] + "\n[…] " + piece) if piece else text
        recipients = list(m.to_emails or []) + list(m.cc_emails or [])
        if not is_own(m.from_email, own) and m.folder != "sent":
            direction = "← ОТРИМАЛИ"
        elif recipients and all(is_own(a, own) for a in recipients):
            direction = "↔ ВНУТРІШНІЙ (колега)"   # напр. колега переслав лист клієнта
        else:
            direction = "→ НАДІСЛАЛИ МИ"
        when = timezone.localtime(m.sent_at).strftime("%d.%m.%Y %H:%M") if m.sent_at else "?"
        src.add("mail", f"{direction} · {when} · {m.from_email} · «{m.subject or '—'}»", text, f"/email/message/{m.pk}/")
        pks.append(m.pk)
    threads = [t for t in seen_threads if isinstance(t, int)]
    detail = (f"листів {len(pks)} (гілок {len(seen_threads)})" if pks else "у ваших скриньках не знайдено")
    return pks, list(dict.fromkeys(project_threads + threads)), detail


# ── RAG ──────────────────────────────────────────────────────────────────────

def _documents(src, user, kind, obj) -> tuple:
    """Файли й посилання, прив'язані до картки (knowledge_items), які бачить користувач — текст бере Minerva сама,
    без RAG. → (document_id для пропуску в RAG, деталь етапу)."""
    from . import knowledge_items
    ids = []
    for item in knowledge_items.items_for(user, kind, obj)[:DOCUMENTS]:
        text = knowledge_items.local_text(item)
        src.add("document", f"{item.get_kind_display()} «{item.title}»"
                + (f" · {item.file_name}" if item.file_name else "") + (f" · {item.url}" if item.url else ""),
                text or "(тексту ще немає — лише назва)", f"/rag/knowledge/{item.pk}/")
        ids.append(item.document_id)
    return ids, (f"документів {len(ids)}" if ids else "не додано")


def _rag(src, user, kind, obj, question, seen, threads, dedup, seen_documents=()) -> tuple:
    """→ (status, detail, note). Фільтр клієнта — лише для клієнта (і проєкту з клієнтом); решта — уся пошта."""
    try:
        from rag_assistant import retrieval
    except ImportError:
        return "skipped", "RAG не підключено", ""
    if not retrieval.available():
        return "skipped", "RAG не налаштований", ""
    if not retrieval.available_for(user):
        return "skipped", "немає права на пошук у базі знань", ""
    from email_assistant.reply_brief import rag_sources
    customer_id = obj.pk if kind == "customer" else (obj.customer_id if kind == "project" else None)
    scope = (f"клієнта {obj.company or obj.name}" if kind == "customer"
             else f"клієнта проєкту" if customer_id else "усій вашій пошті")
    return rag_sources(src, user, build_query(kind, obj, question), scope=scope, seen=seen, dedup=dedup,
                       customer_id=customer_id, exclude_thread_ids=threads, purpose="entity",
                       seen_documents=set(seen_documents))


# ── Запуск ───────────────────────────────────────────────────────────────────

def _stages(mode) -> list:
    keys = ["minerva", "documents", "mail", "rag", mode]
    return [{"key": k, "label": label, "status": "pending", "detail": ""} for k, label in STAGES if k in keys]


def start(user, kind, obj, *, mode, question=""):
    """Новий огляд / відповідь у фоні. → EntityHelp (running)."""
    from email_assistant.reply_brief import _in_background
    from .models import EntityHelp
    help_ = EntityHelp.objects.create(user=user, kind=kind, obj_id=obj.pk, mode=mode, question=(question or "")[:2000],
                                      target=title(kind, obj)[:300], stages=_stages(mode))
    _in_background(lambda: _run(help_.pk))
    return help_


def _run(pk):
    from email_assistant.reply_brief import Sources, _model_label
    from .models import EntityHelp
    h = EntityHelp.objects.select_related("user").get(pk=pk)

    def set_stage(key, status, detail=""):
        for s in h.stages:
            if s["key"] == key:
                s["status"], s["detail"] = status, (detail or "")[:220]
        h.save(update_fields=["stages", "updated_at"])

    try:
        obj = get_object(h.kind, h.obj_id)
        if obj is None:
            raise RuntimeError("Картку видалено")
        set_stage("minerva", "running")
        src, detail = minerva_sources(h.kind, obj)
        h.fingerprint = fingerprint(src)
        set_stage("minerva", "done", " · ".join(detail))
        set_stage("documents", "running")
        doc_ids, detail = _documents(src, h.user, h.kind, obj)
        set_stage("documents", "done" if doc_ids else "skipped", detail)
        set_stage("mail", "running")
        pks, threads, detail = _mail(src, h.user, h.kind, obj)
        set_stage("mail", "done" if pks else "skipped", detail)
        set_stage("rag", "running")
        dedup = {}
        status, detail, note = _rag(src, h.user, h.kind, obj, h.question, set(pks), threads, dedup, doc_ids)
        set_stage("rag", status, detail)
        h.sources = src.public()
        h.used = [f"даних Minerva {sum(1 for i in src.items if i['kind'] in ('minerva', 'offer', 'overview'))}",
                  f"листів {len(pks)}"] + ([f"документів {len(doc_ids)}"] if doc_ids else []) + ([note] if note else [])
        h.save(update_fields=["sources", "used", "fingerprint", "updated_at"])
        set_stage(h.mode, "running")
        if h.mode == EntityHelp.MODE_REVIEW:
            h.result = _review(h, src)
        else:
            h.result = _answer(h, src)
        h.model = _ai_model()
        h.used = h.used + [f"AI — {_model_label(h.model)}"]
        set_stage(h.mode, "done", f"модель {_model_label(h.model)}")
        h.status = EntityHelp.DONE
    except Exception as e:  # noqa: BLE001
        logger.exception("entity help %s failed: %s", pk, e)
        for s in h.stages:
            if s["status"] == "running":
                s["status"] = "failed"
        h.status, h.error = EntityHelp.ERROR, str(e)[:300]
    h.save()


def _ai_model() -> str:
    from email_assistant import ai_helper
    return ai_helper.last_model()[:60]


def _about(h) -> str:
    return f"картку {KIND_LABELS[h.kind]} «{h.target}»"


def _answer(h, src) -> str:
    from email_assistant import ai_helper
    text = ai_helper.answer_from_sources(src.text(), h.question, h.target, None, getattr(h.user, "profile", None),
                                         about=_about(h))
    if not text:
        raise RuntimeError("AI не відповів — перевірте ключ AI або спробуйте ще раз")
    return text


def _review(h, src) -> str:
    from email_assistant import ai_helper
    from projects.overview import _parse_json, validate
    raw = ai_helper.review_from_sources(src.text(), h.target, None, getattr(h.user, "profile", None), about=_about(h))
    data = _parse_json(raw)
    if data is None:
        raise RuntimeError("AI не відповів у форматі огляду — спробуйте ще раз")
    by_n = {i["n"]: i for i in src.items}

    def resolve(value):
        m = re.search(r"#\s*(\d+)", str(value or ""))
        item = by_n.get(int(m.group(1))) if m else None
        return {"label": f"#{item['n']}", "url": item.get("url") or ""} if item else {"label": "", "url": ""}
    return json.dumps(validate(data, [], {}, resolve=resolve), ensure_ascii=False)


# ── Для браузера ─────────────────────────────────────────────────────────────

def latest(user, kind, obj_id, mode):
    from .models import EntityHelp
    return EntityHelp.objects.filter(user=user, kind=kind, obj_id=obj_id, mode=mode).first()


def as_json(h, check_stale=False) -> dict:
    """Стан для браузера; тексти джерел — лише коли готово. check_stale — порівняти дані Minerva з поточними."""
    from email_assistant.reply_brief import Sources
    from projects.overview import for_display
    running = h.status == h.RUNNING
    review = None
    if h.mode == h.MODE_REVIEW and h.result and not running:
        try:
            review = for_display(json.loads(h.result))
        except ValueError:
            review = None
    stale = False
    if check_stale and not running and h.fingerprint:
        obj = get_object(h.kind, h.obj_id)
        try:
            stale = obj is not None and fingerprint(minerva_sources(h.kind, obj)[0]) != h.fingerprint
        except Exception:  # noqa: BLE001 — показ не має ламатися
            stale = False
    return {"ok": True, "id": h.pk, "kind": h.kind, "mode": h.mode, "status": h.status, "error": h.error,
            "question": h.question, "stages": h.stages, "answer": "" if h.mode == h.MODE_REVIEW else h.result,
            "review": review, "sources": Sources(h.sources).public(texts=not running), "used": h.used,
            "stale": stale, "gathered": timezone.localtime(h.created_at).strftime("%d.%m.%Y %H:%M")}
