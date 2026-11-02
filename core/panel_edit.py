"""📌 Панель праворуч (core/side_panel.py): швидкі зміни запису й дії списку для одного запису.

Швидка зміна (`apply_edit`, POST /core/panel/<модель>/<pk>/edit/) іде тим самим шляхом, що «Зберегти» в
картці: `ma.save_form → save_model → save_related → log_change`, тож AuditLog (AuditableMixin), LogEntry
(«🕘 Історія») і сигнали складу / CRM спрацьовують так само. Перевірка — мінімальною формою лише з цим
полем (`modelform_factory` + `ma.formfield_for_dbfield`), а не `ma.get_form()`: форма картки може мати
обов'язкові поля поза моделлю (у товару — базові ціни), і зміна одного поля через неї впала б або затерла
решту. Запис під select_for_update; `expect` — значення, яке людина бачила: хтось змінив раніше — 409
зі свіжою панеллю.

Дії списку (`admin_actions`) — ті самі, що в «Дії» над списком, але лише дозволені людині: правило з
`Panel.admin_perms`, інакше `allowed_permissions` дії (Django вже відсіяв), інакше — право змінювати;
«Видалити обрані» — ніколи (видалення — на сторінці запису).
"""
from __future__ import annotations

import functools
import logging

from django import forms
from django.core.exceptions import FieldError, ObjectDoesNotExist, ValidationError
from django.db import models, transaction
from django.forms import modelform_factory
from django.utils.html import conditional_escape, strip_tags

from core import side_panel as sp

logger = logging.getLogger(__name__)

STALE_TEXT = "Запис щойно змінив інший користувач — показано свіже."
_TRUE = {"1", "true", "on", "yes", "так"}


def _err(status, code, message, **extra):
    return status, {"ok": False, "error": code, "message": message, **extra}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return isinstance(value, str) and value.strip().lower() in _TRUE


def _expect_text(expect) -> str:
    if isinstance(expect, bool):
        return "1" if expect else ""
    return str(expect)


def _same_text(a: str, b: str) -> bool:
    """Значення, яке показала панель, і те, що в базі. Браузер у атрибутах міняє CR / CRLF на LF, тож нотатка
    «Bitte\\r\\nanrufen» (з API чи старої форми) інакше завжди давала б 409."""
    def norm(s):
        return (s or "").replace("\r\n", "\n").replace("\r", "\n")
    return norm(a) == norm(b)


def _form_data(form_cls, edit, value) -> dict:
    name = edit.field
    if edit.kind == "bool":
        ff = form_cls.base_fields.get(name)
        if isinstance(ff, forms.NullBooleanField):
            return {name: "true" if _truthy(value) else "false"}
        return {name: "on"} if _truthy(value) else {}
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "1" if value else ""
    else:
        text = str(value)
    if edit.maxlength:
        text = text[:int(edit.maxlength)]
    return {name: text}


def _errors_text(form) -> str:
    parts = []
    for errors in form.errors.values():
        parts += [str(e) for e in errors]
    return " ".join(parts) or "Значення не підходить."


def _value_label(edit, obj, request) -> str:
    raw = sp.field_value(obj, edit.field)
    text = sp.value_text(raw)
    if edit.kind == "choice":
        label = dict(sp.edit_choices(edit, obj, request)).get(text) or text or "—"
        return sp.split_icon(label)[1] or label
    if edit.kind == "bool":
        return "так" if raw else "ні"
    if not text:
        return "порожньо"
    if edit.kind == "date":
        return sp.date(raw)
    if edit.kind == "number":
        return sp.qty(raw)
    s = " ".join(str(raw).split())
    return s if len(s) <= 60 else s[:59] + "…"


def cell_html(ma, obj, name) -> str:
    """Клітинка рядка списку так, як її малює Django (admin_list.items_for_result), без посилання на картку."""
    from django.contrib.admin.utils import display_for_field, display_for_value, lookup_field
    empty = ma.get_empty_value_display()
    try:
        f, attr, value = lookup_field(name, obj, ma)
    except ObjectDoesNotExist:
        return str(conditional_escape(empty))
    empty = getattr(attr, "empty_value_display", empty)
    if isinstance(value, str) and value.strip() == "":
        value = ""
    if f is None or f.auto_created:
        boolean = getattr(attr, "boolean", False)
        if isinstance(attr, property) and hasattr(attr, "fget"):
            boolean = getattr(attr.fget, "boolean", False)
        rep = display_for_value(value, empty, boolean)
    elif isinstance(f.remote_field, models.ManyToOneRel):
        rel = getattr(obj, f.name)
        rep = empty if rel is None else rel
    else:
        rep = display_for_field(value, f, empty)
    return str(conditional_escape(rep))


def _cells(edit, ma, request, obj) -> dict:
    out = {}
    try:
        names = [n for n in ma.get_list_display(request) if isinstance(n, str)]
    except Exception:  # noqa: BLE001
        return out
    for name in edit.cells:
        if name not in names:
            continue
        try:
            out[name] = cell_html(ma, obj, name)
        except Exception as exc:  # noqa: BLE001 — клітинку не перемальовано, рядок оновиться з наступним списком
            logger.info("side panel cell %s skipped: %s", name, exc)
    return out


