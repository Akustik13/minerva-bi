"""
email_assistant/categorize.py — автоматична категорія листа для фільтра в скриньці.

Порядок (перша, що підходить):
  🏢 internal   — усі учасники з власних адрес/домену
  👤 customer   — співрозмовник є в CRM (Customer.email) або гілку прив'язано до клієнта
  🏭 supplier   — співрозмовник є серед постачальників (Supplier.email або його домен)
  📦 service    — перевізники, маркетплейси, митниця, банки/платежі, no-reply-розсилки сервісів
  📰 newsletter — розсилки (List-Unsubscribe або «unsubscribe/abmelden» у тексті)
  ❓ other
"""
import re

from email_assistant.crm_link import FREE_MAIL_DOMAINS, _addr, is_own, own_addresses

SERVICE_DOMAIN_PARTS = (
    'ups.com', 'dhl.', 'deutschepost', 'fedex', 'tnt.com', 'gls-', 'dpd.', 'hermes', 'jumingo',
    'digikey', 'mouser', 'zoll.de', 'atlas', 'paypal', 'stripe.com', 'klarna', 'amazon.', 'ebay.',
    'ionos', 'google.com', 'microsoft.com', 'apple.com', 'github.com', 'synology', 'lexoffice',
    'sevdesk', 'datev', 'telekom', 'vodafone', 'sparkasse', 'commerzbank', 'deutsche-bank',
    'wise.com', 'revolut', 'linkedin.com', 'xing.com',
)
NOREPLY_RE = re.compile(
    r'^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon|postmaster|'
    r'bounces?|automated|auto-?confirm|billing|invoice|rechnung)\b', re.I)
NEWSLETTER_RE = re.compile(
    r'unsubscribe|abmelden|abbestellen|newsletter|відписатися|отписаться|manage (your )?(email )?preferences',
    re.I)


class CategoryContext:
    """Довідники для класифікації; для пакетної обробки створюється один раз."""

    def __init__(self, preload_customers=False):
        from inventory.models import Supplier
        self.own = own_addresses()
        emails = {_addr(e) for e in Supplier.objects.exclude(email='').values_list('email', flat=True)}
        self.supplier_emails = {e for e in emails if e}
        self.supplier_domains = {e.split('@', 1)[1] for e in self.supplier_emails} - FREE_MAIL_DOMAINS
        self.customer_emails = None
        if preload_customers:
            from crm.models import Customer
            self.customer_emails = {
                e.lower() for e in Customer.objects.exclude(email='').values_list('email', flat=True)}

    def is_customer(self, addresses) -> bool:
        if not addresses:
            return False
        if self.customer_emails is not None:
            return any(a in self.customer_emails for a in addresses)
        from django.db.models import Q
        from crm.models import Customer
        cond = Q()
        for a in addresses:
            cond |= Q(email__iexact=a)
        return Customer.objects.filter(cond).exists()

    def is_supplier(self, addresses) -> bool:
        return any(a in self.supplier_emails or a.split('@', 1)[1] in self.supplier_domains
                   for a in addresses)


def _is_service(address: str) -> bool:
    local, _, domain = address.partition('@')
    return any(part in domain for part in SERVICE_DOMAIN_PARTS) or bool(NOREPLY_RE.match(local))


def categorize(em, ctx=None, list_unsubscribe=False) -> str:
    ctx = ctx or CategoryContext()
    participants = []
    for a in [em.from_email] + list(em.to_emails or []) + list(em.cc_emails or []):
        a = _addr(a)
        if a and a not in participants:
            participants.append(a)
    external = [a for a in participants if not is_own(a, ctx.own)]
    if not external:
        return 'internal'

    # Співрозмовник: для вхідних — відправник, для своїх листів — отримувачі
    sender = _addr(em.from_email)
    counterparts = [sender] if sender in external else external

    if ctx.is_customer(external) or (em.thread_id and em.thread.crm_customer_id):
        return 'customer'
    if ctx.is_supplier(external):
        return 'supplier'
    if any(_is_service(a) for a in counterparts):
        return 'service'
    text = (em.body_text or '')[-4000:] + ' ' + (em.subject or '')
    if list_unsubscribe or NEWSLETTER_RE.search(text):
        return 'newsletter'
    return 'other'


def categorize_pending(account=None, limit=1000, ctx=None, force=False) -> int:
    """Проставити категорію листам без неї (cron дозаповнює історію порціями)."""
    from email_assistant.models import EmailMessage
    qs = EmailMessage.objects.select_related('thread')
    if not force:
        qs = qs.filter(category='')
    if account is not None:
        qs = qs.filter(account=account)
    qs = qs.order_by('-sent_at')
    if limit:
        qs = qs[:limit]
    ctx = ctx or CategoryContext(preload_customers=True)
    done = 0
    for em in qs:
        category = categorize(em, ctx)
        if category != em.category:
            EmailMessage.objects.filter(pk=em.pk).update(category=category)
        done += 1
    return done
