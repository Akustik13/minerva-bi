"""
Запити пропозиції в пошті: «bitte um ein Angebot für 500 Stück ANT-2400» → підказка «📝 запит» і готова чернетка.

Правила, без AI (миттєво й безкоштовно):
- лише вхідний лист від клієнта чи нового контакту (категорії customer / other), не спам і не видалений;
- або лист, який колега переслав (FW / WG): тоді відправник і текст — з пересланого листа (`quotes.forwarded`),
  і відправник має бути клієнтом чи новим контактом (`categorize.address_category`);
- аналізується новий текст листа — без процитованої історії;
- запит: явне прохання про пропозицію / ціну (de, en, uk, ru) або артикули каталогу з кількістю + питання про ціну;
- не запит: подяка за наш Angebot / відповідь на нього, гілка, де Angebot уже створено.
Рішення людини («✕ Не запит», «Angebot створено») автоматика не перебиває.
"""
import logging
import re
import threading
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from email_assistant.quotes import QUOTE_START_RE, forwarded, new_text  # noqa: F401 — текст без цитати, спільний із поштою

logger = logging.getLogger(__name__)

CATEGORIES = ("customer", "other")
# Прохання про пропозицію / ціну
REQUEST_RE = re.compile(
    r"\b(?:bitte|bitten|w[üu]rden|k[öo]nnten|k[öo]nnen|please|pls|kindly|could|can|would|"
    r"прошу|просимо|просим|будь\s+ласка|пожалуйста|можете|могли\s+б)\b[^.\n?!]{0,60}?"
    r"\b(?:angebot|quot(?:e|ation)|offer|proposal|preis\w*|price\w*|pricing|"
    r"пропозиці\w*|предложени\w*|ціну|ціни|цену|цены|вартість|стоимость)"
    r"|\b(?:preis|angebots)anfrage\b|\banfrage\s+(?:f[üu]r|zu|nach|bez[üu]glich|über)\b|^\s*anfrage\b"
    r"|\brfq\b|\brequest\s+for\s+(?:a\s+)?(?:quot\w*|price\w*|offer|proposal)\b"
    r"|\bquotation\s+(?:request|for)\b|\bprice\s+(?:request|inquiry|enquiry)\b|\binquiry\s+for\b"
    r"|комерційн\w*\s+пропозиці\w*|коммерческ\w*\s+предложени\w*"
    r"|\bзапит\s+(?:ціни|на\s+ціну|пропозиції|комерційної)\b|\bзапрос\s+(?:цены|на\s+цену|предложения|кп)\b",
    re.IGNORECASE | re.MULTILINE)
# Питання про ціну — разом з артикулами каталогу й кількостями
PRICE_RE = re.compile(r"\b(?:preis\w*|kost\w*|price\w*|pricing|cost\w*|ціна|ціни|ціну|вартість|скільки\s+коштує|"
                      r"цена|цены|стоимость|сколько\s+стоит)\b", re.IGNORECASE)
# Відповідь на наш Angebot, а не новий запит
THANKS_RE = re.compile(
    r"\b(?:dank\w*|thank\w*|дякую\w*|дякуємо|спасибо|благодар\w*)\b[^.\n]{0,40}\b(?:angebot|quot\w*|offer|пропозиці\w*|"
    r"предложени\w*)"
    r"|\b(?:bezug\w*\s+auf|bezugnehmend\s+auf|bez[üu]glich|regarding|with\s+reference\s+to|щодо|по\s+поводу)\s+"
    r"(?:ihr\w*|unser\w*|your|our|вашої|вашего|нашої|нашего)\s+(?:angebot|quot\w*|offer|пропозиці\w*|предложени\w*)",
    re.IGNORECASE)
SNIPPET = 60
SCAN_BATCH = 200
PROGRESS_EVERY = 25      # листів між оновленнями стану
STALE_MINUTES = 3        # «⏳» без оновлення довше — обробка, мабуть, перервалась (перезапуск сервера)


def _qty(value) -> str:
    text = f"{value:f}"   # 500 → «500», 2.500 → «2.5» (нулі прибираються лише після крапки)
    return text.rstrip("0").rstrip(".") if "." in text else text


def detect(msg):
    """→ {reason, items} якщо лист схожий на запит пропозиції, інакше None."""
    return check(msg)[0]


