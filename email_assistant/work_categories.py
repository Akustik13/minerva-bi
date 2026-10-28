"""
email_assistant/work_categories.py — свої категорії листів (DigiKey, Інтернет-магазин…).

Друга ознака листа поруч із типом співрозмовника (categorize.py: клієнт / постачальник / сервіс):
тип відповідає «хто пише», своя категорія — «з якої роботи лист». Лист потрапляє в першу за порядком
категорію, правила якої підходять:
  ✋ гілку віднесено вручну (EmailThread.work_category) — має пріоритет;
  ✉️ відправник / наш адресат: адреса, @домен або частина домену (MailCategory.senders);
  🔤 слово в темі;
  📮 скринька (усі листи shop@…);
  🧾 у листі номер / трек-номер замовлення з джерел категорії (і, за галочкою, покупець цих джерел);
  ⎘ інакше — категорія інших листів тієї ж гілки (відповідь на лист DigiKey теж DigiKey).
Час на категорію в askDANTE — worktime/mail_time.py.
"""
import logging
import threading

from email_assistant.crm_link import _addr

logger = logging.getLogger('email_assistant')

STATUS_KEY = 'mail_categories_scan'


class Rules:
    """Активні категорії з розібраними правилами — один раз на пакет листів."""

    def __init__(self):
        from email_assistant.models import MailCategory
        from email_assistant.crm_link import own_addresses
        self.own = own_addresses()
        self.items = []
        for c in MailCategory.objects.filter(is_active=True).prefetch_related('sources', 'accounts'):
            self.items.append({
                'cat': c,
                'senders': c.lines('senders'),
                'words': c.lines('subject_words'),
                'accounts': {a.pk for a in c.accounts.all()},
                'sources': {s.slug for s in c.sources.all()},
                'by_customer': c.by_customer_orders,
            })
        self.by_pk = {it['cat'].pk: it['cat'] for it in self.items}

    def __bool__(self):
        return bool(self.items)


def sender_matches(address: str, rule: str) -> bool:
    """Правило відправника: адреса, @домен (з піддоменами) або частина домену без @."""
    address = _addr(address)
    if not address or not rule:
        return False
    domain = address.split('@', 1)[1]
    if '@' in rule and not rule.startswith('@'):
        return address == rule
    if rule.startswith('@'):
        d = rule[1:]
        return domain == d or domain.endswith('.' + d)
    return rule in domain


def _contacts(em, own):
    from email_assistant.categorize import counterparts
    return counterparts(em, own)


def _order_sources(em) -> set:
    """Джерела замовлень, згаданих у листі (номер / трек-номер), і прив'язаного в CRM замовлення."""
    from email_assistant.links import orders_for_tokens, tokens_in
    sources = {o.source for o, _ in orders_for_tokens(tokens_in([em.subject, (em.body_text or '')[:6000]])).values()}
    if em.pk:
        from crm.models import CustomerTimeline
        from sales.models import SalesOrder
        ids = [i for i in CustomerTimeline.objects.filter(email_message=em).exclude(related_order_id=None)
               .values_list('related_order_id', flat=True)]
        if ids:
            sources |= set(SalesOrder.objects.filter(pk__in=ids).values_list('source', flat=True))
    return sources


def match(em, rules=None):
    """Своя категорія листа за правилами (без гілки) або None."""
    rules = rules if rules is not None else Rules()
    if not rules:
        return None
    contacts = _contacts(em, rules.own)
    subject = (em.subject or '').lower()
    lazy = {}
    for it in rules.items:
        if it['senders'] and any(sender_matches(a, r) for a in contacts for r in it['senders']):
            return it['cat']
        if it['words'] and any(w in subject for w in it['words']):
            return it['cat']
        if it['accounts'] and em.account_id in it['accounts']:
            return it['cat']
        if it['sources']:
            if 'orders' not in lazy:
                lazy['orders'] = _order_sources(em)
            if lazy['orders'] & it['sources']:
                return it['cat']
            if it['by_customer'] and contacts:
                from sales.models import SalesOrder
                if SalesOrder.objects.filter(email__in=contacts, source__in=it['sources']).exists():
                    return it['cat']
    return None


def resolve(em, rules=None):
    """Категорія з урахуванням гілки: вручну для гілки → правила → інші листи гілки."""
    rules = rules if rules is not None else Rules()
    thread = em.thread if em.thread_id else None
    if thread is not None and thread.work_category_id:
        cat = rules.by_pk.get(thread.work_category_id)
        if cat is not None:
            return cat
    cat = match(em, rules)
    if cat is not None or thread is None:
        return cat
    from email_assistant.models import EmailMessage
    other = (EmailMessage.objects.filter(thread_id=em.thread_id, work_category__in=list(rules.by_pk))
             .exclude(pk=em.pk).order_by('-sent_at').values_list('work_category_id', flat=True).first())
    return rules.by_pk.get(other)


def assign(em, rules=None) -> bool:
    """Поставити своя категорію листу (сигнал нового листа); True — змінилась."""
    from email_assistant.models import EmailMessage
    try:
        cat = resolve(em, rules)
    except Exception as e:  # noqa: BLE001 — правила не повинні зламати збереження листа
        logger.error('work category %s: %s', em.pk, e)
        return False
    new = cat.pk if cat else None
    if new == em.work_category_id:
        return False
    EmailMessage.objects.filter(pk=em.pk).update(work_category_id=new)
    em.work_category_id = new
    if new and em.thread_id:   # решта гілки без категорії — туди ж
        EmailMessage.objects.filter(thread_id=em.thread_id, work_category=None).update(work_category_id=new)
    return True