def apply_edit(request, panel, ma, pk, data) -> tuple:
    """Швидка зміна одного поля: (HTTP-статус, JSON). Порядок перевірок — як у картці, плюс `expect`."""
    if not isinstance(data, dict):
        return _err(400, "bad_json", "Не вдалося прочитати запит.")
    key = data.get("key")
    edit = panel.edit(key) if isinstance(key, str) else None
    if edit is None:
        return _err(404, "not_found", "Такої швидкої зміни немає.")
    try:
        visible = ma.get_queryset(request).filter(pk=pk).exists()
    except (ValueError, TypeError, ValidationError):
        visible = False
    if not visible:
        return _err(404, "not_found", "Запис не знайдено або він для вас закритий.")
    value, expect = data.get("value"), data.get("expect")
    stale = changed = False
    old = ""
    try:
        with transaction.atomic():
            try:                                    # звичайний менеджер: у queryset адмінки бувають outer join
                obj = ma.model._default_manager.select_for_update(of=("self",)).get(pk=pk)
            except ma.model.DoesNotExist:
                return _err(404, "not_found", "Запис не знайдено або він для вас закритий.")
            if not (sp.can_change(request, ma, obj) and sp.allowed(edit.perm, request, obj)):
                return _err(403, "forbidden", "Немає права змінювати цей запис.")
            if not sp.field_editable(panel, ma, request, obj, edit.field):
                return _err(400, "not_editable", "Це поле тут змінити не можна — відкрийте картку.")
            old = sp.value_text(sp.field_value(obj, edit.field))
            if expect is not None and not _same_text(_expect_text(expect), old):
                stale = True
            else:
                if edit.choices is not None:
                    values = {v for v, _label in sp.edit_choices(edit, obj, request)}
                    if sp.value_text(value) not in values:
                        return _err(400, "bad_value", "Такого значення тут немає.")
                try:
                    form_cls = modelform_factory(
                        ma.model, fields=[edit.field],
                        formfield_callback=functools.partial(ma.formfield_for_dbfield, request=request))
                except FieldError:
                    return _err(400, "not_editable", "Це поле тут змінити не можна — відкрийте картку.")
                form = form_cls(_form_data(form_cls, edit, value), instance=obj)
                try:
                    valid = form.is_valid()
                except (ValueError, ValidationError) as exc:
                    return _err(400, "invalid", f"Не вдалося зберегти: {strip_tags(str(exc))}")
                if not valid:
                    return _err(400, "invalid", _errors_text(form))
                changed = form.has_changed()
                if changed:
                    new = ma.save_form(request, form, change=True)
                    ma.save_model(request, new, form, True)
                    ma.save_related(request, form, [], True)
                    ma.log_change(request, new, ma.construct_change_message(request, form, []))
                    obj = new
    except Exception as exc:  # noqa: BLE001 — зміну відкочено; людина бачить зрозуміле повідомлення
        logger.warning("side panel edit %s.%s pk=%s failed: %s", panel.model, edit.key, pk, exc)
        return _err(400, "invalid", "Не вдалося зберегти — спробуйте в картці.")
    display = ma.get_queryset(request).filter(pk=pk).first() or obj
    if stale:
        return 409, {"ok": False, "error": "stale", "message": STALE_TEXT, "html": sp.render(panel, display, request)}
    p = sp._prefs_for(panel, request)
    blocks = {}
    shown = set(p.get("blocks") or ())
    for b in sp.visible_blocks(panel, request):
        if edit.key in (b.affects or ()) and b.key in shown:
            blocks[b.key] = sp.render_block(panel, b.key, display, request)
    label = sp.split_icon(edit.label)[1] or str(edit.label)
    new_text = sp.value_text(sp.field_value(display, edit.field))
    return 200, {
        "ok": True,
        "message": f"{label}: {_value_label(edit, display, request)}" if changed else "Без змін.",
        "value": new_text,
        "head": sp.render_head(panel, display, request, p),
        "blocks": blocks,
        "cells": _cells(edit, ma, request, display),
        "undo": {"key": edit.key, "value": old} if edit.undo and changed else None,
    }


def admin_actions(panel, ma, request) -> list:
    """Дії списку для одного запису: [{name, label}] — лише ті, що людині дозволено (без «Видалити обрані»)."""
    if ma is None:
        return []
    try:
        actions = ma.get_actions(request)
        choices = ma.get_action_choices(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning("side panel admin actions %s failed: %s", ma.opts.label_lower, exc)
        return []
    rules = getattr(panel, "admin_perms", None) or {}
    out = []
    for name, label in choices:
        if not name or name == "delete_selected":
            continue
        func = (actions.get(name) or (None,))[0]
        if name in rules:
            ok = sp.allowed(rules[name], request)
        elif func is not None and getattr(func, "allowed_permissions", None):
            ok = True                               # Django уже перевірив has_<право>_permission
        else:
            try:
                ok = bool(ma.has_change_permission(request))
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            out.append({"name": str(name), "label": str(label)})
    return out
