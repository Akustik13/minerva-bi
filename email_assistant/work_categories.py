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
PROGRESS_EVERY = 25      # листів між оновленнями стану
STALE_SECONDS = 180      # стільки без оновлення — перерахунок, мабуть, перервався (сервер перезапустився)


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
        if progress and done % PROGRESS_EVERY == 0:
            progress(done, changed)
    return changed


def state() -> dict:
    """Стан фонового перерахунку для сторінки: text, running, done, total.
    «Іде», але давно не оновлювався — перервався (перезапуск сервера), можна запускати знову."""
    import datetime
    import time
    from django.utils import timezone
    from core.shared_cache import cache
    raw = cache.get(STATUS_KEY) or {}
    if isinstance(raw, str):   # старий формат — лише текст
        raw = {'text': raw}
    st = {'text': raw.get('text', ''), 'running': bool(raw.get('running')),
          'done': raw.get('done', 0), 'total': raw.get('total', 0)}
    if st['running'] and time.time() - raw.get('at', 0) > STALE_SECONDS:
        at = timezone.localtime(datetime.datetime.fromtimestamp(raw.get('at', 0), tz=datetime.timezone.utc))
        st.update(running=False, text=f"⚠️ Перерахунок зупинився на {st['done']} з {st['total']} листів "
                                       f"(останній знак {at:%d.%m %H:%M}) — мабуть, сервер перезапускався. "
                                       "Запустіть ще раз.")
    return st


def status() -> str:
    return state()['text']


def _set_status(text: str, running: bool = False, done: int = 0, total: int = 0):
    import time
    from core.shared_cache import cache
    cache.set(STATUS_KEY, {'text': text, 'running': running, 'done': done, 'total': total, 'at': time.time()},
              24 * 3600)


_lock = threading.Lock()


def period_bounds(date_from=None, date_to=None, days: int = 60):
    """Початок і кінець періоду (aware): «з … по …» включно, інакше останні `days` днів."""
    import datetime
    from django.utils import timezone
    tz = timezone.get_current_timezone()
    end = (timezone.make_aware(datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min), tz)
           if date_to else timezone.now())
    start = (timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min), tz) if date_from
             else end - datetime.timedelta(days=days))
    return start, end


def start_recompute(days: int = 730, date_from=None, date_to=None) -> bool:
    """У фоні: листи за `days` днів (0 — усі) за новими правилами. False — інший перерахунок ще йде."""
    import datetime
    from django.db import connection
    from django.utils import timezone
    from email_assistant.models import EmailMessage
    caller = threading.current_thread()
    if state()['running'] or _lock.locked():
        return False
    _set_status('⏳ Збираю листи…', running=True)

    def run():
        if not _lock.acquire(blocking=False):
            return
        try:
            qs = EmailMessage.objects.filter(is_spam=False, is_deleted=False)
            if date_from or date_to:
                start, end = period_bounds(date_from, date_to, days or 730)
                qs = qs.filter(sent_at__gte=start, sent_at__lt=end)
            elif days:
                qs = qs.filter(sent_at__gte=timezone.now() - datetime.timedelta(days=days))
            total = qs.count()
            _set_status(f'⏳ 0 з {total} листів…', running=True, total=total)
            n = recompute(qs, progress=lambda done, ch: _set_status(f'⏳ {done} з {total} листів, змінилось {ch}',
                                                                    running=True, done=done, total=total))
            _set_status(f'✅ {timezone.localtime():%d.%m.%Y %H:%M} · перевірено {total}, змінилось {n}',
                        done=total, total=total)
        except Exception as e:  # noqa: BLE001
            logger.exception('work categories recompute: %s', e)
            _set_status(f'❌ Перерахунок перервано ({type(e).__name__})')
        finally:
            _lock.release()
            if threading.current_thread() is not caller:
                connection.close()

    threading.Thread(target=run, daemon=True).start()
    return True


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


# ── 🔎 пропозиції категорій за період (без AI: з того, що Minerva вже знаходить у листах) ─────────────────
SUGGEST_MIN = 5          # менше листів — групу не пропонуємо
SUGGEST_LIMIT = 4000     # листів за раз


def _domain_label(address: str) -> str:
    """digikey.de / eu.digikey.com → digikey (частина домену для правила «Відправники»)."""
    domain = address.split('@', 1)[1] if '@' in address else address
    parts = [p for p in domain.lower().split('.') if p]
    if len(parts) >= 3 and parts[-2] in ('co', 'com', 'org', 'net') and len(parts[-1]) == 2:
        return parts[-3]   # example.co.uk
    return parts[-2] if len(parts) >= 2 else domain


