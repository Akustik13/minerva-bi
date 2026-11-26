"""✉️ Поле «кілька email-адрес» (Кому / Копія) — адреси чипами, тож видно всі, і «＋ З акаунтів»: я, користувачі
Minerva, скриньки пошти, «Кому» загальних сповіщень — додаються одним кліком.

Значення в базі — як і було, «a@x.de, b@y.com», тож код, що читає поле через кому, не змінюється. Перевірка адрес —
`dashboard.notifications.parse_recipients` (кома, крапка з комою, рядки; без повторів). Без JavaScript лишається
звичайне текстове поле. Підключення в адмінці — `formfield_for_dbfield`:

    if db_field.name in ("notify_email_to", "notify_email_cc"):
        return email_list.formfield(db_field, request)
"""
from django import forms
from django.core.exceptions import ValidationError

# Скільки адрес у «＋ З акаунтів» найбільше (мала фірма — кілька десятків; більше — лише пошуком у полі).
SUGGEST_MAX = 200

KIND_ME, KIND_USER, KIND_MAILBOX, KIND_GENERAL = "me", "user", "mailbox", "general"
GROUPS = (
    (KIND_ME, "🙋 Я"),
    (KIND_USER, "👥 Користувачі Minerva"),
    (KIND_MAILBOX, "📬 Скриньки пошти"),
    (KIND_GENERAL, "⚙️ «Кому» загальних сповіщень"),
)

ASSETS_V = 2


def _name(user) -> str:
    return (user.get_full_name() or user.username or "").strip()


def suggestions(user) -> list:
    """Адреси акаунтів для «＋ З акаунтів»: [{email, label, kind}] без повторів (перше джерело перемагає).

    Лише адреси й імена — без листів і налаштувань скриньок."""
    from django.contrib.auth import get_user_model

    found, order = {}, []

    def add(email, label, kind):
        email = (email or "").strip()
        if "@" not in email or len(order) >= SUGGEST_MAX:
            return
        key = email.lower()
        if key in found:
            if label and not found[key]["label"]:
                found[key]["label"] = label
            return
        found[key] = {"email": email, "label": (label or "").strip(), "kind": kind}
        order.append(key)

    if user is not None and getattr(user, "is_authenticated", False):
        add(user.email, _name(user), KIND_ME)
    User = get_user_model()
    for u in (User.objects.filter(is_active=True, is_staff=True).exclude(email="")
              .order_by("first_name", "last_name", "username")[:SUGGEST_MAX]):
        add(u.email, _name(u), KIND_USER)
    try:
        from email_assistant.models import EmailAccount
        for acc in (EmailAccount.objects.filter(is_active=True, user__is_active=True)
                    .select_related("user").order_by("email_address")[:SUGGEST_MAX]):
            add(acc.email_address, acc.display_name or _name(acc.user), KIND_MAILBOX)
    except Exception:       # без застосунку пошти — лише користувачі
        pass
    try:
        from config.models import NotificationSettings
        from dashboard.notifications import configured_recipients
        for email in configured_recipients(NotificationSettings.get()):
            add(email, "", KIND_GENERAL)
    except Exception:
        pass
    return [found[k] for k in order]


def parse(text) -> tuple:
    """(адреси без повторів, неправильні) — те саме правило, що «Надіслати зараз» сповіщень."""
    from dashboard.notifications import parse_recipients
    return parse_recipients(text)


class EmailListWidget(forms.Textarea):
    template_name = "core/widgets/email_list.html"

    class Media:
        css = {"all": (f"/static/admin/css/mv_email_list.css?v={ASSETS_V}",)}
        js = (f"/static/admin/js/mv_email_list.js?v={ASSETS_V}",)

    def __init__(self, attrs=None, suggestions=()):
        base = {"rows": 2, "class": "mvel-raw", "spellcheck": "false", "autocomplete": "off"}
        base.update(attrs or {})
        super().__init__(attrs=base)
        self.suggestions = list(suggestions or ())

    def get_context(self, name, value, attrs):
        ctx = super().get_context(name, value, attrs)
        groups = []
        for kind, title in GROUPS:
            items = [s for s in self.suggestions if s["kind"] == kind]
            if items:
                groups.append({"kind": kind, "title": title, "items": items})
        ctx["widget"]["groups"] = groups
        ctx["widget"]["suggest_count"] = len(self.suggestions)
        return ctx


class EmailListField(forms.CharField):
    """Кілька адрес; збережене значення — «a@x.de, b@y.com» (порожньо — '')."""

    widget = EmailListWidget

    def __init__(self, *, suggestions=(), **kwargs):
        kwargs.pop("max_length", None)
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)
        self.widget.suggestions = list(suggestions or ())

    def clean(self, value):
        value = super().clean(value)
        good, bad = parse(value)
        if bad:
            raise ValidationError(
                "Неправильні адреси: %(bad)s — виправте або приберіть їх.", code="bad_email",
                params={"bad": ", ".join(bad)})
        if self.required and not good:
            raise ValidationError(self.error_messages["required"], code="required")
        return ", ".join(good)


def formfield(db_field, request, **kwargs) -> EmailListField:
    """Поле форми адмінки для текстового поля моделі зі списком адрес (`formfield_for_dbfield`)."""
    sug = getattr(request, "_mv_email_suggestions", None)
    if sug is None:
        sug = suggestions(getattr(request, "user", None))
        try:
            request._mv_email_suggestions = sug
        except Exception:
            pass
    from django.utils.text import capfirst
    kwargs.setdefault("label", capfirst(db_field.verbose_name))
    kwargs.setdefault("help_text", db_field.help_text)
    kwargs.setdefault("required", not db_field.blank)
    return EmailListField(suggestions=sug, **kwargs)
