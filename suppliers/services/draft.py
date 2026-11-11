"""
suppliers/services/draft.py — новий постачальник з листа / візитки / знахідки в пошті: чернетка полів, перевірка,
чи вже є (match.find), створення (постачальник + контактна особа + подія «звідки взявся»), додавання контакту
до наявного.
"""
import re

from django.db import transaction

from suppliers.services import match

FIELDS = ("name", "website", "email", "phone", "addr_street", "addr_zip", "addr_city", "addr_country", "vat_id",
          "description", "payment_terms", "currency")
CONTACT_FIELDS = ("contact_name", "contact_position", "contact_email", "contact_phone", "contact_mobile")
_URL_RE = re.compile(r"\b(?:https?://)?(?:www\.)[\w-]+(?:\.[\w-]+)+\b|https?://[\w.-]+\.[a-z]{2,}\b", re.I)
_VAT_RE = re.compile(r"\b(?:VAT|USt-?Id(?:Nr)?\.?|UID|NIP|P\.?IVA|TVA)[\s.:№#-]*([A-Z]{2}\s?[0-9A-Z]{8,12})\b", re.I)


def from_message(msg) -> dict:
    """Чернетка з листа: фірма, контакт, email, телефон, адреса — як «👤 Клієнт CRM з листа» (підпис, ім'я
    відправника), сайт — з домену (не поштовика) або посилання в підписі, VAT ID — з тексту."""
    from email_assistant.crm_link import customer_draft
    from email_assistant.quotes import new_text
    base = customer_draft(msg) or {}
    email = (base.get("email") or "").lower()
    domain = email.split("@", 1)[1] if "@" in email else ""
    text = new_text(msg.body_text or "")
    tail = "\n".join(text.splitlines()[-30:])
    site = ""
    m = _URL_RE.search(tail)
    if m:
        site = m.group(0)
    elif domain and not match.is_free(domain):
        site = "www." + domain
    if site and "://" not in site:
        site = "https://" + site
    vat = _VAT_RE.search(text)
    company = base.get("company") or ""
    if not company and domain and not match.is_free(domain):
        company = domain.split(".")[-2].capitalize() if domain.count(".") else domain
    return {
        "name": company[:255], "website": site[:200], "email": email, "phone": base.get("phone", ""),
        "addr_street": base.get("addr_street", ""), "addr_zip": base.get("addr_zip", ""),
        "addr_city": base.get("addr_city", ""), "addr_country": base.get("country", ""),
        "vat_id": vat.group(1).replace(" ", "") if vat else "", "description": "", "payment_terms": "",
        "currency": "",
        "contact_name": base.get("name", "") if base.get("name", "") != company else "",
        "contact_position": "", "contact_email": email, "contact_phone": base.get("phone", ""), "contact_mobile": "",
    }


def existing(data: dict) -> list:
    """[(постачальник, чому)] — схожі на чернетку."""
    emails = [data.get("email"), data.get("contact_email")]
    return match.find(emails=[e for e in emails if e], name=data.get("name", ""), website=data.get("website", ""))


def clean(data: dict) -> dict:
    out = {k: " ".join(str(data.get(k) or "").split()) for k in FIELDS + CONTACT_FIELDS}
    out["description"] = str(data.get("description") or "").strip()[:4000]
    out["addr_country"] = out["addr_country"].upper()[:2] if len(out["addr_country"]) >= 2 else ""
    out["currency"] = out["currency"].upper()[:3]
    for k in ("email", "contact_email"):
        out[k] = match.addr(out[k])
    if out["website"] and "://" not in out["website"]:
        out["website"] = "https://" + out["website"]
    return out


def _contact_exists(supplier, email, name) -> bool:
    qs = supplier.contacts.all()
    if email and qs.filter(email__iexact=email).exists():
        return True
    if name:
        return qs.filter(name__iexact=name).exists()
    return bool(email) and (supplier.email or "").lower() == email     # без імені — лише адреса фірми, не людина


