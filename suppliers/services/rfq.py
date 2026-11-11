"""
suppliers/services/rfq.py — 📨 Запити постачальникам (RFQ): одна форма → кільком постачальникам.

  форма          — TEMPLATES (плати / складання / кабелі / компоненти / механіка / загальна): параметри → текст листа
                   мовою запиту (build_body), тема з номером запиту «[RFQ-2026-004] …» — за ним Minerva впізнає відповіді;
  отримувачі     — add_recipients: контакт / основний контакт з email / email постачальника; один постачальник —
                   один рядок (UniqueConstraint);
  надсилання     — send: кожному свій лист зі скриньки людини (SMTPClient, вкладення — файли запиту), копія листа
                   в Minerva й гілка (як «Написати»), проєкт запиту → гілка в проєкті; стан кожного отримувача;
                   повторне натискання не шле вдруге (атомарний UPDATE зі стану «ще не надіслано / не надіслано»);
  відповіді      — on_incoming (сигнал нового листа): та сама гілка, або номер запиту в темі й відправник —
                   цей постачальник → «📥 Відповів», запит → «Є відповіді»;
  пропозиції     — set_quote (вручну), apply_ai (з AI, suppliers/services/ai.quote), comparison (ціна в валюті
                   Minerva, строк, оцінка постачальника), choose, close, remind (нагадування в тій самій гілці),
                   make_purchase (чернетка закупівлі для вибраного).
"""
import datetime
import hashlib
import logging
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

ATTACH_MAX_MB = 20
CODE_RE = re.compile(r"\bRFQ-\d{4}-\d{3,}\b")

# ── Форми: ключ → назва, поля (ключ, підпис uk, підпис en, підпис de, підказка) ──
TEMPLATES = {
    "generic": ("📝 Загальний запит", [
        ("item", "Що потрібно", "Item", "Artikel", "напр. Корпус алюмінієвий 120×80×40"),
        ("spec", "Вимоги / опис", "Requirements", "Anforderungen", "матеріал, розміри, стандарти…"),
    ]),
    "pcb": ("🟩 Друковані плати (PCB)", [
        ("layers", "Шарів", "Layers", "Lagen", "4"),
        ("size", "Розмір плати, мм", "Board size, mm", "Leiterplattengröße, mm", "100 × 80"),
        ("material", "Матеріал", "Material", "Material", "FR-4 Tg170 / Rogers 4350B"),
        ("thickness", "Товщина, мм", "Thickness, mm", "Dicke, mm", "1.6"),
        ("copper", "Мідь зовн. / внутр.", "Copper outer / inner", "Kupfer außen / innen", "1 oz / 0.5 oz"),
        ("finish", "Покриття", "Surface finish", "Oberfläche", "ENIG"),
        ("mask", "Маска / маркування", "Solder mask / silkscreen", "Lötstopplack / Bestückungsdruck", "green / white"),
        ("track", "Мін. доріжка / зазор", "Min. track / space", "Min. Leiterbahn / Abstand", "0.1 / 0.1 mm"),
        ("drill", "Мін. отвір, переходи", "Min. drill, vias", "Min. Bohrung, Vias", "0.2 mm, tented / filled"),
        ("impedance", "Контроль імпедансу", "Impedance control", "Impedanzkontrolle", "50 Ω ±10 %"),
        ("stackup", "Стекап", "Stack-up", "Lagenaufbau", "L1 sig / L2 GND / L3 PWR / L4 sig, prepreg 0.2"),
        ("panel", "Панелізація", "Panelization", "Nutzen", "single / panel 2×3, V-score"),
        ("testing", "Тест / клас", "Testing / class", "Prüfung / Klasse", "E-test 100 %, IPC Class 2"),
    ]),
    "pcba": ("🔧 Складання плат (PCBA)", [
        ("boards", "Плати", "Bare boards", "Leiterplatten", "наші / ваші (turnkey)"),
        ("sides", "Сторони монтажу", "Assembly sides", "Bestückungsseiten", "top / both"),
        ("parts", "Компонентів (SMD / THT)", "Parts (SMD / THT)", "Bauteile (SMD / THT)", "120 SMD, 6 THT"),
        ("bom", "BOM: рядків, хто закуповує", "BOM: lines, sourcing", "Stückliste: Positionen, Beschaffung",
         "45 lines, turnkey / consigned"),
        ("fine", "Дрібний крок / BGA", "Fine pitch / BGA", "Fine-Pitch / BGA", "0.4 mm QFN, 1 BGA"),
        ("test", "Тестування", "Testing", "Prüfung", "AOI, X-ray, functional test"),
        ("coating", "Лак / пакування", "Coating / packing", "Schutzlack / Verpackung", "no / ESD bags"),
    ]),
    "cable": ("🔌 Кабелі й збірки", [
        ("cable", "Кабель", "Cable type", "Kabeltyp", "RG316 / LMR-200"),
        ("length", "Довжина", "Length", "Länge", "500 mm ±5"),
        ("conn_a", "Роз'єм A", "Connector A", "Stecker A", "SMA male straight"),
        ("conn_b", "Роз'єм B", "Connector B", "Stecker B", "U.FL"),
        ("electrical", "Електричні вимоги", "Electrical", "Elektrisch", "50 Ω, VSWR < 1.3 до 6 GHz"),
        ("marking", "Маркування / пакування", "Marking / packing", "Kennzeichnung / Verpackung", "label with P/N"),
    ]),
    "components": ("🔩 Компоненти", [
        ("parts", "Перелік (артикул виробника × кількість)", "Part list (MPN × qty)", "Teileliste (MPN × Menge)",
         "STM32F405RGT6 × 100\nLM2596S-5.0 × 200"),
        ("alternatives", "Аналоги", "Alternatives", "Alternativen", "дозволено / лише оригінал"),
        ("datecode", "Date code / пакування", "Date code / packing", "Date Code / Verpackung", "≤ 2 роки, reel"),
    ]),
    "mechanical": ("⚙️ Механіка", [
        ("part", "Деталь", "Part", "Teil", "корпус / кронштейн"),
        ("material", "Матеріал", "Material", "Material", "AlMg3 / PA66"),
        ("process", "Технологія", "Process", "Verfahren", "CNC / лиття / листовий метал"),
        ("finish", "Покриття", "Finish", "Oberfläche", "анодування чорне"),
        ("tolerance", "Допуски", "Tolerances", "Toleranzen", "ISO 2768-m"),
    ]),
}
TEMPLATE_CHOICES = [(k, v[0]) for k, v in TEMPLATES.items()]

