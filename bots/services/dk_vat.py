"""📎 PDF рахунку до замовлення DigiKey Marketplace при підтвердженні відправки (ЄС).

DigiKey Marketplace API (Orders v1; специфікація на developer.digikey.com станом на 11.2026) у
PUT /orders/{id}/ship приймає лише трек-номери, supplierInvoiceNumber і netVatInvoiceAmount — поля для файлу
рахунку там немає. Файл можна завантажити (FileDocuments: POST /fileDocument → id), а замовлення показує рахунок
лише для читання: vatFileId / vatFileName і shippingTrackingInfoList[].supplierVATObjectId. Тому:

1. `prepare` — до підтвердження: у замовленні на DigiKey рахунку ще немає → PDF (з форми або «Інвойси DigiKey»)
   завантажується з supplierId замовлення (`digikey.upload_vat_invoice`);
2. `attach` — id файлу йде в кожен трек-номер як supplierVATObjectId (недокументовано); DigiKey відхилив запит
   (`retry_without`: 400 не через кількості) — той самий запит без поля (`digikey._put_ship`);
3. `verify` — після успішного підтвердження замовлення перечитується: чи DigiKey тепер показує рахунок.

`message` — що сказати людині, `remember` → `DigiKeyConfig.vat_link_last` (панель у налаштуваннях DigiKey).
"""
import copy
import logging
import os

from django.utils import timezone

logger = logging.getLogger(__name__)

EU = frozenset({"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
                "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"})
FIELD = "supplierVATObjectId"
PDF_MAX = 10 * 1024 * 1024

LINKED, IGNORED, REJECTED, UPLOAD_FAILED, ALREADY, UNKNOWN, NO_PDF = (
    "linked", "ignored", "rejected", "upload_failed", "already", "unknown", "no_pdf")

# коротко — для панелі в налаштуваннях DigiKey
STATE_TEXT = {
    LINKED: "✅ рахунок прикріплено до замовлення",
    IGNORED: "⚠️ DigiKey прийняв підтвердження, але рахунку в замовленні не показує",
    REJECTED: "⚠️ DigiKey не приймає рахунок разом із підтвердженням",
    UPLOAD_FAILED: "⚠️ PDF не завантажився на DigiKey",
    ALREADY: "ℹ️ у замовленні на DigiKey рахунок уже був",
    UNKNOWN: "⚠️ не вдалося перечитати замовлення з DigiKey",
}


def is_eu(order) -> bool:
    return (getattr(order, "addr_country", "") or "").upper() in EU


def invoice_for(order):
    """Найновіший рахунок «Інвойси DigiKey» цього замовлення (за замовленням або номером DigiKey)."""
    from django.db.models import Q
    from shipping.models import Invoice
    return (Invoice.objects.filter(Q(sales_order=order) | Q(digikey_order_no=order.order_number))
            .order_by("-invoice_date", "-pk").first())


def invoice_pdf(order, invoice=None):
    """(байти, назва) PDF рахунку замовлення або None: немає рахунку, PDF не створився (лише DOCX), файлу немає,
    не PDF за першими байтами, більше за PDF_MAX."""
    inv = invoice or invoice_for(order)
    if not inv or not inv.pdf_file:
        return None
    try:
        with open(inv.pdf_file.path, "rb") as fh:
            data = fh.read(PDF_MAX + 1)
    except (OSError, ValueError, NotImplementedError):
        return None
    if not data.startswith(b"%PDF") or len(data) > PDF_MAX:
        return None
    return data, os.path.basename(inv.pdf_file.name)


def pdf_name(order, invoice=None) -> str:
    """Назва PDF, яку Minerva прикріпить (для форми й сторінки відправлення); '' — немає."""
    doc = invoice_pdf(order, invoice)
    return doc[1] if doc else ""


def shown(order_data) -> dict:
    """Що DigiKey показує як рахунок замовлення: {'id', 'name', 'objects'}."""
    order_data = order_data or {}
    objects = [str(t.get(FIELD)) for t in (order_data.get("shippingTrackingInfoList") or []) if t.get(FIELD)]
    return {"id": str(order_data.get("vatFileId") or ""), "name": order_data.get("vatFileName") or "",
            "objects": objects}


def _has(s) -> bool:
    return bool(s["id"] or s["name"] or s["objects"])


def prepare(config, order_data, document=None, file_id=None) -> dict:
    """До PUT /ship. Повертає стан спроби {'state', 'file_id', 'name', 'detail'}; file_id — що передати."""
    s = shown(order_data)
    if _has(s):
        return {"state": ALREADY, "file_id": None, "name": s["name"] or s["id"] or s["objects"][0], "detail": ""}
    if file_id:
        return {"state": None, "file_id": str(file_id), "name": "", "detail": ""}
    if not document:
        return {"state": None, "file_id": None, "name": "", "detail": ""}
    data, name = document
    from bots.services.digikey import upload_vat_invoice
    try:
        up = upload_vat_invoice(config, data, name, supplier_id=(order_data or {}).get("supplierId") or None)
    except Exception as e:                       # мережа, токен — підтвердження відправки йде далі без рахунку
        up = {"ok": False, "message": f"{type(e).__name__}: {e}"}
    if not up.get("ok") or not up.get("file_id"):
        return {"state": UPLOAD_FAILED, "file_id": None, "name": name,
                "detail": str(up.get("message") or "").lstrip("❌ ")[:300]}
    return {"state": None, "file_id": str(up["file_id"]), "name": name, "detail": ""}


