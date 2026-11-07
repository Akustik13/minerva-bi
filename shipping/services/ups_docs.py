"""
shipping/services/ups_docs.py — документи для митниці в UPS до вже оформленої посилки (UPS Paperless).

Звідки файли: листи замовлення (лише свої скриньки) і папка документів замовлення media/orders/{джерело}/{номер}/.
Тип документа — 13 типів UPS (`DOC_TYPES`), автовизначення за назвою файлу, потім за текстом (`detect`).
До якої посилки — `match`: трек-номер / номер коробки / MRN цієї чи іншої посилки в тексті, інакше найближча
дата оформлення. Що вже надіслано — рядки «UPS_DOC | дата | тип | DocID:… | файл» у нотатках відправлення
(старі «UPS_ABD | …» — тип 004).
"""
import os
import re
from datetime import datetime, timezone as dt_tz

from django.conf import settings
from django.utils import timezone

from . import mrn as _mrn

UPS_FORMATS = _mrn.UPS_FORMATS
FILE_MAX = _mrn.UPS_FILE_MAX
PER_PARCEL = 13
TEXT_CHARS = 6000
IMAGES = ("jpg", "jpeg", "png", "gif", "bmp")

# (код, назва, як на ups.com, коли потрібен)
DOC_TYPES = [
    ("004", "ABD — Ausfuhrbegleitdokument", "Export-Begleitdokument",
     "Експортна декларація з MRN від митного брокера (ATLAS). Для експорту з ЄС понад 1000 € — "
     "UPS просить її, щоб випустити посилку."),
    ("002", "Commercial Invoice — рахунок для митниці", "Handelsrechnung",
     "Рахунок / Proforma з вартістю, HS-кодами й країною походження. Митниця країни отримувача рахує за ним "
     "мито й податки."),
    ("010", "Packing List — пакувальний лист", "Packliste",
     "Що в якій коробці, вага й розміри. Просять, коли коробок кілька або митниця перевіряє вміст."),
    ("003", "Certificate of Origin — сертифікат походження", "Ursprungszeugnis",
     "Від торгової палати (IHK). Потрібен для пільгового мита або коли цього вимагає країна отримувача."),
    ("013", "Declaration — декларація / заява", "Deklaration",
     "Заява відправника або виробника (Lieferantenerklärung, заява про подвійне призначення тощо)."),
    ("005", "Export License — експортна ліцензія", "Export-Lizenz",
     "Дозвіл BAFA на товари подвійного призначення, якщо він потрібен."),
    ("006", "Import Permit — дозвіл на ввезення", "Einfuhrgenehmigung",
     "Дозвіл країни отримувача; зазвичай його надає отримувач."),
    ("009", "Power of Attorney — довіреність", "Vollmacht",
     "Довіреність UPS діяти як митний представник (для деяких країн)."),
    ("001", "Authorization Form — форма доручення", "Autorisierungsformular",
     "Форма доручення UPS на митне оформлення."),
    ("011", "SED / EEI", "SED-Dokument", "Експортна декларація США (для відправлень зі США)."),
    ("012", "Shipper's Letter of Instruction", "Versenderanweisungen",
     "Інструкція відправника для експедитора."),
    ("007", "USMCA / NAFTA", "Einmalige NAFTA/CO", "Сертифікат походження для США / Канади / Мексики."),
    ("008", "Інший документ", "Sonstiges Dokument", "Усе інше, що просить UPS або митниця."),
]
TYPE_NAMES = {code: name for code, name, _de, _hint in DOC_TYPES}

_RULES = [
    ("004", r"ausfuhrbegleit|export[\s_-]*accompanying|\bABD\b|\bMRN\b|ausfuhranmeldung"),
    ("010", r"packing[\s_-]*list|packliste|lieferschein|\bPL[\s_-]?\d"),
    ("003", r"certificate[\s_-]*of[\s_-]*origin|ursprungszeugnis|origin[\s_-]*certificate|\bCOO\b"),
    ("005", r"export[\s_-]*licen[cs]e|ausfuhrgenehmigung|ausfuhrlizenz"),
    ("006", r"import[\s_-]*permit|einfuhrgenehmigung"),
    ("009", r"power[\s_-]*of[\s_-]*attorney|vollmacht"),
    ("007", r"\bUSMCA\b|\bNAFTA\b"),
    ("011", r"shipper'?s[\s_-]*export[\s_-]*declaration|\bSED\b|\bEEI\b"),
    ("012", r"letter[\s_-]*of[\s_-]*instruction|\bSLI\b"),
    ("001", r"authori[sz]ation[\s_-]*form|autorisierungsformular"),
    ("002", r"commercial[\s_-]*invoice|handelsrechnung|pro[\s_-]?forma|rechnung|invoice|\bCI[\s_-]?\d"),
    ("013", r"declaration|erkl(?:ä|ae)rung"),
]
_LABEL_FILE = re.compile(r"^(ups|dhl|fedex)_\d+_|label|etikett|\.dymo$", re.I)


