"""🖼 Фото й даташити на DigiKey — сторінка звірки (`…/digikeylisting/media/`), блок у налаштуваннях DigiKey і
підказка «за правилом» у картці лістингу. Логіка — bots/services/dk_media.py.

GET — лістинги з посиланнями в Minerva й за правилом (без мережі); «🔍 Прочитати з DigiKey і перевірити» —
у фоні (`dk_media.start("check")`), хід — `media/status/`, звіт — зі стану; вибрані рядки → «✓ Записати в картки»
(+ «📤 одразу на DigiKey» для вже створених на DigiKey товарів — `dk_media.start("push")`).
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from bots.models import DigiKeyConfig, DigiKeyListing
from bots.services import dk_media

LIST_LIMIT = 300


def page_url(ids=None) -> str:
    url = reverse("admin:bots_digikeylisting_media")
    return url + ("?ids=" + ",".join(str(i) for i in ids) if ids else "")


def _ids(request) -> list:
    raw = ",".join(request.POST.getlist("ids") or request.GET.getlist("ids"))
    return [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()]


def _listings(request):
    qs = DigiKeyListing.objects.select_related("product").order_by("product__sku", "pk")
    ids = _ids(request)
    if ids:
        qs = qs.filter(pk__in=ids)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(product__sku__icontains=q) | Q(dk_title__icontains=q) | Q(dk_supplier_sku__icontains=q))
    if request.GET.get("published") == "1":
        qs = qs.exclude(dk_product_id="")
    return qs, ids


def media_view(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    can_change = model_admin.has_change_permission(request)
    qs, ids = _listings(request)
    listings = list(qs[:LIST_LIMIT])
    who = request.user.get_username()
    if request.method == "POST":
        if not can_change:
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "check":
            if dk_media.start("check", [l.pk for l in listings], who):
                messages.info(request, f"Читаю товари з DigiKey і перевіряю посилання {len(listings)} лістингів — "
                                       "хід нижче, звіт з'явиться сам.")
            else:
                messages.warning(request, "Уже йде перевірка чи надсилання — дочекайтеся кінця.")
        elif action == "apply":
            picks = dk_media.parse_picks(request.POST.getlist("pick"))
            if not picks:
                messages.warning(request, "Позначте рядки галочкою.")
            else:
                changed, touched = dk_media.apply(picks)
                messages.success(request, f"Записано в картки лістингів: {changed} посилань.")
                if request.POST.get("push") == "1" and touched:
                    if dk_media.start("push", touched, who):
                        messages.info(request, f"Надсилаю картки {len(touched)} товарів на DigiKey — DigiKey перевіряє "
                                               "зміни товару окремо (Products → Pending).")
                    else:
                        messages.warning(request, "Уже йде перевірка чи надсилання — надішліть пізніше.")
        return redirect(request.get_full_path())
    st = dk_media.state()
    report = {}
    if st.get("mode") == "check" and not st.get("running"):
        report = {r["pk"]: r for r in st.get("rows") or []}
    rows = []
    for r in dk_media.rows(listings):
        rep = report.get(r["listing"].pk)
        if rep:                                    # звіт перевірки — для цього лістингу
            r["items"], r["found"], r["checked"] = rep["items"], rep["found"], True
        rows.append(r)
    sheet = dk_media.datasheet_field()
    differ = sum(1 for r in rows for it in r["items"] if it["action"] not in (dk_media.SAME, dk_media.NONE))
    return TemplateResponse(request, "admin/bots/digikeylisting/media.html", {
        **model_admin.admin_site.each_context(request), "opts": DigiKeyListing._meta,
        "title": "🖼 Фото й даташити на DigiKey", "rows": rows, "state": st, "can_change": can_change,
        "q": request.GET.get("q") or "", "ids": ",".join(map(str, ids)), "limit": LIST_LIMIT, "total": qs.count(),
        "published_only": request.GET.get("published") == "1", "sheet": sheet, "differ": differ,
        "source": dict(DigiKeyConfig.MEDIA_CHOICES).get(dk_media.config().media_source, ""),
        "settings_url": reverse("admin:bots_digikeyconfig_changelist") + "#dk-media",
        "has_report": bool(report),
    })


def media_status_view(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    st = dict(dk_media.state())
    st.pop("rows", None)
    return JsonResponse(st)


# ── Налаштування DigiKey і картка лістингу ────────────────────────────────────

def config_panel(cfg) -> str:
    """«Посилання за правилом» у налаштуваннях DigiKey: приклад, поле даташита DigiKey, перехід до звірки."""
    ex = dk_media.example(cfg)
    lines = format_html_join("", '<div>{} {}: <code>{}</code> <span style="color:var(--text-muted)">{}</span></div>',
                             (("🖼" if k == "image" else "📄", dk_media.LABEL[k].capitalize(), ex[k][0] or "—",
                               f"({ex[k][1]})" if ex[k][1] else "") for k in dk_media.KINDS))
    sheet = dk_media.datasheet_field()
    sheet_text = (format_html("Даташит DigiKey приймає в полі «{}» ({}) — Minerva надсилає його разом з карткою.",
                              sheet["name"], sheet["code"]) if sheet else
                  format_html("Поля даташита DigiKey Minerva не знає — даташит лишається в Minerva. "
                              "Запитайте поля: «🔷 Зі складу на DigiKey» → «🔄 Запитати поля в DigiKey»."))
    return format_html(
        '<div id="dk-media">{}<p style="margin:6px 0 0">Приклад для першого лістингу. {}</p>'
        '<p style="margin:8px 0 0"><a class="button" href="{}">🔗 Звірити фото й даташити на DigiKey →</a></p></div>',
        lines, sheet_text, page_url())


def listing_panel(listing) -> str:
    """У картці лістингу під «Фото» й «Datasheet»: що за правилом, «⇢ підставити», звірка з DigiKey."""
    if listing is None or not listing.pk:
        return "—"
    items = []
    for kind in dk_media.KINDS:
        found, how = dk_media.candidates(listing.product, kind, listing.get_supplier_sku())
        url = dk_media.target(listing.product, kind, listing.get_supplier_sku())
        field = dk_media.FIELD[kind]
        current = (getattr(listing, field) or "").strip()
        if not url:
            items.append(format_html('<div>{}: <span style="color:var(--text-muted)">за правилом — немає{}</span></div>',
                                     dk_media.LABEL[kind].capitalize(),
                                     " (довше 200 символів)" if found else ""))
        elif url == current:
            items.append(format_html('<div>{}: ✓ як за правилом <span style="color:var(--text-muted)">({})</span></div>',
                                     dk_media.LABEL[kind].capitalize(), how))
        else:
            items.append(format_html(
                '<div>{}: за правилом <code>{}</code> <span style="color:var(--text-muted)">({})</span> '
                '<button type="button" class="button dkm-fill" data-field="id_{}" data-url="{}">⇢ підставити</button></div>',
                dk_media.LABEL[kind].capitalize(), url, how, field, url))
    return format_html(
        '{}<div style="margin-top:6px"><a href="{}">🖼 Звірити з DigiKey…</a> · <a href="{}">⚙ правило</a></div>'
        '<script>document.addEventListener("click",function(e){{var b=e.target.closest(".dkm-fill");if(!b)return;'
        # картка лістингу в режимі перегляду — спершу «Редагувати» (поля приховані)
        'if(typeof dkEnterEdit==="function"&&document.body.classList.contains("dk-view"))dkEnterEdit();'
        'var i=document.getElementById(b.dataset.field);if(i){{i.value=b.dataset.url;i.dispatchEvent(new Event("input"));'
        'b.textContent="✓ підставлено — збережіть";}}}});</script>',
        format_html_join("", "{}", ((i,) for i in items)), page_url([listing.pk]),
        reverse("admin:bots_digikeyconfig_changelist") + "#dk-media")