def suggest(days: int = 60, accounts=None, date_from=None, date_to=None) -> list:
    """Групи листів за `days` днів, які ще не в своїх категоріях: джерела замовлень (номер / трек-номер у листі або
    замовлення CRM-події — так само, як блок «🔗» у листі) і постійні відправники-сервіси / постачальники (за
    доменом). Те саме джерело й домен (digikey) — одна група. → [{key, name, senders, sources, mails, subjects}]"""
    import datetime
    from collections import Counter, defaultdict
    from django.utils import timezone
    from crm.models import CustomerTimeline
    from email_assistant.categorize import counterparts
    from email_assistant.crm_link import FREE_MAIL_DOMAINS, _TOKEN_RE, own_addresses
    from email_assistant.models import EmailMessage
    from sales.models import SalesOrder, SalesSource
    from shipping.models import Shipment

    since, until = period_bounds(date_from, date_to, days)
    qs = EmailMessage.objects.filter(sent_at__gte=since, sent_at__lt=until, is_spam=False, is_deleted=False,
                                     work_category=None)
    if accounts is not None:
        qs = qs.filter(account__in=accounts)
    msgs = list(qs.order_by('-sent_at').values('pk', 'subject', 'body_text', 'from_email', 'to_emails', 'cc_emails',
                                               'category')[:SUGGEST_LIMIT])
    own = own_addresses()
    tokens_of = {}
    for m in msgs:
        text = f"{m['subject']} {(m['body_text'] or '')[:6000]}"
        tokens_of[m['pk']] = {t for t in _TOKEN_RE.findall(text) if sum(ch.isdigit() for ch in t) >= 5}
    every = set().union(*tokens_of.values()) if tokens_of else set()
    every |= {t.upper() for t in every}
    by_token = {}
    for number, source in SalesOrder.objects.filter(order_number__in=every).values_list('order_number', 'source'):
        by_token[number] = source
    for number, source in (SalesOrder.objects.filter(tracking_number__in=every).exclude(tracking_number='')
                           .values_list('tracking_number', 'source')):
        by_token.setdefault(number, source)
    for number, source in (Shipment.objects.filter(tracking_number__in=every).exclude(tracking_number='')
                           .exclude(order=None).values_list('tracking_number', 'order__source')):
        by_token.setdefault(number, source)
    crm_source = dict(CustomerTimeline.objects.filter(email_message_id__in=[m['pk'] for m in msgs])
                      .exclude(related_order_id=None).values_list('email_message_id', 'related_order_id'))
    if crm_source:
        order_src = dict(SalesOrder.objects.filter(pk__in=set(crm_source.values())).values_list('pk', 'source'))
        crm_source = {mid: order_src.get(oid) for mid, oid in crm_source.items()}

    groups = defaultdict(lambda: {'mails': set(), 'senders': set(), 'sources': set(), 'subjects': Counter()})
    for m in msgs:
        sources = {by_token[t] for t in tokens_of[m['pk']] | {t.upper() for t in tokens_of[m['pk']]} if t in by_token}
        if crm_source.get(m['pk']):
            sources.add(crm_source[m['pk']])
        em = EmailMessage(from_email=m['from_email'], to_emails=m['to_emails'] or [], cc_emails=m['cc_emails'] or [])
        contacts = counterparts(em, own)
        keys = set()
        for s in sources - {None, ''}:
            keys.add(s.lower())
            groups[s.lower()]['sources'].add(s)
        if m['category'] in ('service', 'supplier'):
            for a in contacts:
                domain = a.split('@', 1)[1]
                if domain in FREE_MAIL_DOMAINS:
                    continue
                label = _domain_label(a)
                keys.add(label)
                groups[label]['senders'].add(label)
        for k in keys:
            groups[k]['mails'].add(m['pk'])
            groups[k]['subjects'][(m['subject'] or '')[:80]] += 1

    names = {s.slug.lower(): s.name for s in SalesSource.objects.all()}
    rules = Rules()
    out = []
    for key, g in groups.items():
        if len(g['mails']) < SUGGEST_MIN:
            continue
        if any(set(it['sources']) & g['sources'] or any(sender_matches(f'x@{key}.com', r) for r in it['senders'])
               for it in rules.items):
            continue   # уже є категорія з цим джерелом / відправником
        out.append({'key': key, 'name': names.get(key) or key.capitalize(), 'senders': sorted(g['senders']),
                    'sources': sorted(g['sources']), 'mails': len(g['mails']), 'pks': sorted(g['mails']),
                    'subjects': [s for s, _ in g['subjects'].most_common(4) if s]})
    out.sort(key=lambda g: -g['mails'])
    return out


def create_from_suggestion(name: str, senders, sources, icon: str = '🏷'):
    """Своя категорія з пропозиції: правила «Відправники» (частина домену) і «Замовлення з джерел»."""
    from email_assistant.models import MailCategory
    from sales.models import SalesSource
    cat = MailCategory.objects.filter(name__iexact=name).first() or MailCategory.objects.create(
        name=name[:80], icon=icon[:8], order=MailCategory.objects.count())
    lines = [ln for ln in (cat.senders or '').splitlines() if ln.strip()]
    for s in senders:
        if s.lower() not in [ln.strip().lower() for ln in lines]:
            lines.append(s)
    cat.senders = '\n'.join(lines)
    cat.save(update_fields=['senders'])
    cat.sources.add(*SalesSource.objects.filter(slug__in=list(sources)))
    return cat
