"""Адмінка «🔗 Посилання на фото й даташити за правилом» (логіка — inventory/services/media_links.py):
панель у Налаштуваннях складу, сторінка перевірки зі звітом, дії списку товарів."""
import re

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html, format_html_join

from .models import InventorySettings, Product
from .services import media_links as ml

ACTION_COLOR = {ml.SAME: "#90a4ae", ml.FILL: "#81c784", ml.REPLACE: "#64b5f6", ml.KEEP: "#ffb74d", ml.BROKEN: "#e57373"}


def page_url(ids=None) -> str:
    url = reverse("admin:inventory_inventorysettings_media_links")
    return url + ("?ids=" + ",".join(str(i) for i in ids) if ids else "")


def settings_panel(obj) -> str:
    """Налаштування складу: приклад посилань за правилом + кнопка на сторінку перевірки."""
    lines = []
    for kind, label in ((ml.DATASHEET, "📄 Даташит"), (ml.IMAGE, "🖼 Фото")):
        url = ml.example(kind)
        lines.append(format_html('<div>{}: {}</div>', label,
                                 format_html('<code>{}</code>', url) if url else
                                 format_html('<span style="opacity:.7">правило не задано</span>')))
    groups = ml.shared_groups()
    shared = sum(len(v) for v in groups.values())
    return format_html(
        '<div style="font-size:12px">{}<div style="margin:6px 0;opacity:.8">Приклад для першого товару. Спільних файлів '
        '(кілька товарів — одне фото чи даташит): {}.</div>'
        '<a class="button" href="{}">🔗 Сформувати й перевірити посилання →</a>'
        '<div class="help" style="margin-top:6px">Спершу збережіть правило. На сторінці видно для кожного товару, '
        'яке посилання буде, чи воно відповідає, і що зміниться; записуються лише вибрані рядки.</div></div>',
        format_html_join("", "{}", ((x,) for x in lines)), shared, page_url())


def _ids(raw) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", str(raw or ""))][:2000]


def page_view(model_admin, request):
    """GET — сторінка; POST start — запустити перевірку; POST apply — записати вибрані."""
    if not request.user.has_perm("inventory.view_product"):
        raise PermissionDenied
    ids = _ids(request.GET.get("ids") or request.POST.get("ids"))
    if request.method == "POST":
        if not request.user.has_perm("inventory.change_product"):
            raise PermissionDenied
        if "apply" in request.POST:
            n, warnings = ml.apply(request.POST.getlist("row"), request.user)
            messages.success(request, f"Посилання записано: {n} товарів. «🖼 Фото й даташити» магазину перевірить їх "
                                      "як змінені (автоматично, якщо ввімкнено).")
            for w in warnings:
                messages.warning(request, w)
            if n:
                try:
                    from shop import media
                    if media.settings_obj().enabled:
                        media.start("changed", by=request.user.get_username())
                except Exception:  # noqa: BLE001 — магазин не обов'язковий
                    pass
        else:
            scope = request.POST.get("scope") if request.POST.get("scope") in ("all", "shop", "selected") else "all"
            kinds = [k for k in request.POST.getlist("kind") if k in ml.KINDS] or list(ml.KINDS)
            if ml.start(scope, ids if scope == "selected" else None, kinds, bool(request.POST.get("check")),
                        by=request.user.get_username()):
                messages.info(request, "Перевірку запущено — прогрес на сторінці.")
            else:
                messages.warning(request, "Перевірка вже йде — дочекайтесь її завершення.")
        return redirect(page_url(ids))
    state = ml.progress()
    finished = parse_datetime(state.get("finished_at") or "") if state.get("finished_at") else None
    state["finished_text"] = timezone.localtime(finished).strftime("%d.%m.%Y %H:%M") if finished else ""
    rows = list(ml.check_obj().rows or [])
    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        r["action_text"] = ml.ACTION_TEXT.get(r["action"], r["action"])
        r["color"] = ACTION_COLOR.get(r["action"], "#90a4ae")
        r["kind_label"] = "🖼 фото" if r["kind"] == ml.IMAGE else "📄 даташит"
        r["url"] = reverse("admin:inventory_product_change", args=[r["id"]])
    tpl = ml.templates()
    return render(request, "admin/inventory/inventorysettings/media_links.html", {
        **model_admin.admin_site.each_context(request),
        "title": "Посилання на фото й даташити за правилом", "opts": InventorySettings._meta,
        "state": state, "rows": rows,
        "counts": [(a, ml.ACTION_TEXT[a], counts.get(a, 0), ACTION_COLOR[a]) for a in
                   (ml.FILL, ml.REPLACE, ml.KEEP, ml.BROKEN, ml.SAME)],
        "templates": [(ml.DATASHEET, "📄 Даташит", tpl[ml.DATASHEET], ml.example(ml.DATASHEET)),
                      (ml.IMAGE, "🖼 Фото", tpl[ml.IMAGE], ml.example(ml.IMAGE))],
        "ids": ",".join(str(i) for i in ids), "n_selected": len(ids),
        "n_all": ml.scope_products("all").count(), "n_shop": ml.scope_products("shop").count(),
        "groups": [(("🖼 фото" if k == ml.IMAGE else "📄 даташит"), g) for k, g in ml.shared_groups().items() if g],
        "settings_url": reverse("admin:inventory_inventorysettings_change", args=[1]),
        "status_url": reverse("admin:inventory_inventorysettings_media_links_status"),
        "can_change": request.user.has_perm("inventory.change_product"),
    })


