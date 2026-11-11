"""🎙 Наради в шаблонах інших розділів (картка клієнта CRM тощо):

    {% load meetings_tags %}
    {% meetings_can_record as can_rec %}
    {% if can_rec %}<a href="{% url 'admin:meetings_meeting_new' %}?customer={{ original.pk }}">🎙 Записати нараду</a>{% endif %}

can_rec — те саме правило, що й сторінки нарад (services.can_record): активний співробітник із правом на розділ і
записування ввімкнено в «⚙️ Налаштування нарад». Збій — False (кнопки просто немає).
"""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def meetings_can_record(context) -> bool:
    request = context.get("request")
    user = getattr(request, "user", None) or context.get("user")
    try:
        from meetings import services
        return bool(user is not None and services.can_record(user))
    except Exception:  # noqa: BLE001 — немає таблиць (міграції) — без кнопки
        return False