def attach(payload: dict, file_id: str) -> dict:
    """Копія тіла PUT /ship з supplierVATObjectId у кожному трек-номері."""
    body = copy.deepcopy(payload)
    for entry in body.get("shippingTracking") or []:
        entry[FIELD] = file_id
    return body


def retry_without(status: int, detail) -> bool:
    """DigiKey відхилив запит із полем рахунку — повторити без нього? Лише 400 і не через кількості (та сама
    помилка повторилась би й без поля)."""
    return status == 400 and "shipped quantit" not in str(detail or "").lower()


def verify(config, order_id, token, vat: dict) -> dict:
    """Після успішного підтвердження: чи DigiKey тепер показує рахунок у замовленні."""
    if not vat or not vat.get("file_id") or vat.get("state") in (ALREADY, UPLOAD_FAILED):
        return vat
    from bots.services.digikey import _fetch_marketplace_order
    try:
        after = _fetch_marketplace_order(config, order_id, token)
    except Exception as e:
        logger.warning("dk_vat.verify %s: %s", order_id, e)
        after = {}
    s = shown(after)
    if vat["file_id"] in ({s["id"]} | set(s["objects"])) or _has(s):
        vat["state"] = LINKED
        vat["name"] = s["name"] or vat.get("name") or ""
    elif not after:
        vat["state"] = vat.get("state") or UNKNOWN
    elif vat.get("state") != REJECTED:
        vat["state"] = IGNORED
    return vat


def message(vat: dict, order_number: str):
    """(рівень django.contrib.messages, текст) або None."""
    if not vat or not vat.get("state"):
        return None
    st, name, n = vat["state"], vat.get("name") or "", order_number
    detail = f" ({vat['detail']})" if vat.get("detail") else ""
    portal = f"прикріпіть рахунок на supplier.digikey.com (замовлення {n})"
    if st == LINKED:
        return "success", f"📎 Рахунок «{name}» прикріплено до замовлення {n} на DigiKey — на сайті нічого робити не треба."
    if st == IGNORED:
        return "warning", (f"📎 PDF рахунку «{name}» надіслано на DigiKey, але в замовленні {n} його не видно — "
                           f"DigiKey не прив'язав файл через API. Будь ласка, {portal}.")
    if st == REJECTED:
        return "warning", (f"📎 DigiKey не приймає рахунок разом із підтвердженням{detail} — відправлення "
                           f"підтверджено без нього. Будь ласка, {portal}.")
    if st == UPLOAD_FAILED:
        return "warning", f"📎 PDF рахунку не завантажився на DigiKey{detail} — {portal}."
    if st == ALREADY:
        return "info", f"📎 У замовленні {n} на DigiKey рахунок уже є («{name}») — новий не надсилався."
    if st == UNKNOWN:
        return "warning", (f"📎 PDF рахунку «{name}» надіслано, але перечитати замовлення {n} з DigiKey не вдалося — "
                           f"перевірте на supplier.digikey.com.")
    if st == NO_PDF:
        return "info", (f"📎 Для замовлення {n} (ЄС) немає PDF рахунку — створіть його в «Інвойси DigiKey» "
                        f"або {portal}.")
    return None


def remember(config, vat: dict, order_number: str) -> None:
    """Результат спроби → DigiKeyConfig.vat_link_last (лише справжня спроба через API)."""
    if not vat or vat.get("state") in (None, NO_PDF):
        return
    from bots.models import DigiKeyConfig
    if not isinstance(config, DigiKeyConfig):
        return
    last = {"state": vat["state"], "at": timezone.now().isoformat(timespec="seconds"), "order": str(order_number),
            "file_id": vat.get("file_id") or "", "name": vat.get("name") or "", "detail": vat.get("detail") or ""}
    DigiKeyConfig.objects.filter(pk=config.pk).update(vat_link_last=last)
    config.vat_link_last = last


def last_text(config) -> str:
    """Рядок для панелі налаштувань: чим закінчилась остання спроба."""
    last = getattr(config, "vat_link_last", None) or {}
    if not last.get("state"):
        return "ще не було — спершу підтвердження відправки замовлення в ЄС з PDF рахунку"
    when = last.get("at") or ""
    try:
        from datetime import datetime
        when = timezone.localtime(datetime.fromisoformat(when)).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        pass
    text = STATE_TEXT.get(last["state"], last["state"])
    parts = [text, f"замовлення {last.get('order')}", when]
    if last.get("detail"):
        parts.append(last["detail"])
    return " · ".join(p for p in parts if p)
