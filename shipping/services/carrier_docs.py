"""
shipping/services/carrier_docs.py — митні документи однаково для UPS, DHL Express і FedEx.

Що тут:
- які типи документів приймає перевізник (`DOC_CODES`: внутрішні коди Minerva = коди UPS з ups_docs.DOC_TYPES →
  код перевізника), формати, розмір файлу, скільки документів на посилку;
- `booked_ok` — посилку оформлено через API перевізника, до неї можна надіслати документи;
- `upload` — надіслати документи до вже оформленої посилки (UPS Paperless / DHL upload-image /
  FedEx ETD post-shipment), повертає [(назва, код, ID документа)];
- `needs_customs` — посилка перетинає кордон ЄС (і для імпорту до нас), `paperless` — інвойс пішов
  перевізнику електронно, `invoice_copies` — скільки примірників інвойсу друкувати («🖨️ Друкувати всі документи»).

Що надіслано — рядки «<UPS|DHL|FEDEX>_DOC | дата | тип | DocID:… | файл» у нотатках відправлення (ups_docs.sent).
"""
from django.utils import timezone

from . import ups_docs

EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT",
      "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}

CARRIERS = ("ups", "dhl", "fedex")
NAMES = {"ups": "UPS", "dhl": "DHL", "fedex": "FedEx"}
PREFIX = {"ups": "UPS", "dhl": "DHL", "fedex": "FEDEX"}

# Паперовий інвойс: перевізники просять 3 примірники (один — на коробку в кишеню, два — всередину / митниці)
INVOICE_COPIES = 3

# Код Minerva (ups_docs.DOC_TYPES) → тип документа перевізника. DHL upload-image знає лише INV / PNV / COO / NAF /
# CIN / DCL / AWB, тож решта йде як «Customs declaration» (DCL); FedEx ETD — COMMERCIAL_INVOICE / PRO_FORMA_INVOICE /
# CERTIFICATE_OF_ORIGIN / USMCA_… , решта — OTHER.
DOC_CODES = {
    "ups": {code: code for code, *_ in ups_docs.DOC_TYPES},
    "dhl": {"002": "CIN", "003": "COO", "007": "NAF", "004": "DCL", "013": "DCL", "010": "DCL", "005": "DCL",
            "006": "DCL", "009": "DCL", "001": "DCL", "011": "DCL", "012": "DCL", "008": "DCL"},
    "fedex": {"002": "COMMERCIAL_INVOICE", "003": "CERTIFICATE_OF_ORIGIN", "007": "USMCA_CERTIFICATION_OF_ORIGIN",
              "004": "OTHER", "013": "OTHER", "010": "OTHER", "005": "OTHER", "006": "OTHER", "009": "OTHER",
              "001": "OTHER", "011": "OTHER", "012": "OTHER", "008": "OTHER"},
}
FORMATS = {
    "ups": tuple(ups_docs.UPS_FORMATS),
    "dhl": ("pdf", "png", "gif", "tif", "tiff", "jpg", "jpeg"),
    "fedex": ("pdf", "png", "gif", "jpg", "jpeg", "doc", "docx", "txt", "rtf", "xls", "xlsx"),
}
FILE_MAX = {"ups": ups_docs.FILE_MAX, "dhl": 5 * 1024 * 1024, "fedex": 5 * 1024 * 1024}
PER_PARCEL = {"ups": ups_docs.PER_PARCEL, "dhl": 10, "fedex": 5}
HOW = {
    "ups": "UPS Paperless",
    "dhl": "DHL Paperless Trade (upload-image)",
    "fedex": "FedEx Electronic Trade Documents",
}
BOOKED = ("submitted", "label_ready", "in_transit", "delivered")


class CarrierDocError(Exception):
    pass


def kind(shipment) -> str:
    c = getattr(shipment, "carrier", None)
    t = (getattr(c, "carrier_type", "") or "").lower() if c else ""
    return t if t in CARRIERS else ""


def booked_ok(shipment) -> bool:
    """Посилку оформлено через API перевізника — до неї можна надіслати митні документи."""
    k = kind(shipment)
    if not k or shipment.status not in BOOKED:
        return False
    number = (shipment.carrier_shipment_id or shipment.tracking_number or "").strip()
    if not number:
        return False
    if k == "dhl":                       # upload-image — лише для посилки, оформленої через MyDHL API
        return bool((shipment.raw_request or {}).get("productCode")) if isinstance(shipment.raw_request, dict) else False
    return True


def doc_types(k: str) -> list:
    """[(код Minerva, назва, як у перевізника, пояснення)] для вибору типу в блоці документів."""
    out = []
    for code, name, de, hint in ups_docs.DOC_TYPES:
        if k == "ups":
            out.append((code, name, f"ups.com: «{de}»", hint))
        elif code in DOC_CODES.get(k, {}):
            out.append((code, name, f"{NAMES[k]}: {DOC_CODES[k][code]}", hint))
    return out