def check(msg):
    """→ ({reason, items} | None, чому не запит — зрозуміло людині; '' — запит). Для «🔍 Чому лист тут»."""
    from email_assistant.crm_link import is_own, own_addresses

    from .models import Offer
    from .services import detect_items
    if msg.is_spam or msg.is_deleted:
        return None, "лист у спамі або видалений"
    if msg.folder in ("sent", "spam", "trash", "draft"):
        return None, "наш надісланий лист, чернетка або кошик"
    own = own_addresses()
    fw = None
    if msg.category not in CATEGORIES or is_own(msg.from_email, own):
        fw, why = _forwarded(msg, own)   # колега переслав лист клієнта
        if fw is None:
            return None, why
    offer = Offer.objects.filter(source_message__thread_id=msg.thread_id).first() if msg.thread_id else None
    if offer is not None:   # по цій гілці Angebot уже є — далі переговори, а не новий запит
        return None, f"по цій гілці вже є Angebot {offer.display_number} — далі переговори, а не новий запит"
    body = fw["text"] if fw else new_text(msg.body_text)
    subject = (fw["subject"] or msg.subject or "") if fw else (msg.subject or "")
    text = f"{subject}\n{body}"
    request = REQUEST_RE.search(body) or REQUEST_RE.search(subject)   # у підставу — фраза з тексту
    if THANKS_RE.search(text) and not (request and re.search(r"neu\w*|new|нов\w*", request.group(0), re.I)):
        return None, "схоже на подяку за наш Angebot або відповідь на нього"
    items = [{"product_id": p.pk, "sku": p.sku, "qty": _qty(q) if q else None} for p, q in detect_items(text)]
    parts = [f"переслано від {fw['from']}"] if fw else []
    if request:
        parts.append(f"«{' '.join(request.group(0).split())[:SNIPPET]}»")
    elif any(i["qty"] for i in items) and PRICE_RE.search(text):
        parts.append("артикули з кількістю й питання про ціну")
    else:
        return None, ("немає прохання про Angebot / ціну" + (f" у пересланому листі від {fw['from']}" if fw else "")
                      + " (і артикулів каталогу з кількістю разом із питанням про ціну)")
    if items:
        parts.append("позиції: " + ", ".join(f"{i['sku']} × {i['qty']}" if i["qty"] else i["sku"] for i in items[:4]))
    return {"reason": " · ".join(parts)[:255], "items": items[:30]}, ""


def forwarded_request(msg, own=None):
    """Пересланий колегою лист клієнта / нового контакту → {from, name, subject, text}; інакше None."""
    return _forwarded(msg, own)[0]


def _forwarded(msg, own=None):
    """→ (пересланий лист клієнта | None, чому ні)."""
    from email_assistant.categorize import address_category
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.models import EmailMessage
    own = own if own is not None else own_addresses()
    labels = dict(EmailMessage.CATEGORY_CHOICES)
    if not (is_own(msg.from_email, own) or msg.category == "internal"):
        return None, (f"тип співрозмовника — {labels.get(msg.category, msg.category or 'ще не визначено')}: "
                      "запити шукаються лише від клієнтів і нових контактів")
    fw = forwarded(msg.body_text, skip=lambda a: is_own(a, own))   # «WG: FW: …» — до листа ззовні
    if fw is None:
        inner = forwarded(msg.body_text)
        if inner is not None:
            return None, f"переслано лише зі своїх адрес ({inner['from']}) — листа ззовні всередині не знайдено"
        return None, "лист від своєї адреси, і пересланого листа з «Von: / From:» та адресою в ньому не знайдено"
    kind = address_category(msg.account_id, fw["from"])
    if kind not in CATEGORIES:
        return None, f"переслано від {fw['from']} — це {labels.get(kind, kind)}, а не клієнт чи новий контакт"
    return fw, ""


def explain(msg) -> dict:
    """Для «🔍 Чому лист тут»: {state: new | done | dismissed | found | none, reason, request_id, offer}.
    found — лист схожий на запит, але підказки ще немає (перевірка ще не проходила) — «↻ Перевірити ще раз»."""
    from .models import QuoteRequest
    qr = QuoteRequest.objects.filter(message=msg).select_related("offer").first()
    if qr is not None:
        state = {QuoteRequest.NEW: "new", QuoteRequest.DONE: "done", QuoteRequest.DISMISSED: "dismissed"}.get(
            qr.status, "new")
        return {"state": state, "reason": qr.reason, "request_id": qr.pk,
                "offer": qr.offer.display_number if qr.offer_id else ""}
    found, why = check(msg)
    return {"state": "found" if found else "none", "reason": found["reason"] if found else why,
            "request_id": None, "offer": ""}


