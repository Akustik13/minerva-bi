"""
suppliers/services/match.py — чи є вже такий постачальник: за email, доменом (email, сайт, «Поштові домени»,
контактні особи) і назвою фірми без «GmbH / Co., Ltd.». Ним користуються «🏭 Постачальник з листа», «📷 Візитка»,
«🔎 Постачальники з пошти» й аналіз пошти постачальника.
"""
import re
from urllib.parse import urlparse

from email_assistant.crm_link import FREE_MAIL_DOMAINS

LEGAL_RE = re.compile(
    r"\b(gmbh|co|kg|ag|ug|ohg|mbh|llc|ltd|limited|inc|corp|corporation|company|bv|b\.v|sa|s\.a|srl|s\.r\.l|oy|ab|"
    r"aps|llp|plc|spa|s\.p\.a|sas|sarl|kft|sp\.?\s?z\.?o\.?o|technology|technologies|tech|electronics?|"
    r"industrial|industries|international|group|holding|trading|manufacturing|factory)\b\.?", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def addr(value) -> str:
    """«Name <a@b.de>» → «a@b.de»."""
    m = _EMAIL_RE.search(str(value or ""))
    return m.group(0).lower() if m else ""


def domain_of(email) -> str:
    a = addr(email)
    return a.split("@", 1)[1] if a else ""


def site_domain(url) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_free(domain) -> bool:
    return (domain or "").lower() in FREE_MAIL_DOMAINS


def name_key(name) -> str:
    """«Shenzhen JLC Electronics Co., Ltd.» → «shenzhenjlc»: без правової форми, розділювачів і регістру."""
    s = LEGAL_RE.sub(" ", str(name or "").lower())
    return re.sub(r"[^0-9a-zа-яіїєґ]+", "", s)


def keys(supplier) -> tuple[set, set]:
    """(email-адреси, домени) постачальника: email, контактні особи, домен сайту, «Поштові домени»."""
    emails = {addr(supplier.email)} if supplier.email else set()
    try:
        emails |= {addr(e) for e in supplier.contacts.exclude(email="").values_list("email", flat=True)}
    except Exception:  # noqa: BLE001 — ще не збережений
        pass
    emails.discard("")
    domains = {e.split("@", 1)[1] for e in emails} | {site_domain(supplier.website)}
    domains |= {d.strip().lower().lstrip("@") for d in (supplier.domains or "").split(",")}
    domains = {d for d in domains if d and not is_free(d)}
    return emails, domains


def domain_matches(domain, own_domains) -> bool:
    """mail.pcbway.com ↔ pcbway.com — піддомен теж."""
    d = (domain or "").lower()
    return any(d == x or d.endswith("." + x) for x in own_domains)


class Index:
    """Усі постачальники одним разом: email → id, домен → id, назва → id."""

    def __init__(self):
        from inventory.models import Supplier
        from suppliers.models import SupplierContact
        self.by_email, self.by_domain, self.by_name, self.names = {}, {}, {}, {}
        for s in Supplier.objects.only("pk", "name", "email", "website", "domains"):
            self.names[s.pk] = s.name
            if s.email:
                self.by_email.setdefault(addr(s.email), s.pk)
            for d in [domain_of(s.email), site_domain(s.website)] + (s.domains or "").split(","):
                d = (d or "").strip().lower().lstrip("@")
                if d and not is_free(d):
                    self.by_domain.setdefault(d, s.pk)
            key = name_key(s.name)
            if len(key) >= 3:
                self.by_name.setdefault(key, s.pk)
        for sid, email in SupplierContact.objects.exclude(email="").values_list("supplier_id", "email"):
            a = addr(email)
            if a:
                self.by_email.setdefault(a, sid)
                d = a.split("@", 1)[1]
                if not is_free(d):
                    self.by_domain.setdefault(d, sid)

    def for_email(self, email):
        a = addr(email)
        if not a:
            return None
        if a in self.by_email:
            return self.by_email[a]
        return self.for_domain(a.split("@", 1)[1])

    def for_domain(self, domain):
        d = (domain or "").lower()
        if not d or is_free(d):
            return None
        while d.count("."):
            if d in self.by_domain:
                return self.by_domain[d]
            d = d.split(".", 1)[1]
        return None

    def for_name(self, name):
        key = name_key(name)
        return self.by_name.get(key) if len(key) >= 3 else None


def find(emails=(), name="", website="", domains=(), idx=None) -> list:
    """Наявні постачальники, схожі на ці дані: [(постачальник, чому)] — спершу найнадійніше. idx — один Index на
    багато перевірок (сторінка візиток), інакше — новий."""
    from inventory.models import Supplier
    idx = idx or Index()
    hits = {}

    def hit(pk, why):
        if pk and pk not in hits:
            hits[pk] = why

    for e in emails or ():
        a = addr(e)
        if a and a in idx.by_email:
            hit(idx.by_email[a], f"той самий email {a}")
    for e in emails or ():
        d = domain_of(e)
        hit(idx.for_domain(d), f"той самий домен @{d}")
    for d in list(domains or ()) + [site_domain(website)]:
        hit(idx.for_domain((d or "").strip().lower().lstrip("@")), f"той самий сайт / домен {d}")
    if name:
        hit(idx.for_name(name), "та сама назва фірми")
        key = name_key(name)
        if len(key) >= 5:
            for pk, other in idx.names.items():
                k2 = name_key(other)
                if pk not in hits and len(k2) >= 5 and (key in k2 or k2 in key):
                    hit(pk, f"схожа назва «{other}»")
    found = {s.pk: s for s in Supplier.objects.filter(pk__in=list(hits))}
    return [(found[pk], why) for pk, why in hits.items() if pk in found]