TEXTS = {
    "en": {"subject": "Request for quotation", "hello": "Dear {name},", "hello_all": "Dear Sir or Madam,",
           "intro": "We would like to receive your quotation for the following:", "qty": "Quantities",
           "qty_ask": "Please quote unit price and total for each quantity.",
           "files": "Attached files", "ask": "Please include: unit price per quantity, lead time, shipping cost, "
                                             "payment terms and validity of the offer.",
           "by": "We would appreciate your reply by {date}.", "thanks": "Thank you in advance.",
           "bye": "Best regards,", "ref": "Our reference", "remind": "Kind reminder regarding our request below "
                                                                     "({code}). Could you please let us know if you "
                                                                     "can quote and by when?"},
    "de": {"subject": "Angebotsanfrage", "hello": "Sehr geehrte(r) {name},", "hello_all": "Sehr geehrte Damen und Herren,",
           "intro": "wir bitten um Ihr Angebot für Folgendes:", "qty": "Mengen",
           "qty_ask": "Bitte geben Sie Stückpreis und Gesamtpreis je Menge an.",
           "files": "Anhänge", "ask": "Bitte nennen Sie: Stückpreis je Menge, Lieferzeit, Versandkosten, "
                                      "Zahlungsbedingungen und Gültigkeit des Angebots.",
           "by": "Wir freuen uns über Ihre Antwort bis {date}.", "thanks": "Vielen Dank im Voraus.",
           "bye": "Mit freundlichen Grüßen", "ref": "Unsere Referenz", "remind": "Freundliche Erinnerung an unsere "
                                                                                 "Anfrage unten ({code}). Können Sie "
                                                                                 "uns mitteilen, ob und bis wann Sie "
                                                                                 "anbieten können?"},
    "uk": {"subject": "Запит пропозиції", "hello": "Доброго дня, {name}!", "hello_all": "Доброго дня!",
           "intro": "Просимо надати пропозицію на таке:", "qty": "Кількості",
           "qty_ask": "Вкажіть, будь ласка, ціну за одиницю й суму для кожної кількості.",
           "files": "Файли у вкладенні", "ask": "Просимо вказати: ціну за кількістю, строк виготовлення, вартість "
                                                "доставки, умови оплати й строк дії пропозиції.",
           "by": "Будемо вдячні за відповідь до {date}.", "thanks": "Дякуємо!", "bye": "З повагою,",
           "ref": "Наш номер", "remind": "Нагадуємо про наш запит нижче ({code}). Підкажіть, будь ласка, чи зможете "
                                         "надати пропозицію і коли?"},
}


