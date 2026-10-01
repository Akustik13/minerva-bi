"""
shipping/views_packing_list.py — Packing Lists з власного Word-шаблону.
"""
import json
import logging
from datetime import date

from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from shipping.models import PackingList, Shipment
from shipping.services import packing_list_service as svc

logger = logging.getLogger(__name__)

SHIP_TO_KEYS = ("company", "contact", "street", "city_line", "country", "phone", "email")


def _staff(view_fn):
    return staff_member_required(view_fn, login_url="/admin/login/")


def _ctx(request, **kw):
    return {**admin.site.each_context(request), **kw}


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_parcels(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    parcels = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue
        lines = []
        for ln in p.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            desc = str(ln.get("description") or "").strip()
            pn   = str(ln.get("part_no") or "").strip()
            if not (desc or pn):
                continue
            lines.append({
                "description": desc[:200], "part_no": pn[:80],
                "qty": _num(ln.get("qty")), "unit_net_kg": _num(ln.get("unit_net_kg")),
            })
        parcels.append({
            "dims": str(p.get("dims") or "").strip()[:40],
            "gross_kg": _num(p.get("gross_kg")),
            "lines": lines,
        })
    return parcels


def _form_data(request) -> dict:
    P = request.POST
    try:
        pl_date = date.fromisoformat(P.get("pl_date", ""))
    except ValueError:
        pl_date = date.today()
    return {
        "number":          P.get("number", "").strip()[:30],
        "pl_date":         pl_date,
        "ship_to":         {k: P.get(f"ship_to_{k}", "").strip() for k in SHIP_TO_KEYS},
        "parcels":         _parse_parcels(P.get("parcels_json")),
        "signer_name":     P.get("signer_name", "").strip()[:120],
        "signer_position": P.get("signer_position", "").strip()[:120],
    }


def _render_form(request, data, order=None, pl=None):
    return render(request, "admin/shipping/packing_list_form.html", _ctx(
        request,
        title=f"Packing List #{pl.number}" if pl else "Новий Packing List",
        data=data, order=order, pl=pl,
        parcels=data["parcels"],
        ship_to_keys=SHIP_TO_KEYS,
    ))


def _save_and_generate(request, pl, data):
    pl.number          = data["number"] or (pl.sales_order.order_number if pl.sales_order else "")
    pl.pl_date         = data["pl_date"]
    pl.ship_to         = data["ship_to"]
    pl.parcels         = data["parcels"]
    pl.signer_name     = data["signer_name"]
    pl.signer_position = data["signer_position"]
    if not pl.pk:
        pl.created_by = request.user
    pl.save()
    try:
        svc.generate_files(pl)
    except Exception as e:
        logger.exception("Packing list generation failed")
        messages.error(request, f"❌ Помилка генерації: {e}")
        return False
    msg = f"✅ Packing List #{pl.number} згенеровано"
    msg += " (DOCX + PDF)." if pl.pdf_file else " (DOCX; PDF недоступний — немає LibreOffice)."
    if pl.sales_order:
        msg += " Копія збережена в «Документи замовлення»."
    messages.success(request, msg)
    return True


@_staff
def pl_list(request):
    from sales.models import SalesOrder
    q = request.GET.get("order", "").strip()
    if q:
        order = SalesOrder.objects.filter(order_number=q).first() or \
                SalesOrder.objects.filter(order_number__icontains=q).order_by("-order_date").first()
        if order:
            return redirect(f"/packing-lists/new/?order={order.pk}")
        messages.warning(request, f"Замовлення «{q}» не знайдено.")

    return render(request, "admin/shipping/packing_list_list.html", _ctx(
        request,
        title="Packing Lists",
        packing_lists=PackingList.objects.select_related("sales_order", "created_by")[:300],
        template_vars=svc.PL_TEMPLATE_VARS,
        has_custom_template=svc.has_custom_template(),
    ))


@_staff
def pl_new(request):
    from sales.models import SalesOrder
    order = get_object_or_404(SalesOrder, pk=request.GET.get("order") or request.POST.get("order_id"))

    if request.method == "POST":
        data = _form_data(request)
        shipment = Shipment.objects.filter(pk=request.POST.get("shipment_id") or None, order=order).first()
        pl = PackingList(sales_order=order, shipment=shipment)
        if _save_and_generate(request, pl, data):
            return redirect("/packing-lists/")
        return redirect(f"/packing-lists/{pl.pk}/edit/")

    data = svc.initial_data(order)
    existing = order.packing_lists.order_by("-created_at").first()
    if existing:
        messages.info(request, f"ℹ️ Для цього замовлення вже є Packing List #{existing.number} — "
                               f"можна редагувати його у списку. Тут створюється новий.")
    return _render_form(request, data, order=order)


@_staff
def pl_edit(request, pk):
    pl = get_object_or_404(PackingList, pk=pk)
    if request.method == "POST":
        if _save_and_generate(request, pl, _form_data(request)):
            return redirect("/packing-lists/")
        return redirect(f"/packing-lists/{pl.pk}/edit/")
    data = {
        "number": pl.number, "pl_date": pl.pl_date, "ship_to": pl.ship_to, "parcels": pl.parcels,
        "signer_name": pl.signer_name, "signer_position": pl.signer_position, "shipment": pl.shipment,
    }
    return _render_form(request, data, order=pl.sales_order, pl=pl)


@_staff
def pl_file(request, pk, kind):
    if kind not in ("pdf", "docx"):
        raise Http404
    pl = get_object_or_404(PackingList, pk=pk)
    f = pl.pdf_file if kind == "pdf" else pl.docx_file
    if not f:
        raise Http404("Файл не згенеровано")
    return FileResponse(f.open("rb"), as_attachment=kind != "pdf",
                        filename=f"Packing List_{pl.number}.{kind}")


@_staff
@require_POST
def pl_delete(request, pk):
    pl = get_object_or_404(PackingList, pk=pk)
    for f in (pl.docx_file, pl.pdf_file):
        if f:
            f.delete(save=False)
    num = pl.number
    pl.delete()
    messages.success(request, f"🗑️ Packing List #{num} видалено (копії в документах замовлення залишились).")
    return redirect("/packing-lists/")


@_staff
def pl_template_download(request):
    path = svc.active_template_path()
    name = "packing_list_template_custom.docx" if svc.has_custom_template() else "packing_list_template.docx"
    return FileResponse(open(path, "rb"), as_attachment=True, filename=name)


@_staff
@require_POST
def pl_template_upload(request):
    f = request.FILES.get("template")
    if not f or not f.name.lower().endswith(".docx"):
        messages.error(request, "❌ Потрібен файл .docx")
        return redirect("/packing-lists/")
    try:
        svc.save_custom_template(f)
    except Exception as e:
        messages.error(request, f"❌ Шаблон не прийнято — помилка в змінних: {e}")
        return redirect("/packing-lists/")
    messages.success(request, "✅ Власний шаблон завантажено і перевірено. Нові листи генеруються з нього.")
    return redirect("/packing-lists/")


@_staff
@require_POST
def pl_template_reset(request):
    svc.reset_custom_template()
    messages.success(request, "↩️ Повернуто стандартний шаблон.")
    return redirect("/packing-lists/")