def scan(msg):
    """Новий лист → QuoteRequest, якщо це запит. Рішення людини не перезаписується. → QuoteRequest | None."""
    from .models import QuoteRequest
    existing = QuoteRequest.objects.filter(message=msg).first()
    if existing is not None:
        return existing
    found = detect(msg)
    if found is None:
        return None
    return QuoteRequest.objects.create(message=msg, thread_id=msg.thread_id, **found)


def scan_recent(days=30, progress=None) -> tuple:
    """Дозаповнити історію: вхідні листи за N днів (None — уся пошта), ще без підказки.
    progress(перевірено, усього, знайдено) — після кожної порції. → (перевірено, знайдено)."""
    from django.db import IntegrityError

    from email_assistant.models import EmailMessage
    qs = (EmailMessage.objects.filter(category__in=CATEGORIES + ("internal",), is_spam=False, is_deleted=False,
                                      quote_request__isnull=True)
          .exclude(folder__in=("sent", "spam", "trash")))
    if days:
        qs = qs.filter(sent_at__gte=timezone.now() - timedelta(days=days))
    ids = list(qs.order_by("sent_at", "pk").values_list("pk", flat=True))
    found = 0
    for start in range(0, len(ids), SCAN_BATCH):
        part = ids[start:start + SCAN_BATCH]
        for n, msg in enumerate(EmailMessage.objects.filter(pk__in=part).select_related("thread")
                                .order_by("sent_at", "pk"), 1):
            try:
                with transaction.atomic():
                    found += scan(msg) is not None
            except IntegrityError:   # той самий лист щойно обробив сигнал або паралельний запуск
                pass
            if progress and n % PROGRESS_EVERY == 0:
                progress(start + n, len(ids), found)
        if progress:
            progress(start + len(part), len(ids), found)
    return len(ids), found


_scan_lock = threading.Lock()


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def start_scan(depth):
    """«Знайти запити в уже отриманій пошті» з налаштувань: у фоні, стан — OfferSettings.quote_scan_status."""
    from .models import OfferSettings
    days = None if depth == OfferSettings.SCAN_ALL else int(depth)
    label = dict(OfferSettings.SCAN_CHOICES).get(depth, f"{depth} дн.")
    caller = threading.current_thread()

    def status(text):
        OfferSettings.objects.filter(pk=1).update(quote_scan_status=text[:255], quote_scan_at=timezone.now())

    def run():
        if not _scan_lock.acquire(blocking=False):
            logger.info("quote request scan already running — %s skipped", label)
            return
        try:
            status(f"⏳ {label}: підготовка…")
            seen, found = scan_recent(days, progress=lambda done, total, n: status(
                f"⏳ {label}: перевірено {done} з {total}, знайдено {n}"))
            status(f"✅ {timezone.localtime():%d.%m.%Y %H:%M} · {label}: перевірено листів {seen}, "
                   f"знайдено нових запитів {found}")
        except Exception as e:  # noqa: BLE001
            logger.exception("quote request scan failed: %s", e)
            status(f"❌ Обробку перервано ({type(e).__name__}) — спробуйте ще раз")
        finally:
            _scan_lock.release()
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(run)


def for_thread(thread):
    """Відкритий запит гілки (найновіший) — для підказки в прев'ю."""
    from .models import QuoteRequest
    if thread is None:
        return None
    return QuoteRequest.objects.filter(thread=thread, status=QuoteRequest.NEW).order_by("-created_at").first()


def mark_done(thread, offer, user=None) -> int:
    """Angebot створено з гілки — запити гілки виконано."""
    from .models import QuoteRequest
    return QuoteRequest.objects.filter(thread=thread, status=QuoteRequest.NEW).update(
        status=QuoteRequest.DONE, offer=offer, decided_by=user)


def dismiss(request_obj, user=None):
    from .models import QuoteRequest
    request_obj.status, request_obj.decided_by = QuoteRequest.DISMISSED, user
    request_obj.save(update_fields=["status", "decided_by"])
    return request_obj


def tags_for_messages(message_ids) -> set:
    """pk листів з відкритим запитом — для мітки «📝 запит» у списку."""
    from .models import QuoteRequest
    return set(QuoteRequest.objects.filter(message_id__in=message_ids, status=QuoteRequest.NEW)
               .values_list("message_id", flat=True))
