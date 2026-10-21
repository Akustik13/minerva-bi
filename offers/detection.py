"""
Запити пропозиції в пошті: «bitte um ein Angebot für 500 Stück ANT-2400» → підказка «📝 запит» і готова чернетка.

Правила, без AI (миттєво й безкоштовно):
- лише вхідний лист від клієнта чи нового контакту (категорії customer / other), не спам і не видалений;
- аналізується новий текст листа — без процитованої історії;
- запит: явне прохання про пропозицію / ціну (de, en, uk, ru) або артикули каталогу з кількістю + питання про ціну;
- не запит: подяка за наш Angebot / відповідь на нього, гілка, де Angebot уже створено.
Рішення людини («✕ Не запит», «Angebot створено») автоматика не перебиває.
"""
import logging
import re
from datetime import timedelta

from django.utils import timezone

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
# Початок процитованої історії
QUOTE_START_RE = re.compile(
    r"^\s*(?:>|-{2,}\s*(?:original|ursprüngliche|forwarded|weitergeleitete)|(?:on|am)\s.{5,80}(?:wrote|schrieb)\s*:"
    r"|von:\s|from:\s|gesendet:\s|sent:\s|від:\s|от:\s)", re.IGNORECASE)
SNIPPET = 60


def _qty(value) -> str:
    text = f"{value:f}"   # 500 → «500», 2.500 → «2.5» (нулі прибираються лише після крапки)
    return text.rstrip("0").rstrip(".") if "." in text else text


def new_text(body) -> str:
    """Текст листа без процитованої історії (відповідь клієнта зверху, далі — наш попередній лист)."""
    out = []
    for line in (body or "").splitlines():
        if QUOTE_START_RE.match(line):
            break
        out.append(line)
    return "\n".join(out)[:20000]


def detect(msg):
    """→ {reason, items} якщо лист схожий на запит пропозиції, інакше None."""
    from email_assistant.crm_link import is_own, own_addresses

    from .models import Offer
    from .services import detect_items
    if msg.is_spam or msg.is_deleted or msg.folder in ("sent", "spam", "trash", "draft") \
            or msg.category not in CATEGORIES or is_own(msg.from_email, own_addresses()):
        return None
    if msg.thread_id and Offer.objects.filter(source_message__thread_id=msg.thread_id).exists():
        return None   # по цій гілці Angebot уже є — далі переговори, а не новий запит
    body = new_text(msg.body_text)
    text = f"{msg.subject or ''}\n{body}"
    request = REQUEST_RE.search(body) or REQUEST_RE.search(msg.subject or "")   # у підставу — фраза з тексту
    if THANKS_RE.search(text) and not (request and re.search(r"neu\w*|new|нов\w*", request.group(0), re.I)):
        return None
    items = [{"product_id": p.pk, "sku": p.sku, "qty": _qty(q) if q else None} for p, q in detect_items(text)]
    parts = []
    if request:
        parts.append(f"«{' '.join(request.group(0).split())[:SNIPPET]}»")
    elif any(i["qty"] for i in items) and PRICE_RE.search(text):
        parts.append("артикули з кількістю й питання про ціну")
    else:
        return None
    if items:
        parts.append("позиції: " + ", ".join(f"{i['sku']} × {i['qty']}" if i["qty"] else i["sku"] for i in items[:4]))
    return {"reason": " · ".join(parts)[:255], "items": items[:30]}


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


def scan_recent(days=30) -> int:
    """Дозаповнити історію: вхідні листи за N днів без рішення."""
    from email_assistant.models import EmailMessage
    since = timezone.now() - timedelta(days=days)
    n = 0
    for msg in (EmailMessage.objects.filter(sent_at__gte=since, category__in=CATEGORIES, is_spam=False,
                                            is_deleted=False, quote_request__isnull=True)
                .exclude(folder__in=("sent", "spam", "trash")).select_related("thread").order_by("sent_at")):
        n += scan(msg) is not None
    return n


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