def recompute(qs, rules=None, progress=None) -> int:
    """Перерахувати свої категорії листів (після зміни правил). Повертає скільки змінилось."""
    rules = rules if rules is not None else Rules()
    changed = 0
    done = 0
    for em in qs.select_related('thread').order_by('sent_at').iterator(chunk_size=500):
        if assign(em, rules):
            changed += 1
        done += 1
        if progress and done % 500 == 0:
            progress(done, changed)
    return changed


def status() -> str:
    from core.shared_cache import cache
    return cache.get(STATUS_KEY) or ''


def _set_status(text: str):
    from core.shared_cache import cache
    cache.set(STATUS_KEY, text, 24 * 3600)


_lock = threading.Lock()


def start_recompute(days: int = 730):
    """У фоні: листи за `days` днів (0 — усі) за новими правилами."""
    import datetime
    from django.db import connection
    from django.utils import timezone
    from email_assistant.models import EmailMessage
    caller = threading.current_thread()

    def run():
        if not _lock.acquire(blocking=False):
            return
        try:
            qs = EmailMessage.objects.filter(is_spam=False, is_deleted=False)
            if days:
                qs = qs.filter(sent_at__gte=timezone.now() - datetime.timedelta(days=days))
            total = qs.count()
            _set_status(f'⏳ Перераховую {total} листів…')
            n = recompute(qs, progress=lambda done, ch: _set_status(f'⏳ {done} з {total} листів, змінилось {ch}'))
            _set_status(f'✅ {timezone.localtime():%d.%m.%Y %H:%M} · перевірено {total}, змінилось {n}')
        except Exception as e:  # noqa: BLE001
            logger.exception('work categories recompute: %s', e)
            _set_status(f'❌ Перерахунок перервано ({type(e).__name__})')
        finally:
            _lock.release()
            if threading.current_thread() is not caller:
                connection.close()

    threading.Thread(target=run, daemon=True).start()


# ── з меню листа «Віднести до категорії → своя» ─────────────────────────────
def _remove_line(cat, key: str) -> bool:
    lines = [ln for ln in (cat.senders or '').splitlines() if ln.strip()]
    keep = [ln for ln in lines if ln.strip().lower() != key]
    if len(keep) == len(lines):
        return False
    cat.senders = '\n'.join(keep)
    cat.save(update_fields=['senders'])
    return True


def set_for_message(em, category, scope: str = 'sender') -> dict:
    """Віднести лист до своєї категорії (або category=None — забрати).
    scope: sender — адреса співрозмовника стає правилом категорії; domain — весь @домен;
    thread — лише ця гілка (вручну). Перераховує листи, яких це стосується."""
    from django.db.models import Q
    from email_assistant.models import EmailMessage, EmailThread, MailCategory
    from email_assistant.crm_link import own_addresses
    contacts = _contacts(em, own_addresses())
    address = contacts[0] if contacts else ''
    key = ''
    if scope == 'thread' or not address:
        if em.thread_id:
            EmailThread.objects.filter(pk=em.thread_id).update(work_category=category)
        scope = 'thread'
    else:
        key = '@' + address.split('@', 1)[1] if scope == 'domain' else address
        for other in MailCategory.objects.all():   # одна адреса — одна категорія
            if category is None or other.pk != category.pk:
                _remove_line(other, key)
        if category is not None and key not in category.lines('senders'):
            category.senders = '\n'.join([ln for ln in (category.senders or '').splitlines() if ln.strip()] + [key])
            category.save(update_fields=['senders'])
        if em.thread_id and category is None:
            EmailThread.objects.filter(pk=em.thread_id).update(work_category=None)
    # кого перерахувати: гілка + листи цього співрозмовника
    cond = Q(thread_id=em.thread_id) if em.thread_id else Q(pk=em.pk)
    if key:
        needle = key.lstrip('@')
        cond |= Q(from_email__icontains=needle) | Q(to_emails__icontains=needle) | Q(cc_emails__icontains=needle)
    qs = EmailMessage.objects.filter(cond, is_deleted=False)
    if category is None:   # забрали — спершу скинути, правила інших категорій поставлять своє
        qs.filter(work_category__isnull=False).update(work_category=None)
    changed = recompute(qs)
    em.refresh_from_db(fields=['work_category'])
    return {'key': key or 'гілка', 'scope': scope, 'changed': changed,
            'category': em.work_category.label if em.work_category_id else ''}


def counts(qs) -> list:
    """[(категорія, листів, непрочитаних)] для фільтра скриньки."""
    from django.db.models import Count, Q
    from email_assistant.models import MailCategory
    rows = {r['work_category']: r for r in qs.order_by().exclude(work_category=None).values('work_category')
            .annotate(n=Count('id'), unread=Count('id', filter=Q(is_read=False)))}
    return [(c, rows.get(c.pk, {}).get('n', 0), rows.get(c.pk, {}).get('unread', 0))
            for c in MailCategory.objects.filter(is_active=True)]
