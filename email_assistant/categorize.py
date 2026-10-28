"""
email_assistant/categorize.py — автоматична категорія листа для фільтра в скриньці.

Порядок (перша, що підходить):
  ✋ правило    — категорія, яку користувач задав для контакту (адреса або @домен), EmailCategoryRule
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
        from email_assistant.models import EmailCategoryRule
        self.own = own_addresses()
        self.rules = {(r.account_id, r.key): r.category for r in EmailCategoryRule.objects.all()}
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

    def rule_for(self, account_id, addresses):
        """Запам'ятована категорія: спершу точна адреса, потім домен."""
        for a in addresses:
            if (account_id, a) in self.rules:
                return self.rules[(account_id, a)]
        for a in addresses:
            key = (account_id, '@' + a.split('@', 1)[1])
            if key in self.rules:
                return self.rules[key]
        return None

    def is_supplier(self, addresses) -> bool:
        return any(a in self.supplier_emails or a.split('@', 1)[1] in self.supplier_domains
                   for a in addresses)


def _is_service(address: str) -> bool:
    local, _, domain = address.partition('@')
    return any(part in domain for part in SERVICE_DOMAIN_PARTS) or bool(NOREPLY_RE.match(local))


def counterparts(em, own) -> list:
    """Співрозмовники листа: для вхідних — відправник, для своїх — зовнішні отримувачі."""
    participants = []
    for a in [em.from_email] + list(em.to_emails or []) + list(em.cc_emails or []):
        a = _addr(a)
        if a and a not in participants:
            participants.append(a)
    external = [a for a in participants if not is_own(a, own)]
    sender = _addr(em.from_email)
    return [sender] if sender in external else external


def address_category(account_id, address, ctx=None) -> str:
    """Тип співрозмовника для однієї адреси (напр. відправника пересланого колегою листа) — ті самі правила,
    що й `categorize`, без тексту листа."""
    ctx = ctx or CategoryContext()
    a = _addr(address)
    if not a or is_own(a, ctx.own):
        return 'internal'
    return ctx.rule_for(account_id, [a]) or ('customer' if ctx.is_customer([a]) else 'supplier'
                                              if ctx.is_supplier([a]) else 'service' if _is_service(a) else 'other')


def categorize(em, ctx=None, list_unsubscribe=False) -> str:
    ctx = ctx or CategoryContext()
    contacts = counterparts(em, ctx.own)
    if not contacts:
        return 'internal'

    remembered = ctx.rule_for(em.account_id, contacts)
    if remembered:
        return remembered

    if ctx.is_customer(contacts) or (em.thread_id and em.thread.crm_customer_id):
        return 'customer'
    if ctx.is_supplier(contacts):
        return 'supplier'
    if any(_is_service(a) for a in contacts):
        return 'service'
    text = (em.body_text or '')[-4000:] + ' ' + (em.subject or '')
    if list_unsubscribe or NEWSLETTER_RE.search(text):
        return 'newsletter'
    return 'other'


def _matches(address: str, key: str) -> bool:
    return address.endswith(key) if key.startswith('@') else address == key


def recategorize_contact(account, key: str) -> int:
    """Перерахувати категорії всіх листів, де співрозмовник = key (адреса або @домен)."""
    from email_assistant.models import EmailMessage
    ctx = CategoryContext(preload_customers=True)
    matched = []
    for pk, frm, to, cc in (EmailMessage.objects.filter(account=account)
                            .values_list('pk', 'from_email', 'to_emails', 'cc_emails')):
        addresses = [_addr(a) for a in [frm] + list(to or []) + list(cc or [])]
        if any(a and _matches(a, key) for a in addresses):
            matched.append(pk)
    changed = []
    for em in EmailMessage.objects.filter(pk__in=matched).select_related('thread'):
        category = categorize(em, ctx)
        if category != em.category:
            EmailMessage.objects.filter(pk=em.pk).update(category=category)
            changed.append(em.pk)
    from email_assistant.knowledge_hook import touch
    touch(changed)   # категорія визначає, чи лист іде в базу знань
    return len(changed)


def remember_category(em, category: str, scope: str = 'sender'):
    """
    Запам'ятати категорію для співрозмовника листа (scope='sender' — адреса, 'domain' — весь домен)
    і перерахувати його листи. category='' — забути правило (повернути автовизначення).
    Повертає (ключ правила, скільки листів змінило категорію) або (None, 0) для внутрішніх листів.
    """
    from email_assistant.models import EmailCategoryRule
    contacts = counterparts(em, own_addresses())
    if not contacts:
        return None, 0
    address = contacts[0]
    domain_key = '@' + address.split('@', 1)[1]
    key = domain_key if scope == 'domain' else address
    if category:
        EmailCategoryRule.objects.update_or_create(
            account=em.account, key=key, defaults={'category': category})
        return key, recategorize_contact(em.account, key)
    # «Автоматично»: забути правила адреси й домену — перерахувати весь домен
    EmailCategoryRule.objects.filter(account=em.account, key__in=[address, domain_key]).delete()
    return key, recategorize_contact(em.account, domain_key)


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
    done, changed = 0, []
    for em in qs:
        category = categorize(em, ctx)
        if category != em.category:
            EmailMessage.objects.filter(pk=em.pk).update(category=category)
            changed.append(em.pk)
        done += 1
    from email_assistant.knowledge_hook import touch
    touch(changed)
    return done
