"""{% side_panel_config cl %} — на сторінці списку, де для моделі є 📌 панель праворуч (core/side_panel.py):
налаштування для static/admin/js/mv_panel.js (json_script «mv-panel-config») і самі скрипт і стилі.
Нічого, якщо панелі немає, людина не бачить список, це вікно вибору (popup) або щось пішло не так."""
import logging

from django import template
from django.urls import reverse
from django.utils.html import format_html, json_script
from django.utils.safestring import mark_safe

register = template.Library()
logger = logging.getLogger(__name__)

ASSETS = ('<link rel="stylesheet" href="/static/admin/css/mv_panel.css?v=6">'
          '<script src="/static/admin/js/mv_panel.js?v=6" defer></script>')


@register.simple_tag(takes_context=True)
def side_panel_config(context, cl):
    request = context.get("request")
    if request is None or cl is None or not getattr(request.user, "is_authenticated", False):
        return ""
    if getattr(cl, "is_popup", False):
        return ""
    from core import side_panel
    try:
        key = side_panel.model_key(cl.opts)
        panel = side_panel.get(key)
        if panel is None:
            return ""
        ma = cl.model_admin
        if not request.user.has_perm(f"{cl.opts.app_label}.view_{cl.opts.model_name}") or \
                not (ma.has_view_permission(request) or ma.has_change_permission(request)):
            return ""
        cfg = side_panel.config(panel, request, url=reverse("side_panel", args=[key, 0]).rsplit("0/", 1)[0],
                                prefs_url=reverse("side_panel_prefs", args=[key]), cl=cl)
    except Exception as exc:  # noqa: BLE001 — панель не ламає сторінку списку
        logger.warning("side panel config for %s failed: %s", getattr(cl, "opts", None), exc)
        return ""
    return format_html("{}{}", json_script(cfg, "mv-panel-config"), mark_safe(ASSETS))