def detect(name: str, text: str = "") -> tuple:
    """(код типу, чому) — за назвою файлу, потім за текстом; нічого — (None, '')."""
    stem = (name or "").rsplit(".", 1)[0].replace("_", " ")
    for code, rx in _RULES:
        if re.search(rx, stem, re.I):
            return code, "за назвою файлу"
    for code, rx in _RULES:
        m = re.search(rx, text or "", re.I)
        if m:
            return code, f"у тексті: «{m.group(0).strip()}»"
    return None, ""


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def order_folder(order):
    return os.path.join(str(settings.MEDIA_ROOT), "orders", order.source or "manual", order.order_number or "")


def live_parcels(order):
    from shipping.models import Shipment
    if not order:
        return []
    return list(order.shipments.exclude(status=Shipment.Status.CANCELLED).exclude(direction="in").order_by("pk"))


_SENT = re.compile(r"UPS_(?:DOC|ABD) \| ([^|]+?) \| (?:(\d{3}) \| )?DocID:(\S+) \| ([^\n]*)")


def sent(shipment) -> list:
    """[{when, type, type_name, doc, name}] — що вже надіслано в UPS до цієї посилки."""
    out = []
    for m in _SENT.finditer(shipment.notes or ""):
        code = m.group(2) or "004"
        out.append({"when": m.group(1).strip(), "type": code, "type_name": TYPE_NAMES.get(code, code),
                    "doc": m.group(3), "name": m.group(4).strip()})
    return out


def note_line(code, doc_id, name) -> str:
    return f"UPS_DOC | {timezone.localtime().strftime('%d.%m.%Y %H:%M')} | {code} | DocID:{doc_id} | {name}"


def _numbers(s):
    nums = {(s.tracking_number or "").strip().upper()} | {t.strip().upper() for t in (s.package_tracking or [])}
    return {n for n in nums if n}


def match(shipment, parcels, text: str, date, mrn_in: str = "") -> dict:
    """До якої посилки документ: {here: True|False|None, other: n|None, why}.
    here None — доказу немає (лише підказка за датою)."""
    up = (text or "").upper()
    for n, p in enumerate(parcels, 1):
        hit = next((t for t in _numbers(p) if t in up), None)
        if hit:
            if p.pk == shipment.pk:
                return {"here": True, "other": None, "why": f"у документі трек-номер цієї посилки {hit}"}
            return {"here": False, "other": n, "why": f"у документі трек-номер посилки {n} ({hit})"}
    if mrn_in:
        for n, p in enumerate(parcels, 1):
            if (p.export_mrn or "").upper() == mrn_in.upper():
                return ({"here": True, "other": None, "why": f"MRN {mrn_in} — як у цієї посилки"}
                        if p.pk == shipment.pk else
                        {"here": False, "other": n, "why": f"MRN {mrn_in} — як у посилки {n}"})
    if len(parcels) > 1 and date:
        def gap(p):
            when = p.submitted_at or p.created_at
            return abs((when - date).total_seconds()) if when else float("inf")
        best = min(range(len(parcels)), key=lambda i: gap(parcels[i]))
        if parcels[best].pk == shipment.pk:
            return {"here": None, "other": None, "why": "за датою — найближча посилка (перевірте)"}
        return {"here": None, "other": best + 1, "why": f"за датою ближче до посилки {best + 1} (перевірте)"}
    return {"here": None, "other": None, "why": ""}


def _text_of(att, path=None):
    try:
        from email_assistant.reply_brief import attachment_text
        return attachment_text(att, limit=TEXT_CHARS, path=path)
    except Exception:  # noqa: BLE001 — текст не обов'язковий
        return ""


