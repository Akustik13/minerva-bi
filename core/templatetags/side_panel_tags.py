"""{% side_panel_config cl %} — на сторінці списку, де для моделі є 📌 панель праворуч (core/side_panel.py):
налаштування для static/admin/js/mv_panel.js. Нічого, якщо панелі немає або людина не бачить список."""
from django import template
from django.urls import reverse
from django.utils.html import format_html, json_script

register = template.Library()


@register.simple_tag(takes_context=True)
def side_panel_config(context, cl):
    request = context.get("request")
    if request is None or cl is None or not getattr(request.user, "is_authenticated", False):
        return ""
    from core import side_panel
    key = side_panel.model_key(cl.opts)
    panel = side_panel.get(key)
    if panel is None:
        return ""
    ma = cl.model_admin
    if not request.user.has_perm(f"{cl.opts.app_label}.view_{cl.opts.model_name}") or             not (ma.has_view_permission(request) or ma.has_change_permission(request)):
        return ""
    import json
    cfg = json.loads(side_panel.config(panel, request.user, reverse("side_panel", args=[key, 0]).rsplit("0/", 1)[0],
                                       reverse("side_panel_prefs", args=[key])))
    return format_html('{}<link rel="stylesheet" href="/static/admin/css/mv_panel.css?v=1">'
                       '<script src="/static/admin/js/mv_panel.js?v=1" defer></script>',
                       json_script(cfg, "mv-panel-config"))
