"""
config/admin_send.py — «Надіслати зараз» у налаштуваннях сповіщень: спершу вікно «Кому».

GET — сторінка: що буде надіслано, «Кому (email)» з адресами з налаштувань (можна змінити, вписати свою,
«лише мені»), Telegram так / ні. POST — надсилає лише вибраним (`dashboard.notifications.send_only_to`);
розклад (last_sent) таке надсилання не зсуває. Види: test (тестовий лист), alerts (критичний склад і
прострочені строки), daily / weekly / monthly (звіти).
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse

from config.models import NotificationSettings

WHAT = {
    "test": ("📧 Тестовий лист", "Перевірка пошти (SMTP): лист без реальних даних."),
    "alerts": ("🔔 Критичний склад і прострочені строки",
               "Minerva перевірить зараз, без огляду на «Мінімальний інтервал», і надішле, якщо є що повідомити."),
    "daily": ("📊 Щоденний звіт", "Звіт за останню добу — той самий, що приходить за розкладом."),
    "weekly": ("📊 Тижневий звіт", "Звіт за 7 повних днів до вчора."),
    "monthly": ("📊 Місячний звіт", "Звіт за попередній повний місяць."),
}


def _back():
    return redirect(reverse("admin:config_notificationsettings_change", args=[1]))


def _telegram_default(ns, what) -> bool:
    if what == "test":
        return False
    if what in ("daily", "weekly", "monthly"):
        return bool(ns.telegram_enabled and ns.digest_telegram)
    return bool(ns.telegram_enabled)


def _preview(ns, what) -> str:
    """Що зараз піде (лише для попереджень — їх можна порахувати без надсилання)."""
    if what != "alerts":
        return ""
    from dashboard.notifications import _get_critical_stock, _get_overdue_orders
    critical = len(_get_critical_stock()) if ns.stock_alerts_enabled else 0
    overdue = len(_get_overdue_orders(ns.deadline_overdue_days)) if ns.deadline_alerts_enabled else 0
    if not critical and not overdue:
        return "Зараз надсилати нічого: критичних товарів і прострочених замовлень немає."
    return f"Буде в листі: критичних залишків — {critical}, прострочених замовлень — {overdue}."


def _send(ns, what, emails, telegram) -> dict:
    from dashboard.digest import send_digest
    from dashboard.notifications import run_alerts, send_only_to
    with send_only_to(emails, telegram):
        if what == "test":
            return run_alerts(is_test=True, test_channel="email")
        if what == "alerts":
            return run_alerts(force=True)
        return send_digest(force=True, period=what)


def _report(request, what, emails, telegram, result):
    channels = []
    email = result.get("email") or {}
    tg = result.get("telegram") or {}
    if email.get("sent"):
        channels.append("email: " + ", ".join(emails))
    if tg.get("sent"):
        channels.append("Telegram")
    errors = [f"{name}: {info['error']}" for name, info in (("Email", email), ("Telegram", tg)) if info.get("error")]
    if result.get("sent"):
        messages.success(request, f"✅ {WHAT[what][0]} — надіслано ({'; '.join(channels)}).")
        for e in errors:
            messages.error(request, f"❌ {e}")
    elif result.get("ok"):
        messages.info(request, f"ℹ️ {result.get('reason', 'Немає що надсилати')}")
    else:
        messages.error(request, "❌ " + ("; ".join(errors) or result.get("error") or result.get("reason") or "?"))


def send_now_view(model_admin, request, what):
    if what not in WHAT:
        raise PermissionDenied
    ns = NotificationSettings.get()
    from dashboard.notifications import MAX_RECIPIENTS, configured_recipients, parse_recipients
    tg_ready = bool(ns.telegram_bot_token and ns.telegram_chat_id)
    text, telegram, error = ", ".join(configured_recipients(ns)), _telegram_default(ns, what) and tg_ready, ""
    if request.method == "POST":
        if not model_admin.has_change_permission(request, ns):
            raise PermissionDenied
        text = request.POST.get("to", "")
        telegram = tg_ready and what != "test" and request.POST.get("telegram") == "1"
        emails, bad = parse_recipients(text)
        if bad:
            error = "Неправильні адреси: " + ", ".join(bad)
        elif len(emails) > MAX_RECIPIENTS:
            error = f"Забагато адрес — не більше {MAX_RECIPIENTS}."
        elif emails and not ns.email_host:
            error = "Пошту (SMTP) не налаштовано — заповніть «Email (SMTP)» у налаштуваннях сповіщень."
        elif not emails and not telegram:
            error = "Кому надсилати? Впишіть адресу або виберіть Telegram."
        else:
            _report(request, what, emails, telegram, _send(ns, what, emails, telegram))
            return _back()
    title, about = WHAT[what]
    return TemplateResponse(request, "admin/config/notificationsettings/send_now.html", {
        **model_admin.admin_site.each_context(request), "opts": NotificationSettings._meta,
        "title": f"Надіслати зараз — {title}", "what": what, "about": about, "preview": _preview(ns, what),
        "to": text, "configured": ", ".join(configured_recipients(ns)), "my_email": request.user.email or "",
        "telegram": telegram, "tg_ready": tg_ready, "tg_allowed": what != "test", "error": error,
        "back_url": reverse("admin:config_notificationsettings_change", args=[1]),
        "can_send": model_admin.has_change_permission(request, ns),
    })
