"""З якої адреси йдуть листи клієнтам: «📧 посилку відправлено» (ship) і «📥 замовлення отримано» (confirm).

Вибір — NotificationSettings.customer_senders {"all": "" | "acc:<pk>", "ship" / "confirm": "" | "ns" | "acc:<pk>"}:
"" у листа — як «all» (для всіх листів клієнтам), "ns" і порожнє «all» — SMTP сповіщень (email_from, як завжди),
"acc:<pk>" — скринька пошти Minerva (email_assistant.EmailAccount): лист іде через SMTPClient, From — адреса
скриньки, копія — у її «Надісланих» (після синхронізації лист видно в пошті й у хронології клієнта). Скриньку
видалено, вимкнено або в ній немає SMTP — лист іде з SMTP сповіщень, а сторінка налаштувань показує попередження
(Sender.problem).

Хто вибирає: скриньки особисті, тож у меню — лише свої скриньки людини, адміністратору Minerva — усі (may_use);
уже вибрану чужу скриньку видно й можна замінити, але вибрати іншу чужу не-адміністратор не може (save). Надсилає
лист кожен, кому дозволено лист клієнту (sales/admin._notify_refused): адресу вибрали в налаштуваннях заздалегідь.
Таблиця на сторінці — config/admin_notify_map.py; скриньки — ті самі помічники, що для магазинів (shop/mail.py).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from shop.mail import route_account, usable

logger = logging.getLogger("dashboard")

GROUP = "all"
KINDS = ("ship", "confirm")
NS = "ns"


class SendError(Exception):
    """Скринька не надіслала лист (відповідь SMTP) — текст помилки для людини."""


@dataclass
class Sender:
    route: str              # що діє: "" — SMTP сповіщень, "acc:<pk>" — скринька
    account: object = None  # скринька, через яку піде лист; None — SMTP сповіщень
    address: str = ""       # адреса From
    problem: str = ""       # вибрану скриньку взяти не можна — чому (лист іде з SMTP сповіщень)

    @property
    def source(self) -> str:
        return "скринька пошти Minerva" if self.account is not None else "SMTP сповіщень"


def ns_address(ns) -> str:
    return (ns.email_from or ns.email_host_user or "").strip()


def _clean(value, group: bool = False) -> str:
    v = str(value or "").strip()
    if v == NS:
        return "" if group else NS
    return v if v.startswith("acc:") and v[4:].isdigit() else ""


def _data(ns) -> dict:
    data = getattr(ns, "customer_senders", None)
    return data if isinstance(data, dict) else {}


def route_of(ns, kind: str) -> str:
    """Що вибрано: для листа — "" (як для всіх), "ns" або "acc:<pk>"; для всіх (GROUP) — "" або "acc:<pk>"."""
    return _clean(_data(ns).get(kind), group=kind == GROUP)


def effective_route(ns, kind: str) -> str:
    """"" — SMTP сповіщень, "acc:<pk>" — скринька."""
    route = route_of(ns, kind)
    if kind != GROUP and route == "":
        route = route_of(ns, GROUP)
    return "" if route == NS else route


def why_unusable(acc, route: str) -> str:
    if acc is None:
        return f"скриньку пошти №{route[4:]} видалено"
    if not acc.is_active:
        return f"скриньку {acc.email_address} вимкнено"
    return f"у скриньці {acc.email_address} не задано SMTP"


def sender(ns, kind: str) -> Sender:
    """З якої адреси піде лист цього виду зараз (з урахуванням недоступної скриньки)."""
    route = effective_route(ns, kind)
    if not route:
        return Sender("", None, ns_address(ns))
    acc = route_account(route)
    if not usable(acc):
        return Sender(route, None, ns_address(ns), why_unusable(acc, route))
    return Sender(route, acc, acc.email_address)


def may_use(user, acc) -> bool:
    """Вибрати скриньку для листів клієнтам: її власник або адміністратор Minerva."""
    if acc is None or user is None or not getattr(user, "is_authenticated", False):
        return False
    if acc.user_id == user.pk:
        return True
    from core.utils import is_minerva_admin
    return is_minerva_admin(user)


def mailboxes(user) -> list:
    """Скриньки, які людина може вибрати: свої (адміністратор — усі) активні з SMTP."""
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    from email_assistant.models import EmailAccount
    qs = EmailAccount.objects.filter(is_active=True).select_related("user").order_by("email_address")
    from core.utils import is_minerva_admin
    if not is_minerva_admin(user):
        qs = qs.filter(user=user)
    return [a for a in qs if usable(a)]


def label(acc, user=None, route: str = "") -> str:
    if acc is None:
        return f"⚠ скриньку пошти №{route[4:]} видалено"
    owner = (acc.user.get_full_name() or acc.user.get_username()) if acc.user_id else ""
    text = acc.email_address + (f" — {owner}" if owner else "")
    if user is not None and acc.user_id == getattr(user, "pk", None):
        text += " (ваша)"
    if not usable(acc):
        text += " — ⚠ вимкнена або без SMTP"
    return text


def options(ns, user, kind: str = GROUP) -> list[tuple[str, str]]:
    """Скриньки для меню «з адреси» листа kind: [("acc:<pk>", підпис)] — ті, що людина може вибрати, і вже вибрана в
    цьому меню (навіть чужа чи недоступна — щоб було видно, що зараз діє)."""
    out = [(f"acc:{a.pk}", label(a, user)) for a in mailboxes(user)]
    route = route_of(ns, kind)
    if route.startswith("acc:") and route not in {v for v, _ in out}:
        out.append((route, label(route_account(route), user, route)))
    return out


def save(ns, post, user) -> list[str]:
    """Меню «з адреси» таблиці «🗺 Хто що отримує» → ns.customer_senders (ns ще не збережено). Лише меню, що прийшли
    у формі; без змін — нічого не перевіряється (уже вибрана чужа скринька лишається). Нову скриньку можна вибрати
    лише доступну (may_use, активна з SMTP) — інакше без змін; повертає такі (для попередження)."""
    if "cm_seen" not in post:
        return []
    data, refused = dict(_data(ns)), []
    for key in (GROUP, *KINDS):
        name = f"cm_{key}"
        if name not in post:
            continue
        raw = str(post.get(name) or "").strip()
        new = _clean(raw, group=key == GROUP)
        if raw and raw != NS and not new:
            refused.append(raw)
            continue
        if new == route_of(ns, key):
            continue
        if new.startswith("acc:"):
            acc = route_account(new)
            if not (usable(acc) and may_use(user, acc)):
                refused.append(acc.email_address if acc is not None else new)
                continue
        if new:
            data[key] = new
        else:
            data.pop(key, None)
    ns.customer_senders = data
    return refused


def send(ns, kind: str, to: list, subject: str, text: str, html: str, cc=()) -> Sender:
    """Надіслати лист клієнту з адреси, вибраної для цього виду. Помилка — виняток (SendError або SMTP Django)."""
    s = sender(ns, kind)
    to, cc = [a for a in to if a], [a for a in cc if a]
    if s.problem:
        logger.warning("Лист клієнту (%s): %s — надсилаю з SMTP сповіщень", kind, s.problem)
    if s.account is not None:
        from email_assistant.smtp_client import SMTPClient
        result = SMTPClient(s.account).send(to, subject, text, html, cc_emails=cc, signature=False)
        if not result.get("ok"):
            raise SendError(result.get("error") or f"скринька {s.address} не надіслала лист")
        return s
    from django.core.mail import EmailMultiAlternatives

    from dashboard import notifications
    msg = EmailMultiAlternatives(subject=subject, body=text, from_email=s.address, to=to, cc=cc,
                                 connection=notifications._smtp_connection(ns))
    msg.attach_alternative(html, "text/html")
    msg.send()
    return s


def cc_list(value: str) -> list[str]:
    return [a.strip() for a in (value or "").split(",") if a.strip()]
