"""Спільні зустрічі для календаря команди (Google «x_Alle Mitarbeiter»): «V, S: Telco Wirelessworks».

Правило синхронізації «Minerva → календар команди» (SyncRule.what = TEAM / mode = TEAM) бере зустріч Minerva, занесену
з запрошення (MeetingInvite → CalendarEvent), і учасників — із самого запрошення (ORGANIZER + ATTENDEE файлу .ics
листа, invites._source_event):
- «наші» — адреси власних скриньок і доменів Minerva (email_assistant.crm_link.own_addresses), власник правила — завжди
  перший (він прийняв зустріч), далі — у порядку запрошення;
- буква людини — CalendarSettings.team_letter людини Minerva з цією адресою (User.email або її скринька), інакше перша
  літера імені / логіна; невідома наша адреса — перша літера адреси;
- «спільна» зустріч (what = TEAM) — наших щонайменше двоє;
- компанія — клієнт CRM гілки листа (без правової форми: GmbH, B.V. …), інакше домен організатора або першого
  стороннього учасника («wirelessworks.it» → «Wirelessworks»), немає сторонніх — назва зустрічі;
- назва — шаблон правила (SyncRule.title_template, «{who}: Telco {company}»; є ще {title}), опис — рядок
  «👥 Учасники: …» (SyncRule.participants: лише наші / усі / нікого) + опис зустрічі.

Учасники з .ics кешуються (core.shared_cache) за запрошенням і його SEQUENCE: синхронізація йде кожні кілька хвилин,
а файл може братися з IMAP.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = "{who}: Telco {company}"
CACHE_TTL = 24 * 3600
CACHE_EMPTY_TTL = 3600     # запрошення без .ics — не питати IMAP частіше
LEGAL_RE = re.compile(r"[\s,]+(?:gmbh(?:\s*&\s*co\.?\s*kg)?|ug|ag|kg|ohg|e\.?\s?k\.?|b\.?v\.?|n\.?v\.?|ltd\.?|limited"
                      r"|llc|l\.?l\.?c\.?|inc\.?|corp\.?|co\.?|s\.?r\.?l\.?|s\.?p\.?a\.?|s\.?a\.?|sas|sarl|oy|ab|as|aps"
                      r"|sp\.?\s?z\s?o\.?\s?o\.?|s\.?r\.?o\.?|plc|pty)\.?$", re.I)
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "or", "ne"}   # example.co.uk → example


def clean_letter(value: str) -> str:
    """Буква людини: до 3 літер / цифр, великими («v» → «V», «Vi» → «VI»)."""
    return "".join(ch for ch in (value or "") if ch.isalnum())[:3].upper()


def letter_of(user) -> str:
    """Буква людини Minerva: своя (CalendarSettings.team_letter), інакше перша літера імені / логіна."""
    from .models import CalendarSettings
    cfg = CalendarSettings.objects.filter(user=user).values_list("team_letter", flat=True).first()
    return clean_letter(cfg or "") or clean_letter((user.first_name or user.username or "?")[:1]) or "?"


def _name_of(user) -> str:
    return user.first_name or user.username


def company_of_domain(address: str) -> str:
    """«pieter@mail.wirelessworks.it» → «Wirelessworks»."""
    domain = (address or "").rsplit("@", 1)[-1].lower().strip(". ")
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return ""
    label = parts[-2]
    if label in SECOND_LEVEL and len(parts) >= 3:
        label = parts[-3]
    return label[:1].upper() + label[1:]


def short_company(name: str) -> str:
    """«Wireless Works B.V.» → «Wireless Works»: правова форма в кінці не потрібна в назві події."""
    name = (name or "").strip()
    for _ in range(2):
        shorter = LEGAL_RE.sub("", name).strip(" ,")
        if shorter == name:
            break
        name = shorter
    return name


def _mailto(value: str) -> str:
    return re.sub(r"^mailto:", "", (value or "").strip(), flags=re.I).strip().lower()


def people_of(inv) -> list[dict] | None:
    """Учасники запрошення [{email, cn, organizer}] (організатор першим); None — файлу запрошення немає."""
    from core.shared_cache import cache
    key = f"cal-team:{inv.pk}:{inv.sequence}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    from .invites import RespondError, _source_event
    try:
        raw = _source_event(inv)
    except RespondError:
        raw = None
    except Exception as e:  # noqa: BLE001 — пошта недоступна: без учасників цього разу
        logger.info("team people of invite %s: %s", inv.pk, e)
        return None
    if raw is None:
        cache.set(key, [], CACHE_EMPTY_TTL)
        return None
    out, seen = [], set()
    for name, organizer in (("ORGANIZER", True), ("ATTENDEE", False)):
        for params, value in raw.get(name, []):
            email = _mailto(value)
            if "@" not in email or email in seen:
                continue
            seen.add(email)
            out.append({"email": email, "cn": (params.get("CN") or "").strip()[:100], "organizer": organizer})
    cache.set(key, out, CACHE_TTL)
    return out


class Team:
    """Хто з наших і хто ще на зустрічі. ours — [(буква, ім'я, email)] (власник правила першим), others — сторонні."""

    def __init__(self, ours, others, company):
        self.ours, self.others, self.company = ours, others, company

    @property
    def letters(self) -> str:
        return ", ".join(letter for letter, _n, _e in self.ours)

    def title(self, template: str, original: str) -> str:
        company = self.company or original
        out = (template or DEFAULT_TEMPLATE).replace("{who}", self.letters).replace("{company}", company)
        return out.replace("{title}", original).strip()[:300]

    def participants_line(self, which: str) -> str:
        from .models import SyncRule
        if which == SyncRule.PEOPLE_NONE:
            return ""
        names = [f"{n} ({letter})" for letter, n, _e in self.ours]
        if which == SyncRule.PEOPLE_ALL:
            names += [p["cn"] or p["email"] for p in self.others]
        return f"👥 Учасники: {', '.join(names)}" if names else ""


class Resolver:
    """Одна синхронізація — один Resolver: власні адреси й люди Minerva рахуються раз."""

    def __init__(self, owner):
        from django.contrib.auth import get_user_model
        from email_assistant.crm_link import own_addresses
        from email_assistant.models import EmailAccount
        self.owner = owner
        self.own = own_addresses()
        users = {}
        for u in get_user_model().objects.filter(is_active=True).exclude(email=""):
            users.setdefault(u.email.strip().lower(), u)
        for acc in EmailAccount.objects.filter(user__is_active=True).select_related("user"):
            users.setdefault((acc.email_address or "").strip().lower(), acc.user)
        self.users = users
        self._letters = {}

    def is_ours(self, email: str) -> bool:
        from email_assistant.crm_link import is_own
        return is_own(email, self.own)

    def _letter(self, user) -> str:
        if user.pk not in self._letters:
            self._letters[user.pk] = letter_of(user)
        return self._letters[user.pk]

    def team_for_event(self, event_id) -> Team | None:
        from .models import MeetingInvite
        inv = (MeetingInvite.objects.filter(user=self.owner, event_id=event_id)
               .select_related("email_message__account", "crm_customer").order_by("-pk").first())
        if inv is None:
            return None
        people = people_of(inv)
        if people is None:
            return None
        ours, others, seen_users = [], [], set()
        owner_entry = (self._letter(self.owner), _name_of(self.owner), "")
        for p in people:
            if not self.is_ours(p["email"]):
                others.append(p)
                continue
            user = self.users.get(p["email"])
            if user is not None:
                if user.pk in seen_users:
                    continue
                seen_users.add(user.pk)
                if user.pk == self.owner.pk:
                    owner_entry = (self._letter(user), _name_of(user), p["email"])
                    continue
                ours.append((self._letter(user), _name_of(user), p["email"]))
            else:
                name = (p["cn"].split() or [p["email"].split("@")[0]])[0]
                ours.append((clean_letter(name[:1]) or "?", name, p["email"]))
        ours.insert(0, owner_entry)   # власник правила прийняв зустріч — він теж на ній
        company = short_company(inv.crm_customer.company) if inv.crm_customer and inv.crm_customer.company else ""
        if not company:
            organizer = next((p for p in others if p["organizer"]), None) or (others[0] if others else None)
            company = company_of_domain(organizer["email"]) if organizer else ""
        return Team(ours, others, company)