def candidates(shipment, user) -> list:
    """Файли для UPS: [{ref, name, source mail|order, date, type, type_why, here, other, match_why, mrn,
    sent_to [n], checked}] — листи замовлення (свої скриньки) і папка документів замовлення."""
    order = shipment.order
    if not order:
        return []
    parcels = live_parcels(order)
    country = (shipment.carrier.sender_country if shipment.carrier_id else "") or "DE"
    sent_to = {}
    for n, p in enumerate(parcels, 1):
        for row in sent(p):
            sent_to.setdefault(row["name"], []).append(n)

    out, seen_names = [], set()
    if user is not None and getattr(user, "is_authenticated", False):
        for msg in _mrn._order_messages(order, user):
            hint = bool(_mrn.ABD_HINT.search(f"{msg.subject or ''} {(msg.body_text or '')[:2000]}"))
            for i, att in enumerate(msg.attachments or []):
                if not isinstance(att, dict) or att.get("is_inline"):
                    continue
                name = att.get("name") or ""
                ext = _ext(name)
                if ext not in UPS_FORMATS:
                    continue
                code, why = detect(name)
                if ext in IMAGES and not code:
                    continue                     # логотипи й підписи з листів
                text = "" if ext in IMAGES else _text_of(att)
                if not code:
                    code, why = detect("", text)
                if not code and hint:
                    code, why = "004", "лист ABD"
                if not code:
                    continue                     # звичайні вкладення листів не пропонуються
                mrn_in = _mrn.find_in_text(f"{msg.subject or ''} {text}", country)
                out.append(_row(shipment, parcels, f"msg:{msg.pk}:{i}", name, "mail",
                                msg.sent_at or msg.created_at, code, why, text, mrn_in, sent_to,
                                msg.subject or ""))
                seen_names.add(name)

    folder = order_folder(order)
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            ext = _ext(name)
            if not os.path.isfile(path) or ext not in UPS_FORMATS or _LABEL_FILE.search(name) or name in seen_names:
                continue
            code, why = detect(name)
            text = "" if ext in IMAGES else _text_of({"name": name}, path=path)
            if not code:
                code, why = detect("", text)
            if not code:
                code, why = "008", "тип не визначено"
            date = datetime.fromtimestamp(os.path.getmtime(path), tz=dt_tz.utc)
            mrn_in = _mrn.find_in_text(text, country)
            out.append(_row(shipment, parcels, f"order:{name}", name, "order", date, code, why, text, mrn_in,
                            sent_to, ""))
    order_key = {"004": 0, "002": 1, "010": 2}
    out.sort(key=lambda r: (not r["checked"], order_key.get(r["type"], 9), r["name"].lower()))
    return out


def _row(shipment, parcels, ref, name, source, date, code, why, text, mrn_in, sent_to, subject):
    m = match(shipment, parcels, text, date, mrn_in)
    here_n = next((n for n, p in enumerate(parcels, 1) if p.pk == shipment.pk), None)
    already = sent_to.get(name, [])
    checked = (code == "004" and m["here"] is not False and here_n not in already
               and not (m["here"] is None and m["other"] and already))
    if m["here"] is None and m["other"]:
        checked = False
    return {"ref": ref, "name": name, "source": source, "date": date, "type": code,
            "type_name": TYPE_NAMES.get(code, code), "type_why": why, "here": m["here"], "other": m["other"],
            "match_why": m["why"], "mrn": mrn_in, "sent_to": already, "sent_here": here_n in already,
            "checked": checked, "subject": subject}


def read(shipment, user, ref):
    """(назва, байти) файлу-кандидата або None — лише файли зі списку `candidates` цієї людини."""
    kind, _, rest = (ref or "").partition(":")
    if kind == "order":
        name = os.path.basename(rest)
        if not name or name.startswith(".") or not shipment.order_id:
            return None
        path = os.path.join(order_folder(shipment.order), name)
        if _ext(name) not in UPS_FORMATS or not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return name, fh.read(), ""
    if kind == "msg":
        from email_assistant.attachments import attachment_bytes
        from email_assistant.models import EmailMessage
        pk, _, idx = rest.partition(":")
        if not (pk.isdigit() and idx.isdigit()) or not shipment.order_id:
            return None
        allowed = {m.pk for m in _mrn._order_messages(shipment.order, user)}
        msg = EmailMessage.objects.filter(pk=int(pk)).select_related("account").first()
        if msg is None or msg.pk not in allowed:
            return None
        atts = msg.attachments or []
        if not 0 <= int(idx) < len(atts):
            return None
        att = atts[int(idx)]
        data = attachment_bytes(msg.account, msg, int(idx))
        return (att.get("name") or "document", data, att.get("content_type", "")) if data else None
    return None
