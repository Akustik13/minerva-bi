"""
Етап 3 — Minerva сама додає документи в базу знань (KnowledgeItem.auto; вид offer / order_file / datasheet) за
галочками на «🧠 База знань» (KnowledgeSettings.auto_*; за замовчуванням вимкнено — це масова індексація):
- offer      — кожна надіслана версія Angebot (OfferRevision.sent_at): PDF, інакше DOCX, і текст позицій зі знімка
               (номер, клієнт, артикули, кількості, ціни, строки) — прив'язки: Angebot, клієнт, проєкт, замовлення;
- order_file — файли з папки замовлення media/orders/<джерело>/<номер>/ (PDF, DOCX, TXT, CSV) — замовлення
               й клієнт; лише замовлення не старші за «Листи починаючи з» (index_from), якщо його задано;
- datasheet  — файл даташита в картці товару (Product.datasheet_file) — товар.
Файл не копіюється: KnowledgeItem.file вказує на той самий файл у MEDIA_ROOT, і автоматичний документ
прибирається лише записом (файл лишається). Видимість — team (усі, хто бачить пов'язану картку).
sync_all() — у звірці стрічки (knowledge.refresh_all: після збереження налаштувань і щоночі); sync_offer /
sync_product — одразу після надсилання Angebot / зміни даташита (rag_assistant/signals.py).
Галочку знято → автоматичні документи цього виду прибираються (tombstone у стрічці).
"""
import logging
import mimetypes
import os

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

FILE_TYPES = ("pdf", "docx", "txt", "csv")     # те, з чого RAG і Minerva вміють витягти текст
ORDER_FILES_MAX = 20
TEXT_LINES = 40
BATCH = 300


def _media_rel(name) -> str:
    """Відносний шлях файлу в MEDIA_ROOT, якщо файл існує (і лише всередині MEDIA_ROOT)."""
    if not name:
        return ""
    root = os.path.normpath(str(settings.MEDIA_ROOT))
    path = os.path.normpath(os.path.join(root, str(name)))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return ""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _ext(name) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


# ── Що має бути в базі знань ─────────────────────────────────────────────────

def _offer_spec(rev) -> dict:
    offer = rev.offer
    snap = rev.snapshot or {}
    number = snap.get("display_number") or offer.display_number
    customer = snap.get("customer") or {}
    lines = [f"Angebot {number} (версія {rev.revision}) від {snap.get('date') or ''}"
             + (f", діє до {snap['valid_until']}" if snap.get("valid_until") else ""),
             f"Клієнт: {customer.get('company') or customer.get('name') or ''}".rstrip(": "),
             f"Валюта {snap.get('currency') or offer.currency}, нетто {snap.get('net_total') or ''}, "
             f"брутто {snap.get('gross_total') or ''}"]
    for l in (snap.get("lines") or [])[:TEXT_LINES]:
        tiers = "; ".join(f"{t.get('qty')}: {t.get('unit_price')}" for t in (l.get("tiers") or []))
        lines.append(f"- {l.get('sku') or ''} {l.get('description') or ''} × {l.get('qty')} {l.get('unit') or ''} "
                     f"по {l.get('unit_price')}" + (f" (ступені {tiers})" if tiers else "")
                     + (f", строк {l['lead_time']}" if l.get("lead_time") else ""))
    for key, label in (("delivery_terms", "Умови поставки"), ("payment_terms", "Оплата")):
        if snap.get(key):
            lines.append(f"{label}: {snap[key]}")
    return {"title": f"Angebot {number} · версія {rev.revision}",
            "file": _media_rel(rev.pdf_file.name if rev.pdf_file else "") or _media_rel(rev.docx_file.name if rev.docx_file else ""),
            "text": "\n".join(x for x in lines if x.strip()),
            "customer_id": offer.customer_id, "project_id": offer.project_id, "order_id": offer.order_id,
            "offer_id": offer.pk, "product_id": None}


def _order_specs(order) -> dict:
    from email_assistant.attachments import order_dir
    from .entity_help import _order_customer
    folder = order_dir(order)
    try:
        names = sorted((e.name for e in os.scandir(folder) if e.is_file() and _ext(e.name) in FILE_TYPES),
                       key=lambda n: n.lower())[:ORDER_FILES_MAX]
    except OSError:
        return {}
    if not names:
        return {}
    customer = _order_customer(order)
    rel_dir = os.path.relpath(folder, os.path.normpath(str(settings.MEDIA_ROOT))).replace(os.sep, "/")
    out = {}
    for name in names:
        rel = _media_rel(f"{rel_dir}/{name}")
        if rel:
            out[f"order_file:{order.pk}:{name}"[:300]] = {
                "title": f"{name} · замовлення {order.source}:{order.order_number}", "file": rel,
                "text": f"Документ замовлення {order.source}:{order.order_number}"
                        + (f", клієнт {order.client}" if order.client else ""),
                "customer_id": customer.pk if customer else None, "project_id": None, "order_id": order.pk,
                "offer_id": None, "product_id": None}
    return out


def _datasheet_spec(product):
    rel = _media_rel(product.datasheet_file.name if product.datasheet_file else "")
    if not rel or _ext(rel) not in FILE_TYPES:
        return None
    return {"title": f"Даташит {product.sku}" + (f" · {product.name}" if product.name else ""), "file": rel,
            "text": f"Даташит товару {product.sku}" + (f" ({product.manufacturer})" if product.manufacturer else ""),
            "customer_id": None, "project_id": None, "order_id": None, "offer_id": None, "product_id": product.pk}