def status_view(model_admin, request):
    if not request.user.has_perm("inventory.view_product"):
        raise PermissionDenied
    s = ml.progress()
    return JsonResponse({k: s.get(k) for k in ("state", "total", "done", "current", "started_at", "finished_at",
                                                "message", "counts")})


# ── Дії списку товарів ────────────────────────────────────────────────────────

def shared_form_class(products):
    """Форма дії «🖼 Спільне фото / даташит»: файл одного з вибраних товарів або своя назва."""
    choices = [(p.sku, p.sku) for p in products[:200]]
    wide = forms.URLInput(attrs={"style": "width:100%;min-width:420px;max-width:680px"})

    class SharedMediaForm(forms.Form):
        image_link = forms.URLField(
            label="🖼 Готове посилання на фото", required=False, max_length=500, assume_scheme="https", widget=wide,
            help_text="Однакове для всіх вибраних, напр. https://docs.sevskiy.com/pictures/download.php?file=CA-MHF1-MC1.13-XXX-MHF4.png")
        datasheet_link = forms.URLField(label="📄 Готове посилання на даташит", required=False, max_length=500,
                                        assume_scheme="https", widget=wide)
        kinds = forms.MultipleChoiceField(
            label="…або спільний файл за правилом для", widget=forms.CheckboxSelectMultiple, required=False,
            choices=[(ml.IMAGE, "🖼 Фото"), (ml.DATASHEET, "📄 Даташит")],
            help_text="Посилання збере правило з Налаштувань складу. Вид, для якого вписано готове посилання, тут не діє.")
        key = forms.ChoiceField(label="Файл якого товару", required=False, widget=forms.RadioSelect,
                                choices=choices, initial=choices[0][0] if choices else None)
        custom = forms.CharField(label="…або назва файлу", required=False, max_length=120,
                                 help_text="Напр. AN050201-01C-175-MHF1 — тоді Minerva пробує .jpg, .png … і бере те, що є; "
                                           "з розширенням (X.png) — береться як є. Має пріоритет над вибором вище.")

        def clean(self):
            d = super().clean()
            d["links"] = {k: d[f] for k, f in ((ml.IMAGE, "image_link"), (ml.DATASHEET, "datasheet_link")) if d.get(f)}
            d["key_kinds"] = [k for k in d.get("kinds") or [] if k not in d["links"]]
            d["final"] = (d.get("custom") or "").strip() or d.get("key") or ""
            if not d["links"] and not d["key_kinds"]:
                raise forms.ValidationError("Вставте готове посилання або позначте «Фото» / «Даташит» для спільного файлу.")
            if d["key_kinds"] and not d["final"]:
                raise forms.ValidationError("Оберіть товар або вкажіть назву файлу.")
            if d["key_kinds"] and not re.fullmatch(r"[\w.\-()~ ]{1,120}", d["final"]):
                raise forms.ValidationError("Назва файлу — літери, цифри, «-», «_», «.»")
            return d
    return SharedMediaForm


def link_result_text(kind, count, url, error, by_rule) -> tuple[int, str]:
    label = "фото" if kind == ml.IMAGE else "даташит"
    note = "" if by_rule else (" Посилання не за правилом Налаштувань складу — у «Сформувати й перевірити» не записуйте "
                               "для цих товарів рядки «замінити», якщо хочете лишити його.")
    if error:
        return messages.WARNING, f"Спільне посилання на {label} для {count} товарів: {url} — ⚠ {error}.{note}"
    return messages.SUCCESS, f"Спільне посилання на {label} для {count} товарів: {url} — ✅ відповідає.{note}"


def shared_result_text(key, count, result) -> list[tuple[int, str]]:
    out = []
    for kind, (url, error) in result.items():
        label = "фото" if kind == ml.IMAGE else "даташит"
        if url is None or not url:
            out.append((messages.WARNING, f"Спільний {label} «{key}» для {count} товарів збережено; правило посилання "
                                          f"не задано (Налаштування складу) — посилання не змінено."))
        elif error:
            out.append((messages.WARNING, f"Спільний {label} «{key}» для {count} товарів: {url} — ⚠ {error}."))
        else:
            out.append((messages.SUCCESS, f"Спільний {label} «{key}» для {count} товарів: {url} — ✅ відповідає."))
    return out