class RFQError(Exception):
    pass


def fields_of(template) -> list:
    return TEMPLATES.get(template, TEMPLATES["generic"])[1]


def parse_quantities(text) -> list:
    out = []
    for part in re.split(r"[;,/\s]+", str(text or "")):
        part = part.replace(".", "").strip()
        if part.isdigit() and int(part) > 0 and int(part) not in out:
            out.append(int(part))
    return out[:8]


def subject_for(req) -> str:
    t = TEXTS.get(req.language, TEXTS["en"])
    return f"[{req.code}] {t['subject']}: {req.title}"[:250]


def build_body(req) -> str:
    """Текст запиту мовою запиту (без привітання — воно своє кожному отримувачу, greeting)."""
    t = TEXTS.get(req.language, TEXTS["en"])
    col = {"uk": 1, "en": 2, "de": 3}.get(req.language, 2)
    lines = [t["intro"], "", req.title, ""]
    for f in fields_of(req.template):
        value = str((req.specs or {}).get(f[0]) or "").strip()
        if value:
            label = f[col]
            if "\n" in value:
                lines += [f"{label}:"] + [f"  {ln}" for ln in value.splitlines() if ln.strip()]
            else:
                lines.append(f"{label}: {value}")
    extra = str((req.specs or {}).get("_notes") or "").strip()
    if extra:
        lines += ["", extra]
    qty = parse_quantities(req.quantities)
    if qty:
        lines += ["", f"{t['qty']}: " + " / ".join(f"{q:,}".replace(",", " ") for q in qty), t["qty_ask"]]
    names = [f.name for f in req.files.all()] if req.pk else []
    if names:
        lines += ["", f"{t['files']}:"] + [f"  - {n}" for n in names]
    lines += ["", t["ask"]]
    if req.reply_by:
        lines.append(t["by"].format(date=req.reply_by.strftime("%d.%m.%Y")))
    lines += ["", f"{t['ref']}: {req.code}", "", t["thanks"]]
    return "\n".join(lines).strip()


def greeting(req, recipient) -> str:
    t = TEXTS.get(req.language, TEXTS["en"])
    name = recipient.contact.name if recipient.contact_id and recipient.contact.name else ""
    return t["hello"].format(name=name) if name else t["hello_all"]


def pick_email(supplier, contact=None):
    """(email, контакт) для запиту: вибраний контакт → основний контакт з email → email постачальника."""
    if contact is not None and contact.email:
        return contact.email, contact
    c = supplier.contacts.filter(is_active=True).exclude(email="").order_by("-is_primary", "pk").first()
    if c is not None:
        return c.email, c
    return (supplier.email or ""), None


def add_recipients(req, suppliers, contacts=None) -> dict:
    """suppliers — постачальники; contacts — {supplier_pk: контакт}. → {added, skipped: [назва без email]}."""
    from suppliers.models import SupplierRequestRecipient as R
    out = {"added": 0, "skipped": []}
    contacts = contacts or {}
    for s in suppliers:
        email, contact = pick_email(s, contacts.get(s.pk))
        if not email:
            out["skipped"].append(s.name)
            continue
        _r, created = R.objects.get_or_create(request=req, supplier=s, defaults={"email": email, "contact": contact})
        out["added"] += created
    return out


def _attachments(req) -> list:
    out, total = [], 0
    for f in req.files.all():
        try:
            with f.file.open("rb") as fh:
                data = fh.read()
        except (OSError, ValueError) as e:
            raise RFQError(f"Файл «{f.name}» недоступний: {e}")
        total += len(data)
        import mimetypes
        out.append({"name": f.name, "content": data,
                    "content_type": mimetypes.guess_type(f.name)[0] or "application/octet-stream"})
    if total > ATTACH_MAX_MB * 1024 * 1024:
        raise RFQError(f"Файли разом понад {ATTACH_MAX_MB} МБ — більшість пошт не прийме. Стисніть у ZIP або "
                       f"дайте посилання в тексті.")
    return out