# ── Застосування ─────────────────────────────────────────────────────────────

def _fill(item, spec) -> bool:
    """Записати spec у KnowledgeItem; → чи щось змінилось."""
    root = str(settings.MEDIA_ROOT)
    size = os.path.getsize(os.path.join(root, spec["file"])) if spec["file"] else 0
    values = {"title": spec["title"][:300], "text": spec["text"], "file_name": os.path.basename(spec["file"])[:300],
              "file_size": size, "customer_id": spec["customer_id"], "project_id": spec["project_id"],
              "order_id": spec["order_id"], "offer_id": spec["offer_id"], "product_id": spec["product_id"]}
    changed = (item.file.name if item.file else "") != spec["file"]
    for field, value in values.items():
        if getattr(item, field) != value:
            changed = True
            setattr(item, field, value)
    if changed:
        item.file = spec["file"] or ""
        item.content_type = (mimetypes.guess_type(spec["file"])[0] or "")[:200] if spec["file"] else ""
        item.file_sha256 = ""            # вміст міг змінитись — порахується заново при передачі
    return changed


def _apply(kind, desired, existing_qs) -> dict:
    """Звести автоматичні документи виду kind (existing_qs) до desired {source_key: spec} → лічильники."""
    from . import knowledge, knowledge_items
    from .models import KnowledgeItem
    existing = {i.source_key: i for i in existing_qs}
    touched, created, updated = [], 0, 0
    with knowledge_items.quiet():           # без подій на кожен запис — пачками нижче
        for key, spec in desired.items():
            item = existing.pop(key, None)
            if item is None:
                item = KnowledgeItem(kind=kind, auto=True, source_key=key, visibility=KnowledgeItem.TEAM)
                _fill(item, spec)
                item.save()
                created += 1
            elif _fill(item, spec):
                item.save()
                updated += 1
            else:
                continue
            touched.append(item.pk)
        removed = [i.pk for i in existing.values()]
        if removed:
            KnowledgeItem.objects.filter(pk__in=removed).delete()   # лише записи: файли належать Angebot / замовленню / товару
    pks = touched + removed
    for i in range(0, len(pks), BATCH):
        knowledge.touch_docs(knowledge_items.DOC_TYPE, pks[i:i + BATCH])
    return {"created": created, "updated": updated, "removed": len(removed)}


def _auto(kind):
    from .models import KnowledgeItem
    return KnowledgeItem.objects.filter(auto=True, kind=kind)


def sync_offers(settings_obj=None, offer=None) -> dict:
    from offers.models import OfferRevision
    from .models import KnowledgeSettings
    s = settings_obj or KnowledgeSettings.get()
    existing = _auto("offer") if offer is None else _auto("offer").filter(offer=offer)
    if not s.auto_offers:
        return _apply("offer", {}, existing)
    revs = OfferRevision.objects.filter(sent_at__isnull=False).select_related("offer")
    if offer is not None:
        revs = revs.filter(offer=offer)
    return _apply("offer", {f"offer:{r.pk}": _offer_spec(r) for r in revs.order_by("pk")}, existing)


def sync_orders(settings_obj=None, order=None) -> dict:
    from sales.models import SalesOrder
    from .models import KnowledgeSettings
    s = settings_obj or KnowledgeSettings.get()
    existing = _auto("order_file") if order is None else _auto("order_file").filter(order=order)
    if not s.auto_order_files:
        return _apply("order_file", {}, existing)
    orders = SalesOrder.objects.all() if order is None else SalesOrder.objects.filter(pk=order.pk)
    if s.index_from and order is None:
        orders = orders.filter(order_date__gte=s.index_from)
    desired = {}
    for o in orders.order_by("pk").iterator(chunk_size=500):
        desired.update(_order_specs(o))
    return _apply("order_file", desired, existing)


def sync_datasheets(settings_obj=None, product=None) -> dict:
    from inventory.models import Product
    from .models import KnowledgeSettings
    s = settings_obj or KnowledgeSettings.get()
    existing = _auto("datasheet") if product is None else _auto("datasheet").filter(source_key=f"datasheet:{product.pk}")
    if not s.auto_datasheets:
        return _apply("datasheet", {}, existing)
    products = Product.objects.exclude(datasheet_file="").exclude(datasheet_file__isnull=True)
    if product is not None:
        products = products.filter(pk=product.pk)
    desired = {}
    for p in products.order_by("pk").iterator(chunk_size=500):
        spec = _datasheet_spec(p)
        if spec:
            desired[f"datasheet:{p.pk}"] = spec
    return _apply("datasheet", desired, existing)


def sync_all() -> dict:
    """Усі три види — у звірці стрічки (knowledge.refresh_all). Збій одного виду не зупиняє інші."""
    from .models import KnowledgeSettings
    s = KnowledgeSettings.get()
    result = {}
    for name, fn in (("offer", sync_offers), ("order_file", sync_orders), ("datasheet", sync_datasheets)):
        try:
            result[name] = fn(s)
        except Exception as e:  # noqa: BLE001
            logger.exception("knowledge auto %s failed: %s", name, e)
            result[name] = {"error": str(e)[:200]}
    if any(v.get("created") or v.get("updated") or v.get("removed") for v in result.values()):
        logger.info("knowledge auto documents %s at %s", result, timezone.now())
    return result
