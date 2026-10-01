"""
shipping/views_commercial_invoice.py — Commercial Invoices з власного Word-шаблону.
"""
import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from shipping.models import CommercialInvoice, Shipment
from shipping.services import commercial_invoice_service as svc

logger = logging.getLogger(__name__)

SHIP_TO_KEYS = ("company", "contact", "street", "city_line", "country", "phone", "email")
BASE = "/commercial-invoices/"


def _staff(view_fn):
    return staff_member_required(view_fn, login_url="/admin/login/")


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_lines(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    lines = []
    for ln in data if isinstance(data, list) else []:
        if not isinstance(ln, dict):
            continue
        desc = str(ln.get("description") or "").strip()
        pn = str(ln.get("part_no") or "").strip()
        if not (desc or pn):
            continue
        pid = ln.get("product_id")
        lines.append({
            "description": desc[:200], "part_no": pn[:80],
            "hs_code": str(ln.get("hs_code") or "").strip()[:20],
            "origin": str(ln.get("origin") or "").strip()[:60],
            "qty": _num(ln.get("qty")), "unit_value": _num(ln.get("unit_value")),
            "product_id": pid if isinstance(pid, int) else None,
        })
    return lines


def _form_data(request) -> dict:
    P = request.POST
    try:
        inv_date = date.fromisoformat(P.get("inv_date", ""))
    except ValueError:
        inv_date = date.today()
    try:
        shipping = Decimal(str(P.get("shipping_cost") or 0).replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        shipping = Decimal(0)
    return {
        "number":          P.get("number", "").strip()[:30],
        "inv_date":        inv_date,
        "ship_to":         {k: P.get(f"ship_to_{k}", "").strip() for k in SHIP_TO_KEYS},
        "billing_address": P.get("billing_address", "").strip(),
        "currency":        (P.get("currency", "").strip().upper() or "USD")[:3],
        "lines":           _parse_lines(P.get("lines_json")),
        "shipping_cost":   max(shipping, Decimal(0)),
        "incoterm":        P.get("incoterm", "DAP").strip().upper()[:10],
        "export_reason":   P.get("export_reason", "").strip()[:100],
        "payment_terms":   P.get("payment_terms", "").strip()[:200],
        "signer_name":     P.get("signer_name", "").strip()[:120],
        "signer_position": P.get("signer_position", "").strip()[:120],
    }


def _invalid(request, data) -> bool:
    errors = []
    if not data["lines"]:
        errors.append("додайте хоча б одну позицію")
    bad_qty = [i for i, l in enumerate(data["lines"], 1) if l["qty"] <= 0]
    if bad_qty:
        errors.append("кількість має бути > 0 (позиції " + ", ".join(map(str, bad_qty)) + ")")
    if errors:
        messages.error(request, "❌ Інвойс не згенеровано: " + "; ".join(errors))
    return bool(errors)


def _render_form(request, data, order=None, ci=None):
    return render(request, "admin/shipping/commercial_invoice_form.html", {
        **admin.site.each_context(request),
        "title": f"Commercial Invoice #{ci.number}" if ci else "Новий Commercial Invoice",
        "data": data, "order": order, "ci": ci,
        "lines": data["lines"],
        "incoterms": svc.INCOTERMS.items(),
        "reasons": sorted(set(svc.EXPORT_REASONS.values())),
    })


def _save_and_generate(request, ci, data) -> bool:
    for k in ("ship_to", "billing_address", "currency", "lines", "shipping_cost", "incoterm",
              "export_reason", "payment_terms", "signer_name", "signer_position", "inv_date"):
        setattr(ci, k, data[k])
    ci.number = data["number"] or (ci.sales_order.order_number if ci.sales_order else "")
    if not ci.pk:
        ci.created_by = request.user
    ci.save()
    try:
        svc.generate_files(ci)
    except Exception as e:
        logger.exception("Commercial invoice generation failed")
        messages.error(request, f"❌ Помилка генерації: {e}")
        return False
    msg = f"✅ Invoice #{ci.number} згенеровано"
    msg += " (DOCX + PDF)." if ci.pdf_file else " (DOCX; PDF недоступний — немає LibreOffice)."
    if ci.sales_order:
        msg += " Копія збережена в «Документи замовлення»."
    messages.success(request, msg)
    return True


@_staff
def ci_list(request):
    from sales.models import SalesOrder
    q = request.GET.get("order", "").strip()
    if q:
        order = SalesOrder.objects.filter(order_number=q).first() or \
                SalesOrder.objects.filter(order_number__icontains=q).order_by("-order_date").first()
        if order:
            return redirect(f"{BASE}new/?order={order.pk}")
        messages.warning(request, f"Замовлення «{q}» не знайдено.")
    return render(request, "admin/shipping/commercial_invoice_list.html", {
        **admin.site.each_context(request),
        "title": "Commercial Invoices",
        "invoices": CommercialInvoice.objects.select_related("sales_order")[:300],
        "template_vars": svc.CI_TEMPLATE_VARS,
        "has_custom_template": svc.has_custom_template(),
    })


@_staff
def ci_new(request):
    from sales.models import SalesOrder
    order = get_object_or_404(SalesOrder, pk=request.GET.get("order") or request.POST.get("order_id"))
    if request.method == "POST":
        data = _form_data(request)
        shipment = Shipment.objects.filter(pk=request.POST.get("shipment_id") or None, order=order).first()
        if _invalid(request, data):
            data["shipment"] = shipment
            return _render_form(request, data, order=order)
        ci = CommercialInvoice(sales_order=order, shipment=shipment)
        if _save_and_generate(request, ci, data):
            return redirect(BASE)
        return redirect(f"{BASE}{ci.pk}/edit/")

    data = svc.initial_data(order)
    existing = order.commercial_invoices.order_by("-created_at").first()
    if existing:
        messages.info(request, f"ℹ️ Для цього замовлення вже є Invoice #{existing.number} — "
                               f"його можна редагувати у списку. Тут створюється новий.")
    return _render_form(request, data, order=order)


@_staff
def ci_edit(request, pk):
    ci = get_object_or_404(CommercialInvoice, pk=pk)
    if request.method == "POST":
        data = _form_data(request)
        if _invalid(request, data):
            data["shipment"] = ci.shipment
            return _render_form(request, data, order=ci.sales_order, ci=ci)
        if _save_and_generate(request, ci, data):
            return redirect(BASE)
        return redirect(f"{BASE}{ci.pk}/edit/")
    data = {k: getattr(ci, k) for k in ("number", "inv_date", "ship_to", "billing_address", "currency",
                                         "lines", "incoterm", "export_reason", "payment_terms",
                                         "signer_name", "signer_position", "shipment")}
    data["shipping_cost"] = float(ci.shipping_cost)
    return _render_form(request, data, order=ci.sales_order, ci=ci)


@_staff
def ci_file(request, pk, kind):
    if kind not in ("pdf", "docx"):
        raise Http404
    ci = get_object_or_404(CommercialInvoice, pk=pk)
    f = ci.pdf_file if kind == "pdf" else ci.docx_file
    if not f:
        raise Http404("Файл не згенеровано")
    return FileResponse(f.open("rb"), as_attachment=kind != "pdf", filename=f"Invoice_{ci.number}.{kind}")


@_staff
@require_POST
def ci_delete(request, pk):
    ci = get_object_or_404(CommercialInvoice, pk=pk)
    for f in (ci.docx_file, ci.pdf_file):
        if f:
            f.delete(save=False)
    num = ci.number
    ci.delete()
    messages.success(request, f"🗑️ Invoice #{num} видалено (копії в документах замовлення залишились).")
    return redirect(BASE)


@_staff
def ci_template_download(request):
    name = "commercial_invoice_template_custom.docx" if svc.has_custom_template() else "commercial_invoice_template.docx"
    return FileResponse(open(svc.active_template_path(), "rb"), as_attachment=True, filename=name)


@_staff
@require_POST
def ci_template_upload(request):
    f = request.FILES.get("template")
    if not f or not f.name.lower().endswith(".docx"):
        messages.error(request, "❌ Потрібен файл .docx")
        return redirect(BASE)
    try:
        svc.save_custom_template(f)
    except Exception as e:
        messages.error(request, f"❌ Шаблон не прийнято — помилка в змінних: {e}")
        return redirect(BASE)
    messages.success(request, "✅ Власний шаблон завантажено і перевірено. Нові інвойси генеруються з нього.")
    return redirect(BASE)


@_staff
@require_POST
def ci_template_reset(request):
    svc.reset_custom_template()
    messages.success(request, "↩️ Повернуто стандартний шаблон.")
    return redirect(BASE)