def store_sent(account, result, to, cc, subject, body, attachments, reply_to=None):
    """Копія надісланого листа в Minerva й гілка (так само, як лист з «Написати»), щоб відповідь лягла поруч."""
    from email_assistant.models import EmailMessage
    from email_assistant.sync import get_or_create_thread
    now = timezone.now()
    thread = reply_to.thread if reply_to is not None and reply_to.thread_id else get_or_create_thread(account, {
        "subject": subject, "from_email": account.email_address, "to_emails": to, "cc_emails": cc, "sent_at": now,
        "message_id": result.get("message_id", ""), "in_reply_to": reply_to.message_id if reply_to else ""})
    msg = EmailMessage.objects.create(
        account=account, thread=thread, folder="sent",
        imap_folder_name=("minerva:" + result.get("message_id", ""))[:200],
        in_reply_to=(reply_to.message_id if reply_to else "")[:500], message_id=result.get("message_id", ""),
        subject=subject, from_email=account.email_address, from_name=account.display_name, to_emails=to,
        cc_emails=cc, body_text=body, is_read=True, sent_at=now,
        attachments=[{"name": a["name"], "content_type": a["content_type"], "size": len(a["content"])}
                     for a in attachments])
    thread.message_count = thread.messages.count()
    thread.last_message_at = now
    thread.save(update_fields=["message_count", "last_message_at"])
    return msg


def account_for(user, req=None):
    from email_assistant.models import EmailAccount
    qs = EmailAccount.objects.filter(user=user, is_active=True)
    if req is not None and req.account_id:
        acc = qs.filter(pk=req.account_id).first()
        if acc is not None:
            return acc
    return qs.order_by("-is_primary", "pk").first()


def send(req, user, only=None) -> dict:
    """Надіслати запит отримувачам у стані «ще не надіслано» / «не надіслано» (only — лише ці pk).
    → {sent, failed: [(назва, помилка)]}."""
    from email_assistant.smtp_client import SMTPClient
    from suppliers.models import SupplierEvent, SupplierRequest, SupplierRequestRecipient as R
    account = account_for(user, req)
    if account is None:
        raise RFQError("У вас немає активної поштової скриньки в Minerva — запит нема з чого надіслати.")
    if not req.body.strip():
        raise RFQError("Порожній текст листа.")
    attachments = _attachments(req)
    subject = req.subject or subject_for(req)
    qs = req.recipients.filter(state__in=[R.PLANNED, R.FAILED]).select_related("supplier", "contact")
    if only:
        qs = qs.filter(pk__in=list(only))
    out = {"sent": 0, "failed": []}
    project_threads = []
    for r in qs:
        # бронь: лише один процес шле цьому отримувачу (подвійне натискання / дві вкладки)
        if not R.objects.filter(pk=r.pk, state__in=[R.PLANNED, R.FAILED]).update(
                state=R.SENT, sent_at=timezone.now(), error=""):
            continue
        cc = [a.strip() for a in (r.cc or "").split(",") if "@" in a]
        body = f"{greeting(req, r)}\n\n{req.body.strip()}"
        try:
            result = SMTPClient(account).send(to_emails=[r.email], subject=subject, body_text=body, cc_emails=cc,
                                              attachments=attachments)
        except Exception as e:  # noqa: BLE001 — не впустити бронь
            result = {"ok": False, "error": str(e)}
        if not result.get("ok"):
            err = str(result.get("error") or "помилка SMTP")[:500]
            R.objects.filter(pk=r.pk).update(state=R.FAILED, error=err, sent_at=None)
            out["failed"].append((r.supplier.name, err))
            continue
        sent = None
        try:
            sent = store_sent(account, result, [r.email], cc, subject, body, attachments)
        except Exception:  # noqa: BLE001 — лист уже пішов
            logger.warning("rfq: store sent copy failed", exc_info=True)
        R.objects.filter(pk=r.pk).update(message_id=result.get("message_id", "")[:500], sent_message=sent,
                                         thread=sent.thread if sent else None)
        if sent is not None and sent.thread_id:
            project_threads.append(sent.thread)
        SupplierEvent.objects.create(supplier=r.supplier, kind=SupplierEvent.REQUEST, request=req, message=sent,
                                     user=user, text=f"📨 Запит {req.code} «{req.title[:120]}» → {r.email}")
        out["sent"] += 1
    if out["sent"]:
        SupplierRequest.objects.filter(pk=req.pk, status=SupplierRequest.DRAFT).update(
            status=SupplierRequest.SENT, sent_at=timezone.now(), account=account, subject=subject)
        SupplierRequest.objects.filter(pk=req.pk).update(account=account, subject=subject)
    if req.project_id and project_threads:
        try:
            from projects.services import link_thread
            for th in project_threads:
                link_thread(req.project, th, user=user, reason=f"Запит постачальникам {req.code}")
        except Exception:  # noqa: BLE001 — проєкт не головне
            logger.warning("rfq: link thread to project failed", exc_info=True)
    return out