def note_line(k: str, code, doc_id, name) -> str:
    return (f"{PREFIX.get(k, 'UPS')}_DOC | {timezone.localtime().strftime('%d.%m.%Y %H:%M')} | {code} | "
            f"DocID:{doc_id or '-'} | {name}")


def check(k: str, files: list, already: int) -> str:
    """Порожньо — можна надсилати; інакше — чому ні. files: [(назва, байти, код)]."""
    fmts, size, per = FORMATS[k], FILE_MAX[k], PER_PARCEL[k]
    for name, data, code in files:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in fmts:
            return f"{NAMES[k]} приймає лише {', '.join(fmts)} — «{name}» не підходить."
        if len(data) > size:
            return f"«{name}» більший за {size // (1024 * 1024)} МБ — {NAMES[k]} його не прийме."
        if code not in DOC_CODES[k]:
            return f"Невідомий тип документа для «{name}»."
    if already + len(files) > per:
        return f"{NAMES[k]} приймає до {per} документів на посилку — більше додати не можна."
    return ""


def upload(shipment, files: list) -> list:
    """Надіслати документи до вже оформленої посилки. files: [(назва, байти, код Minerva)] →
    [(назва, код Minerva, ID документа)]; відмова перевізника — CarrierDocError."""
    k = kind(shipment)
    shipped_at = timezone.localtime(shipment.submitted_at or shipment.created_at)
    if k == "ups":
        from shipping.ups_client import UPSClient, UPSError
        ups_id = (shipment.carrier_shipment_id or shipment.tracking_number).strip()
        tracking = [shipment.tracking_number or ups_id] + [t for t in (shipment.package_tracking or [])
                                                           if t and t != shipment.tracking_number]
        done = []
        try:
            client = UPSClient(carrier=shipment.carrier)
            for name, data, code in files:
                ext = name.rsplit(".", 1)[-1].lower()
                done.append((name, code, client.upload_paperless_document(
                    data, name, document_type=code, file_format="jpg" if ext == "jpeg" else ext)))
            client.push_paperless_documents([d for _n, _c, d in done], ups_id, tracking, shipped_at)
        except UPSError as e:
            raise CarrierDocError(str(e)) from e
        return done
    if k == "dhl":
        from . import dhl
        res = dhl.upload_images(shipment.carrier, shipment,
                                [(n, d, DOC_CODES["dhl"][c]) for n, d, c in files])
        if not res.get("success"):
            raise CarrierDocError(res.get("error") or "DHL не прийняв документи")
        return [(n, c, "-") for n, _d, c in files]
    if k == "fedex":
        from shipping.fedex_client import FedExClient, FedExError
        origin = (shipment.sender_country or shipment.carrier.sender_country or "DE").upper()
        dest = (shipment.recipient_country or "").upper()
        done = []
        try:
            client = FedExClient(carrier=shipment.carrier)
            for name, data, code in files:
                done.append((name, code, client.upload_etd(
                    data, name, DOC_CODES["fedex"][code], origin, dest,
                    tracking=(shipment.tracking_number or "").strip(), ship_date=shipped_at)))
        except FedExError as e:
            raise CarrierDocError(str(e)) from e
        return done
    raise CarrierDocError("Документи надсилаються лише для посилок UPS, DHL і FedEx, оформлених у Minerva.")


def needs_customs(shipment) -> bool:
    """Посилка перетинає кордон ЄС: експорт з ЄС, імпорт до ЄС (і до нас) або між двома країнами поза ЄС."""
    c = getattr(shipment, "carrier", None)
    src = (shipment.sender_country or (getattr(c, "sender_country", "") if c else "") or "DE").upper()
    dst = (shipment.recipient_country or "").upper()
    if not dst or src == dst:
        return False
    return not (src in EU and dst in EU)


def paperless(shipment) -> bool:
    """Інвойс пішов перевізнику електронно — паперові примірники до посилки не потрібні."""
    k = kind(shipment)
    notes = shipment.notes or ""
    if k == "ups":
        return bool(shipment.use_custom_invoice and shipment.ups_document_id)
    if k == "dhl":
        return "| Paperless Trade" in notes
    if k == "fedex":
        return "FEDEX_ETD |" in notes
    return False


def invoice_copies(shipment) -> int:
    """Скільки примірників інвойсу класти в «🖨️ Друкувати всі документи»: без митниці — 0, електронно — 1
    (для себе), на папері — INVOICE_COPIES."""
    if not needs_customs(shipment):
        return 0
    return 1 if paperless(shipment) else INVOICE_COPIES