def add_contact(supplier, data: dict, source="mail", user=None, media=None, message=None):
    """Контактна особа з чернетки (якщо ще немає такої) → (контакт | None, створено?)."""
    from suppliers.models import SupplierContact, SupplierEvent
    name, email = (data.get("contact_name") or "").strip(), match.addr(data.get("contact_email"))
    if not (name or email):
        return None, False
    if _contact_exists(supplier, email, name):
        c = supplier.contacts.filter(email__iexact=email).first() if email else \
            supplier.contacts.filter(name__iexact=name).first()
        return c, False
    c = SupplierContact.objects.create(
        supplier=supplier, name=(name or email.split("@")[0])[:200], position=(data.get("contact_position") or "")[:200],
        email=email, phone=(data.get("contact_phone") or "")[:60], mobile=(data.get("contact_mobile") or "")[:80],
        source=source if source in dict(SupplierContact.SOURCES) else "manual",
        is_primary=not supplier.contacts.exists())
    SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.CONTACT, user=user, media=media,
                                 message=message, text=f"Контакт: {c.name}" + (f" · {c.email}" if c.email else ""))
    return c, True


def create(data: dict, user=None, source="mail", message=None, media=None, categories=(), note=""):
    """Новий постачальник (+ контакт, + подія «🆕 звідки»). Назва вже зайнята — ValueError."""
    from inventory.models import Supplier
    from suppliers.models import SupplierEvent
    d = clean(data)
    name = d["name"] or (d["email"].split("@")[1] if d["email"] else "")
    if not name:
        raise ValueError("Вкажіть назву фірми.")
    if Supplier.objects.filter(name__iexact=name).exists():
        raise ValueError(f"Постачальник «{name}» уже є.")
    with transaction.atomic():
        s = Supplier.objects.create(
            name=name[:255], website=d["website"][:200], email=d["email"], phone=d["phone"][:50],
            addr_street=d["addr_street"][:300], addr_zip=d["addr_zip"][:20], addr_city=d["addr_city"][:100],
            addr_country=d["addr_country"], vat_id=d["vat_id"][:40], description=d["description"],
            payment_terms=d["payment_terms"][:100], currency=d["currency"] or "EUR",
            contact_person=d["contact_name"][:255], notes=(note or "")[:4000],
            source=source if source in dict(Supplier.SOURCE_CHOICES) else "manual",
            status=Supplier.STATUS_POTENTIAL if source in ("card", "discover") else Supplier.STATUS_ACTIVE)
        if categories:
            s.categories.set(categories)
        origin = {"mail": "з листа", "card": "з візитки / флаєра", "discover": "знайдено в пошті"}.get(source, "вручну")
        text = f"Створено {origin}"
        if message is not None:
            text += f": «{(message.subject or '')[:150]}» від {message.from_email}"
        SupplierEvent.objects.create(supplier=s, kind=SupplierEvent.CREATED, text=text, message=message, media=media,
                                     user=user)
        if media is not None:
            type(media).objects.filter(pk=media.pk).update(supplier=s)
        add_contact(s, d, source=source, user=user, media=media, message=message)
    return s


def fill_empty(supplier, data: dict, user=None, why="") -> list:
    """Доповнити наявного постачальника: лише порожні поля. → назви змінених полів."""
    from suppliers.models import SupplierEvent
    d = clean(data)
    changed = []
    for f in ("website", "email", "phone", "addr_street", "addr_zip", "addr_city", "addr_country", "vat_id",
              "description", "payment_terms"):
        if d.get(f) and not getattr(supplier, f):
            setattr(supplier, f, d[f][:supplier._meta.get_field(f).max_length or 4000])
            changed.append(f)
    if changed:
        supplier.save(update_fields=changed)
        SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.NOTE, user=user,
                                     text=f"Доповнено {why}: " + ", ".join(
                                         str(supplier._meta.get_field(f).verbose_name) for f in changed))
    return changed