def on_incoming(msg) -> int:
    """Новий лист: чи це відповідь на запит. → скільки отримувачів позначено «📥 Відповів»."""
    from email_assistant.crm_link import is_own, own_addresses
    from suppliers.models import SupplierEvent, SupplierRequest, SupplierRequestRecipient as R
    from suppliers.services import match
    if msg.folder in (msg.FOLDER_SENT, msg.FOLDER_DRAFT, msg.FOLDER_SPAM) or msg.is_spam:
        return 0
    if is_own(msg.from_email, own_addresses()):
        return 0
    cond = Q(pk__in=[])
    if msg.thread_id:
        cond |= Q(thread_id=msg.thread_id)
    if msg.in_reply_to:
        cond |= Q(message_id=msg.in_reply_to.strip())
    codes = CODE_RE.findall(f"{msg.subject or ''} {(msg.body_text or '')[:4000]}")
    if codes:
        cond |= Q(request__code__in=codes)
    found = 0
    sender = match.addr(msg.from_email)
    for r in R.objects.filter(cond).exclude(state=R.PLANNED).select_related("supplier", "request"):
        if not (r.thread_id and r.thread_id == msg.thread_id) and (not msg.in_reply_to or
                                                                  msg.in_reply_to.strip() != r.message_id):
            emails, domains = match.keys(r.supplier)   # лише за номером у темі — відправник має бути цей постачальник
            if sender not in emails and sender != r.email.lower() and not match.domain_matches(
                    sender.split("@", 1)[1] if sender else "", domains):
                continue
        updates = {"reply_message": msg} if r.reply_message_id is None else {}
        if r.state in (R.SENT, R.FAILED, R.SILENT):
            updates.update(state=R.REPLIED, replied_at=msg.sent_at or timezone.now())
        if not updates:
            continue
        R.objects.filter(pk=r.pk).update(**updates)
        SupplierRequest.objects.filter(pk=r.request_id, status=SupplierRequest.SENT).update(
            status=SupplierRequest.ANSWERED)
        SupplierEvent.objects.create(supplier=r.supplier, kind=SupplierEvent.REQUEST, request=r.request, message=msg,
                                     text=f"📥 Відповідь на запит {r.request.code}: «{(msg.subject or '')[:120]}»")
        found += 1
    return found


def remind(recipient, user) -> dict:
    """⏰ Нагадування в тій самій гілці (Re: …)."""
    from email_assistant.smtp_client import SMTPClient
    from suppliers.models import SupplierRequestRecipient as R
    req = recipient.request
    account = account_for(user, req)
    if account is None:
        raise RFQError("Немає поштової скриньки.")
    if recipient.state not in (R.SENT, R.SILENT):
        raise RFQError("Нагадувати можна лише тим, хто ще не відповів.")
    t = TEXTS.get(req.language, TEXTS["en"])
    prev = recipient.sent_message
    subject = req.subject or subject_for(req)
    subject = subject if subject.lower().startswith(("re:", "aw:")) else f"Re: {subject}"
    quote = "\n".join("> " + ln for ln in ((prev.body_text if prev else req.body) or "").splitlines()[:60])
    body = f"{greeting(req, recipient)}\n\n{t['remind'].format(code=req.code)}\n\n{t['bye']}\n\n{quote}"
    result = SMTPClient(account).send(to_emails=[recipient.email], subject=subject, body_text=body,
                                      reply_to_message=prev)
    if not result.get("ok"):
        raise RFQError(str(result.get("error") or "помилка SMTP"))
    try:
        store_sent(account, result, [recipient.email], [], subject, body, [], reply_to=prev)
    except Exception:  # noqa: BLE001
        logger.warning("rfq: store reminder failed", exc_info=True)
    R.objects.filter(pk=recipient.pk).update(reminded_at=timezone.now(), state=R.SENT)
    return result


