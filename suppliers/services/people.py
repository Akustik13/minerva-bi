"""
suppliers/services/people.py — 👥 люди постачальника з листування → контактні особи картки. Не лише той, з чийого листа
створили постачальника: усі, хто писав нам з їхніх адрес / домену, і їхні колеги в «Кому» / «Копія» наших і їхніх
листів. Лише листи ВЛАСНИХ скриньок людини (mailscan.scan), лише адреси постачальника (match.keys), без своїх
адрес і службових (noreply, newsletter…). Викликає history.save — тобто «🔄 Зібрати / Оновити з пошти», «✉️ Аналіз
пошти», постачальник з листа.

  collect(supplier, msgs) → [{email, name, phone, count}] (найчастіші першими);
  sync(supplier, msgs, user) → нові SupplierContact (не більше MAX_NEW за раз; та сама адреса — ні; контакт з тим
                              самим іменем без email — отримує email).
"""
import re
from email.utils import parseaddr

from suppliers.services import match

MAX_NEW = 15
SKIP_RE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounces?|notifications?|"
                     r"newsletters?|marketing|news|promo|mailer|automated|system)\b", re.I)
_ROLE_RE = re.compile(r"^[a-z]+$")


def _belongs(addr, emails, domains) -> bool:
    return addr in emails or match.domain_matches(addr.split("@", 1)[1] if "@" in addr else "", domains)


def _clean_name(name, addr) -> str:
    """«"Cindy Pan" <…>», ім'я = адреса, лапки — геть; порожньо, якщо це не ім'я."""
    n = " ".join(str(name or "").strip().strip("\"'").split())
    if not n or "@" in n or n.lower() == addr.split("@")[0]:
        return ""
    return n[:200]


def pretty(addr) -> str:
    """Ім'я з адреси, коли іншого немає: cindy.pan → Cindy Pan, sales → Sales."""
    local = addr.split("@", 1)[0]
    if _ROLE_RE.match(local):
        return local.capitalize()
    return " ".join(w.capitalize() for w in re.split(r"[._-]+", local) if w)[:200] or local


def collect(supplier, msgs) -> list:
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.quotes import new_text, signature
    emails, domains = match.keys(supplier)
    own = own_addresses()
    people = {}
    for m in msgs:
        found = []
        if getattr(m, "theirs", False):
            found.append((m.from_email, m.from_name, m))
        for raw in list(m.to_emails or []) + list(m.cc_emails or []):
            name, addr = parseaddr(str(raw))
            found.append((addr or raw, name, None))
        for raw_addr, name, own_letter in found:
            addr = match.addr(raw_addr)
            if not addr or not _belongs(addr, emails, domains) or is_own(addr, own) \
                    or SKIP_RE.match(addr.split("@", 1)[0]):
                continue
            p = people.setdefault(addr, {"email": addr, "name": "", "phone": "", "count": 0})
            p["count"] += 1
            clean = _clean_name(name, addr)
            if clean:
                p["name"] = clean
            if own_letter is not None:          # їхній лист — підпис: ім'я, якщо його немає, і телефон
                sig = signature(new_text(own_letter.body_text or ""))
                if sig.get("name") and not p["name"]:
                    p["name"] = sig["name"][:200]
                if sig.get("phone"):
                    p["phone"] = sig["phone"][:60]
    return sorted(people.values(), key=lambda p: -p["count"])


def sync(supplier, msgs, user=None) -> list:
    """Нові контактні особи з листування (див. модуль). → [SupplierContact]"""
    from suppliers.models import SupplierContact, SupplierEvent
    created = []
    have = {match.addr(e) for e in supplier.contacts.exclude(email="").values_list("email", flat=True)}
    for p in collect(supplier, msgs):
        if len(created) >= MAX_NEW:
            break
        if p["email"] in have:
            continue
        name = p["name"] or pretty(p["email"])
        same = supplier.contacts.filter(email="", name__iexact=name).first()
        if same is not None:                    # уже вписаний вручну без адреси — лише дописати
            same.email = p["email"]
            same.phone = same.phone or p["phone"]
            same.save(update_fields=["email", "phone"])
            have.add(p["email"])
            continue
        c = SupplierContact.objects.create(supplier=supplier, name=name, email=p["email"], phone=p["phone"],
                                           source="mail", is_primary=not supplier.contacts.exists())
        SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.CONTACT, user=user,
                                     text=f"👤 Контакт з листування: {c.name} · {c.email}")
        have.add(p["email"])
        created.append(c)
    return created
