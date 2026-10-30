"""📌 Панель праворуч у списках: клік по рядку — подробиці запису поруч, без переходу на картку.

Модель реєструє блоки й швидкі дії (`register`, модулі в `PROVIDERS` — напр. sales/side_panel.py); людина сама
вибирає, які блоки й у якому порядку, які дії, ширину й чи панель увімкнена — правий клік / ⚙ на панелі
(static/admin/js/mv_panel.js). Вибір — у профілі (`UserProfile.ui_prefs["panels"][модель]`, core/ui_prefs.py поруч),
однаково на всіх пристроях. Запис для панелі береться з queryset адмінки моделі — ті самі права й обмеження,
що в списку; блок, який упав, показує «не вдалося», решта панелі працює.
"""
from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

logger = logging.getLogger(__name__)

PROVIDERS = {"sales.salesorder": "sales.side_panel"}
WIDTHS = (340, 400, 480, 560)
DEFAULT_WIDTH = 400


@dataclass
class Block:
    key: str
    title: str
    render: Callable                      # (obj, request) -> безпечний HTML (format_html)
    default: bool = True


@dataclass
class Action:
    key: str
    label: str
    href: Callable                        # (obj) -> адреса або "" (дія недоступна для цього запису)
    default: bool = True
    new_tab: bool = False


@dataclass
class Panel:
    model: str
    title: Callable                       # (obj) -> заголовок (безпечний HTML)
    blocks: list = field(default_factory=list)
    actions: list = field(default_factory=list)

    def block(self, key):
        return next((b for b in self.blocks if b.key == key), None)

    def action(self, key):
        return next((a for a in self.actions if a.key == key), None)


_registry: dict[str, Panel] = {}


def register(model: str, *, title, blocks, actions) -> Panel:
    _registry[model] = Panel(model, title, list(blocks), list(actions))
    return _registry[model]


def get(model: str) -> Panel | None:
    if model not in _registry and model in PROVIDERS:
        importlib.import_module(PROVIDERS[model])
    return _registry.get(model)


def model_key(opts) -> str:
    return f"{opts.app_label}.{opts.model_name}"


# ── вибір людини ──

def _profile(user):
    try:
        return user.profile
    except Exception:  # noqa: BLE001
        return None


def defaults(panel: Panel) -> dict:
    return {"on": True, "blocks": [b.key for b in panel.blocks if b.default],
            "actions": [a.key for a in panel.actions if a.default], "width": DEFAULT_WIDTH}


def clean(panel: Panel, data) -> dict:
    """Лише відомі блоки й дії, без повторів; ширина — з WIDTHS."""
    base = defaults(panel)
    if not isinstance(data, dict):
        return base
    out = dict(base)
    if isinstance(data.get("on"), bool):
        out["on"] = data["on"]
    for name, known in (("blocks", {b.key for b in panel.blocks}), ("actions", {a.key for a in panel.actions})):
        if isinstance(data.get(name), list):
            seen = []
            for k in data[name]:
                if isinstance(k, str) and k in known and k not in seen:
                    seen.append(k)
            out[name] = seen
    try:
        w = int(data.get("width", base["width"]))
        out["width"] = w if w in WIDTHS else base["width"]
    except (TypeError, ValueError):
        pass
    return out


def prefs(user, panel: Panel) -> dict:
    p = _profile(user)
    saved = ((getattr(p, "ui_prefs", None) or {}).get("panels") or {}).get(panel.model) if p else None
    return clean(panel, saved) if saved else defaults(panel)


def save_prefs(user, panel: Panel, data) -> dict:
    p = _profile(user)
    new = clean(panel, data)
    if p is not None:
        ui = dict(p.ui_prefs or {})
        panels = dict(ui.get("panels") or {})
        panels[panel.model] = new
        ui["panels"] = panels
        p.ui_prefs = ui
        p.save(update_fields=["ui_prefs"])
    return new


def config(panel: Panel, user, url: str, prefs_url: str) -> str:
    """JSON для сторінки списку (json_script у шаблоні)."""
    return json.dumps({
        "model": panel.model, "url": url, "prefsUrl": prefs_url, "prefs": prefs(user, panel),
        "widths": list(WIDTHS),
        "blocks": [{"key": b.key, "title": b.title} for b in panel.blocks],
        "actions": [{"key": a.key, "label": a.label} for a in panel.actions],
    }, ensure_ascii=False)


# ── HTML панелі ──

def render(panel: Panel, obj, request, p: dict | None = None) -> str:
    p = p or prefs(request.user, panel)
    parts = [format_html(
        '<div class="mvp-head"><div class="mvp-title">{}</div>'
        '<button type="button" class="mvp-icon" data-mvp-menu title="Налаштувати панель: блоки, швидкі дії, '
        'ширина (або правий клік)" aria-label="Налаштувати панель">⚙</button>'
        '<button type="button" class="mvp-icon" data-mvp-close title="Закрити (Esc)" aria-label="Закрити">✕</button>'
        '</div>', panel.title(obj))]
    body = []
    for key in p["blocks"]:
        block = panel.block(key)
        if block is None:
            continue
        try:
            inner = block.render(obj, request)
        except Exception as exc:  # noqa: BLE001 — один блок не ламає панель
            logger.warning("side panel block %s.%s failed: %s", panel.model, key, exc)
            inner = format_html('<div class="mvp-empty">Не вдалося показати цей блок.</div>')
        if inner:
            body.append(format_html('<section class="mvp-block" data-block="{}"><h3>{}</h3>{}</section>',
                                    key, block.title, inner))
    if not body:
        body.append(format_html('<div class="mvp-empty">Блоків немає — правий клік → «➕ Додати блок».</div>'))
    parts.append(format_html('<div class="mvp-body">{}</div>', mark_safe("".join(body))))
    acts = []
    for key in p["actions"]:
        a = panel.action(key)
        href = a.href(obj) if a else ""
        if a and href:
            acts.append((href, ' target="_blank" rel="noopener"' if a.new_tab else "", a.label))
    if acts:
        parts.append(format_html('<div class="mvp-actions">{}</div>', format_html_join(
            "", '<a class="mvp-act" href="{}"{}>{}</a>', ((h, mark_safe(t), label) for h, t, label in acts))))
    return "".join(parts)