def _dec(value, places="0.0001"):
    value = str(value or "").replace(" ", "").replace(",", ".").strip()
    if not value:
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise RFQError(f"Не число: {value}")
    if d < 0:
        raise RFQError("Ціна не може бути від'ємною.")
    return d.quantize(Decimal(places))


def set_quote(recipient, data: dict):
    """Пропозиція вручну: ціна, сума, валюта, строк, доставка, діє до, умови, стан."""
    from suppliers.models import SupplierRequestRecipient as R
    recipient.price = _dec(data.get("price"))
    recipient.total = _dec(data.get("total"), "0.01")
    recipient.shipping = _dec(data.get("shipping"), "0.01")
    recipient.currency = str(data.get("currency") or "").strip().upper()[:3]
    lead = str(data.get("lead_days") or "").strip()
    recipient.lead_days = int(lead) if lead.isdigit() else None
    valid = str(data.get("valid_until") or "").strip()
    try:
        recipient.valid_until = datetime.date.fromisoformat(valid) if valid else None
    except ValueError:
        raise RFQError("Дата «діє до» — у форматі РРРР-ММ-ДД.")
    recipient.quote_note = str(data.get("quote_note") or "")[:4000]
    state = data.get("state")
    if state in dict(R.STATES) and state != R.PLANNED:
        recipient.state = state
    elif recipient.price is not None or recipient.total is not None:
        recipient.state = R.QUOTED
    recipient.save()
    _touch_answered(recipient.request)


def apply_ai(recipient, data: dict, overwrite=False):
    """Результат ai.quote → поля пропозиції (лише порожні, якщо не overwrite) + стан."""
    from suppliers.models import SupplierRequestRecipient as R
    recipient.ai = data
    tiers = data.get("tiers") or []
    if tiers and (overwrite or not recipient.tiers):
        recipient.tiers = tiers
    qty = parse_quantities(recipient.request.quantities)
    first = next((t for t in tiers if qty and t["qty"] == qty[0]), tiers[0] if tiers else None)
    if first and (overwrite or recipient.price is None):
        recipient.price = Decimal(first["unit_price"])
        recipient.total = Decimal(first["total"])
    if data.get("currency") and (overwrite or not recipient.currency):
        recipient.currency = data["currency"]
    if data.get("lead_days") is not None and (overwrite or recipient.lead_days is None):
        recipient.lead_days = data["lead_days"]
    if data.get("shipping") is not None and (overwrite or recipient.shipping is None):
        recipient.shipping = Decimal(str(data["shipping"])).quantize(Decimal("0.01"))
    if data.get("valid_until") and (overwrite or recipient.valid_until is None):
        try:
            recipient.valid_until = datetime.date.fromisoformat(data["valid_until"])
        except ValueError:
            pass
    if data.get("conditions") and (overwrite or not recipient.quote_note):
        recipient.quote_note = data["conditions"]
    if data.get("declined"):
        recipient.state = R.DECLINED
    elif tiers or recipient.price is not None:
        recipient.state = R.QUOTED
    recipient.save()
    _touch_answered(recipient.request)


def _touch_answered(req):
    from suppliers.models import SupplierRequest
    SupplierRequest.objects.filter(pk=req.pk, status=SupplierRequest.SENT).update(status=SupplierRequest.ANSWERED)


def comparison(req) -> list:
    """Рядки порівняння: ціна за шт. і сума у валюті Minerva (за «Курсами валют»), строк, оцінка постачальника.
    Найдешевший і найшвидший позначено."""
    from sales.revenue import Rates, home_currency
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services.overview import purchases, requests_stats, score
    home = home_currency()
    rates = Rates(home)
    rows = []
    for r in req.recipients.select_related("supplier", "contact", "reply_message").order_by("pk"):
        rate = rates.rate(r.currency or home)
        price_home = (r.price * rate).quantize(Decimal("0.01")) if r.price is not None and rate is not None else None
        total = r.total if r.total is not None else None
        if total is None and r.price is not None:
            q = parse_quantities(req.quantities)
            total = (r.price * q[0]).quantize(Decimal("0.01")) if q else None
        total_home = (total * rate).quantize(Decimal("0.01")) if total is not None and rate is not None else None
        ship_home = (r.shipping * rate).quantize(Decimal("0.01")) if r.shipping is not None and rate is not None else None
        try:
            sc = score(purchases(r.supplier), None, requests_stats(r.supplier)).get("value")
        except Exception:  # noqa: BLE001
            sc = None
        rows.append({"recipient": r, "home": home, "price_home": price_home, "total_home": total_home,
                     "price_text": price_text(r.price),
                     "all_in": (total_home + ship_home) if total_home is not None and ship_home is not None
                     else total_home, "no_rate": rate is None and bool(r.currency), "score": sc})
    priced = [x for x in rows if x["all_in"] is not None and x["recipient"].state == R.QUOTED]
    if priced:
        cheapest = min(priced, key=lambda x: x["all_in"])
        cheapest["cheapest"] = True
    timed = [x for x in rows if x["recipient"].lead_days is not None and x["recipient"].state == R.QUOTED]
    if timed:
        min(timed, key=lambda x: x["recipient"].lead_days)["fastest"] = True
    return rows


