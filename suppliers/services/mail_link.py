"""
suppliers/services/mail_link.py — 🏭 постачальник у шапці листа (пошта, email_assistant/views.py preview):
хто зі співрозмовників гілки — постачальник у Minerva, і запити (RFQ), до яких належить ця гілка.

  for_messages(emails, thread, user) → [{pk, name, why, contact, cards, url, brief_url, rfq: [{code, title, url}]}]
    — співрозмовники (від / кому / копія без своїх адрес): email постачальника чи контактної особи, потім домен
      (і піддомен, без поштовиків — match.Index); рядок запиту з цією гілкою або цими листами — теж; ≤ MAX;
      права — бачити постачальників (і запити — для 📨).

Довідник адрес (match.Index) — у пам'яті процесу на INDEX_TTL: лист відкривають часто, постачальники змінюються рідко.
"""
import time

from django.db.models import Count, Q
from django.urls import reverse

MAX = 3
INDEX_TTL = 120
_cache = {"at": 0.0, "idx": None}


def _index():
    from suppliers.services.match import Index
    now = time.monotonic()
    if _cache["idx"] is None or now - _cache["at"] > INDEX_TTL:
        _cache["idx"], _cache["at"] = Index(), now
    return _cache["idx"]


def reset():
    _cache["idx"] = None


def for_messages(emails, thread, user) -> list:
    if not emails or not user.has_perm("inventory.view_supplier"):
        return []
    from email_assistant.crm_link import is_own, own_addresses
    from inventory.models import Supplier
    from suppliers.models import SupplierContact, SupplierRequestRecipient as R
    from suppliers.services.match import addr
    own = own_addresses()
    addrs = []                                   # спершу відправники (найновіші), потім кому / копія
    for m in reversed(list(emails)):
        for a in [m.from_email] + list(m.to_emails or []) + list(m.cc_emails or []):
            a = addr(a)
            if a and a not in addrs and not is_own(a, own):
                addrs.append(a)
    found = {}                                   # pk → (чому, email контакту)
    rfq = {}
    if user.has_perm("suppliers.view_supplierrequest"):
        pks = [m.pk for m in emails]
        cond = Q(reply_message_id__in=pks) | Q(sent_message_id__in=pks)
        if thread is not None:
            cond |= Q(thread_id=thread.pk)
        for r in R.objects.filter(cond).select_related("request").order_by("-request__created_at")[:10]:
            found.setdefault(r.supplier_id, ("запит постачальнику в цій гілці", r.email))
            rfq.setdefault(r.supplier_id, []).append({
                "code": r.request.code, "title": r.request.title,
                "url": reverse("admin:suppliers_supplierrequest_work", args=[r.request_id]) + f"#r{r.pk}"})
    if addrs:
        idx = _index()
        for a in addrs:
            pk = idx.by_email.get(a)
            if pk:
                found.setdefault(pk, (f"email {a}", a))
                continue
            pk = idx.for_domain(a.split("@", 1)[1])
            if pk:
                found.setdefault(pk, (f"домен @{a.split('@', 1)[1]}", a))
    if not found:
        return []
    order = list(found)[:MAX]
    sups = {s.pk: s for s in Supplier.objects.filter(pk__in=order).annotate(n_cards=Count("media"))}
    contacts = {}
    mails = [found[pk][1] for pk in order if found[pk][1]]
    for c in SupplierContact.objects.filter(supplier_id__in=order).exclude(email=""):
        if addr(c.email) in mails:
            contacts[(c.supplier_id, addr(c.email))] = c.name
    out = []
    for pk in order:
        s = sups.get(pk)
        if s is None:                            # видалили, а довідник ще старий
            continue
        why, a = found[pk]
        out.append({
            "pk": s.pk, "name": s.name, "why": why, "contact": contacts.get((s.pk, a), ""), "cards": s.n_cards,
            "url": reverse("admin:inventory_supplier_change", args=[s.pk]),
            "brief_url": reverse("admin:inventory_supplier_brief", args=[s.pk]),
            "rfq": rfq.get(pk, [])[:3],
        })
    return out
