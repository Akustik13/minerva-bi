"""Пошта сайту магазину з Minerva — одна скринька для всіх листів магазину.

Сайт (api/shop/_lib.php shop_mail_remote) раз на хвилину читає GET /api/v1/shop/site-mail/ своїм ключем:
- «Сайт надсилає листи з цієї ж скриньки» вимкнено (managed = false) — сайт бере пошту з private/contact-config.php
  і /admin/ → «Пошта», як раніше;
- увімкнено — SMTP, відправник, ім'я, «Відповіді покупців» і «Прихована копія» беруться з картки магазину;
  підтвердження замовлення покупцю — «Сайт надсилає покупцю підтвердження замовлення», сповіщення про нові
  замовлення — «Сповіщення команді — на адреси» (порожньо — як на сайті).
У тому самому запиті сайт повідомляє, що в його власних налаштуваннях пошти (без пароля), — це видно в картці.
Пароль скриньки віддається лише ключу, прив'язаному до магазину; без нього сайт лишається на своїх налаштуваннях.
"""
from __future__ import annotations

from django.utils import timezone

from .assistant import key_is_bound
from .models import Shop


def sender_account(shop: Shop):
    acc = shop.mail_sender
    if acc is not None and acc.is_active and acc.smtp_host and acc.email_address:
        return acc
    return None


def public_config(shop: Shop, key=None) -> dict:
    """Відповідь GET /shop/site-mail/ для сайту."""
    from .mail import addresses
    out = {"shop": shop.slug, "managed": False}
    acc = sender_account(shop)
    if not shop.site_mail_managed or acc is None or not key_is_bound(shop, key):
        return out
    security = "ssl" if acc.smtp_use_ssl else ("tls" if acc.smtp_use_tls else "")
    out.update({
        "managed": True,
        "smtp": {"host": acc.smtp_host, "port": int(acc.smtp_port or (465 if security == "ssl" else 587)),
                 "security": security, "user": acc.smtp_username or acc.email_address, "password": acc.smtp_password},
        "from": acc.email_address,
        "from_name": shop.mail_from_name or acc.display_name or shop.name,
        "reply_to": shop.mail_reply_to,
        "bcc": addresses(shop.mail_bcc),   # прихована копія листів покупцям (як у листах Minerva)
        "notify": addresses(shop.team_emails),
        "confirmation": bool(shop.site_order_confirmation),
    })
    return out


def problem(shop: Shop) -> str:
    """Чому сайт не бере пошту з Minerva, хоча увімкнено ('' — усе гаразд)."""
    if not shop.site_mail_managed:
        return ""
    if sender_account(shop) is None:
        return "Не вибрано «Відправник листів покупцям» (активний акаунт пошти з SMTP) — сайт лишається на своїх налаштуваннях."
    return ""


def record_report(shop: Shop, params) -> None:
    """Що в пошті сайту (його файли / /admin/): ?mail_site=…&mail_host=…&mail_user=…&mail_from=…&mail_password=1…"""
    host = str(params.get("mail_site") or "").strip().lower()[:120]
    if not host or not all(c.isalnum() or c in ".-:" for c in host):
        return
    text = lambda k, n=200: str(params.get(k) or "")[:n]  # noqa: E731
    report = {"at": timezone.now().isoformat(timespec="seconds"), "source": text("mail_source", 20),
              "host": text("mail_host"), "user": text("mail_user"), "from": text("mail_from"),
              "password": params.get("mail_password") == "1", "notify": text("mail_notify", 300),
              "confirmation": params.get("mail_confirmation") == "1", "recipient": text("mail_recipient")}
    reports = dict(shop.site_mail_reports or {})
    reports[host] = report
    reports = dict(sorted(reports.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:10])
    Shop.objects.filter(pk=shop.pk).update(site_mail_reports=reports)
    shop.site_mail_reports = reports