def price_text(d) -> str:
    """7.9000 → «7.90», 0.0123 → «0.0123»: щонайменше 2 знаки, зайві нулі прибрано."""
    if d is None:
        return ""
    d = d.normalize()
    return format(d.quantize(Decimal("0.01")) if d.as_tuple().exponent > -2 else d, "f")


def choose(recipient, user=None):
    from suppliers.models import SupplierEvent
    recipient.request.recipients.exclude(pk=recipient.pk).update(chosen=False)
    type(recipient).objects.filter(pk=recipient.pk).update(chosen=True)
    SupplierEvent.objects.create(supplier=recipient.supplier, kind=SupplierEvent.REQUEST, request=recipient.request,
                                 user=user, text=f"🏆 Вибрано в запиті {recipient.request.code}")


def close(req, decision="", user=None):
    """✅ Запит вирішено: хто не відповів — «🔕 Не відповів»."""
    from suppliers.models import SupplierRequest, SupplierRequestRecipient as R
    req.recipients.filter(state=R.SENT).update(state=R.SILENT)
    SupplierRequest.objects.filter(pk=req.pk).update(status=SupplierRequest.CLOSED, closed_at=timezone.now(),
                                                     decision=(decision or req.decision)[:4000])


def make_purchase(recipient, user=None):
    """🛒 Чернетка закупівлі у вибраного постачальника: одна позиція — назва запиту, перша кількість, ціна."""
    from inventory.models import PurchaseOrder, PurchaseOrderLine
    from suppliers.models import SupplierEvent
    req = recipient.request
    qty = parse_quantities(req.quantities)
    with transaction.atomic():
        po = PurchaseOrder.objects.create(supplier=recipient.supplier, status=PurchaseOrder.Status.DRAFT,
                                          currency=recipient.currency or recipient.supplier.currency or "EUR",
                                          notes=f"З запиту {req.code}: {req.title}"[:2000])
        PurchaseOrderLine.objects.create(purchase_order=po, description=req.title[:255],
                                         qty_ordered=Decimal(qty[0] if qty else 1),
                                         unit_price=recipient.price.quantize(Decimal("0.01"))
                                         if recipient.price is not None else None,
                                         currency=recipient.currency or po.currency)
    if req.project_id:
        try:
            from projects.services import link_object
            link_object(req.project, user=user, reason=f"З запиту {req.code}", purchase_order=po)
        except Exception:  # noqa: BLE001
            logger.warning("rfq: link PO to project failed", exc_info=True)
    SupplierEvent.objects.create(supplier=recipient.supplier, kind=SupplierEvent.REQUEST, request=req, user=user,
                                 text=f"🛒 Чернетка закупівлі {po.code} з запиту {req.code}")
    return po


def add_file(req, upload, user=None):
    from suppliers.models import SupplierRequestFile
    from inventory.services.po_documents import safe_name
    data = upload.read()
    if not data:
        raise RFQError("Порожній файл.")
    if len(data) > ATTACH_MAX_MB * 1024 * 1024:
        raise RFQError(f"Файл понад {ATTACH_MAX_MB} МБ.")
    upload.seek(0)
    name = safe_name(upload.name)[:255]
    f = SupplierRequestFile(request=req, name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                            uploaded_by=user)
    f.file.save(name, upload, save=False)
    f.save()
    return f


def waiting_overdue(req) -> list:
    from suppliers.models import SupplierRequestRecipient as R
    if not req.reply_by or req.reply_by >= timezone.localdate():
        return []
    return list(req.recipients.filter(state=R.SENT).select_related("supplier"))
