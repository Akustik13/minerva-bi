"""
shipping/views_invoice.py — Invoice management views.
All views require login and staff status (checked via MinervaAdminMixin pattern).
"""
import json
import logging
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from shipping.models import Invoice

logger = logging.getLogger(__name__)


def _staff(view_fn):
    return staff_member_required(view_fn, login_url="/admin/login/")


@_staff
def invoice_list(request):
    from django.db.models import F, ExpressionWrapper, DecimalField as DField, Sum
    invoices = (
        Invoice.objects
        .select_related("sales_order", "created_by")
        .annotate(
            gross_amount=ExpressionWrapper(
                F('subtotal') - F('discount_amount') - F('shipping_charges'),
                output_field=DField(max_digits=12, decimal_places=2),
            )
        )
        .order_by("-invoice_number")
    )
    agg = Invoice.objects.aggregate(sum_subtotal=Sum('subtotal'), sum_total=Sum('total_amount'))
    return render(request, "invoices/list.html", {
        "invoices":     invoices,
        "sum_subtotal": agg["sum_subtotal"] or 0,
        "sum_total":    agg["sum_total"] or 0,
    })


@_staff
def invoice_detail(request, pk):
    inv = get_object_or_404(Invoice, pk=pk)
    gross_amount = inv.subtotal - inv.discount_amount - inv.shipping_charges
    discount_pct = None
    if inv.discount_amount and gross_amount:
        discount_pct = abs(inv.discount_amount / gross_amount * 100)
    return render(request, "invoices/detail.html", {
        "inv": inv,
        "gross_amount": gross_amount,
        "discount_pct": discount_pct,
    })


@_staff
@require_POST
def invoice_generate(request):
    """
    POST JSON: {"digikey_order_no": "99674401"}
    or with manual fallback data embedded:
    {"digikey_order_no": "99674401", "manual": true, "order_data": {...}}
    """
    from shipping.services.invoice_service import InvoiceService, _fetch_digikey_order

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    dk_order_no = (body.get("digikey_order_no") or "").strip()
    if not dk_order_no:
        return JsonResponse({"error": "digikey_order_no is required"}, status=400)

    manual_data = None
    if body.get("manual") and body.get("order_data"):
        manual_data = body["order_data"]

    invoice_number_override = (body.get("invoice_number") or "").strip() or None
    vat_id_override = (body.get("vat_id") or "").strip() or None

    try:
        inv = InvoiceService.generate_from_digikey_order(
            dk_order_no, request.user,
            manual_data=manual_data,
            invoice_number=invoice_number_override,
            vat_id=vat_id_override,
        )
        return JsonResponse({
            "ok": True,
            "invoice_id":         inv.pk,
            "invoice_number":     inv.invoice_number,
            "digikey_order_no":   inv.digikey_order_no,
            "order_date":         inv.order_date.strftime("%d.%m.%Y") if inv.order_date else "—",
            "shipped_to_company": inv.shipped_to_company,
            "shipped_to_vat":     inv.shipped_to_vat,
            "subtotal":           str(inv.subtotal),
            "discount_amount":    str(inv.discount_amount),
            "shipping_charges":   str(inv.shipping_charges),
            "vat_amount":         str(inv.vat_amount),
            "total_amount":       str(inv.total_amount),
            "pdf_url":            f"/invoices/{inv.pk}/pdf/",
            "download_url":       f"/invoices/{inv.pk}/download/",
            "detail_url":         f"/invoices/{inv.pk}/",
        })
    except ValueError as e:
        # API unavailable or order not found — return error so UI can show manual form
        return JsonResponse({"error": str(e), "needs_manual": True}, status=422)
    except Exception as e:
        logger.exception("invoice_generate error for order %s", dk_order_no)
        return JsonResponse({"error": str(e)}, status=500)


@_staff
def invoice_next_number(request):
    """GET /invoices/next-number/ — suggest next invoice number."""
    from shipping.services.invoice_service import get_next_invoice_number
    return JsonResponse({"number": get_next_invoice_number()})


@_staff
def invoice_fetch_preview(request, dk_order_no):
    """
    GET /invoices/fetch/<dk_order_no>/ — fetch order data from DigiKey API for preview.
    Returns JSON order_data if available, or error for manual fallback.
    Add ?debug=1 to also get the raw shipping address fields from the API.
    """
    from shipping.services.invoice_service import _fetch_digikey_order
    try:
        data = _fetch_digikey_order(dk_order_no)
        resp = {"ok": True, "order_data": data}
        if request.GET.get("debug"):
            resp["_debug_shipping_address"] = data.pop("_raw_shipping_address", {})
        else:
            data.pop("_raw_shipping_address", None)
        # Check if an invoice already exists for this DK order number
        existing = Invoice.objects.filter(digikey_order_no=dk_order_no).order_by("-invoice_date").first()
        if existing:
            resp["existing_invoice"] = {
                "number":     existing.invoice_number,
                "pk":         existing.pk,
                "total":      str(existing.total_amount),
                "detail_url": f"/invoices/{existing.pk}/",
            }
        return JsonResponse(resp)
    except ValueError as e:
        return JsonResponse({"error": str(e), "needs_manual": True}, status=422)
    except Exception as e:
        logger.exception("invoice_fetch_preview error for %s", dk_order_no)
        return JsonResponse({"error": str(e), "needs_manual": True}, status=500)


@_staff
def invoice_pdf_view(request, pk):
    from pathlib import Path
    from shipping.services.invoice_service import convert_docx_to_pdf

    inv = get_object_or_404(Invoice, pk=pk)

    # 1. Serve stored PDF if available
    if inv.pdf_file:
        pdf_path = Path(settings.MEDIA_ROOT) / str(inv.pdf_file)
        if pdf_path.exists():
            return FileResponse(
                open(pdf_path, "rb"),
                content_type="application/pdf",
                filename=f"Invoice_{inv.invoice_number}.pdf",
            )

    # 2. Convert from .docx on-the-fly (for older invoices without stored PDF)
    if inv.docx_file:
        docx_path = Path(settings.MEDIA_ROOT) / str(inv.docx_file)
        if docx_path.exists():
            output_dir = Path(settings.MEDIA_ROOT) / "invoices"
            pdf_path = convert_docx_to_pdf(docx_path, output_dir)
            if pdf_path and pdf_path.exists():
                inv.pdf_file = f"invoices/{pdf_path.name}"
                inv.save(update_fields=["pdf_file"])
                return FileResponse(
                    open(pdf_path, "rb"),
                    content_type="application/pdf",
                    filename=f"Invoice_{inv.invoice_number}.pdf",
                )

    # 3. Fallback: printable HTML page
    return render(request, "invoices/pdf.html", {"inv": inv})


@_staff
def invoice_download(request, pk):
    inv = get_object_or_404(Invoice, pk=pk)
    if not inv.docx_file:
        raise Http404("File not found")
    file_path = Path(settings.MEDIA_ROOT) / str(inv.docx_file)
    if not file_path.exists():
        raise Http404("File not found on disk")
    filename = f"Invoice_{inv.invoice_number}.docx"
    return FileResponse(
        open(file_path, "rb"),
        as_attachment=True,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@_staff
@require_POST
def invoice_register(request):
    """
    Register an existing invoice (file already exists, just add DB record for correct numbering).
    Accepts multipart/form-data:
      invoice_number, digikey_order_no, order_date, shipped_to_company, total_amount
      docx_file (optional upload)
    """
    from datetime import date as _date

    def _g(k, default=""):
        return request.POST.get(k, default).strip()

    invoice_number = _g("invoice_number")
    if not invoice_number:
        return JsonResponse({"error": "Номер інвойсу обов'язковий"}, status=400)

    if Invoice.objects.filter(invoice_number=invoice_number).exists():
        return JsonResponse({"error": f"Інвойс #{invoice_number} вже є в базі"}, status=409)

    def _parse(s):
        if not s:
            return None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                from datetime import datetime
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
        return None

    order_date_raw = _g("order_date")
    order_date = _parse(order_date_raw) or _date.today()

    total_raw = _g("total_amount", "0")
    try:
        from decimal import Decimal
        total = Decimal(total_raw.replace(",", "."))
    except Exception:
        total = Decimal("0")

    docx_field = None
    uploaded = request.FILES.get("docx_file")
    if uploaded:
        # Save to MEDIA/invoices/
        from django.core.files.storage import default_storage
        fname = f"invoices/Invoice_{invoice_number}.docx"
        default_storage.save(fname, uploaded)
        docx_field = fname

    inv = Invoice.objects.create(
        invoice_number     = invoice_number,
        digikey_order_no   = _g("digikey_order_no"),
        order_date         = order_date,
        shipped_to_company = _g("shipped_to_company"),
        shipped_to_vat     = _g("shipped_to_vat"),
        total_amount       = total,
        subtotal           = total,
        docx_file          = docx_field,
        created_by         = request.user,
    )

    return JsonResponse({
        "ok": True,
        "invoice_id":     inv.pk,
        "invoice_number": inv.invoice_number,
        "total_amount":   str(inv.total_amount),
        "download_url":   f"/invoices/{inv.pk}/download/" if docx_field else None,
        "detail_url":     f"/invoices/{inv.pk}/",
    })


@_staff
@require_POST
def invoice_update_recipient(request, pk):
    """POST JSON — update shipped_to_json + shipped_to_vat for an existing invoice."""
    inv = get_object_or_404(Invoice, pk=pk)
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    shipped_to = {
        "company":  body.get("company", inv.shipped_to_company or ""),
        "contact":  body.get("contact", ""),
        "address1": body.get("address1", ""),
        "city_zip": body.get("city_zip", ""),
        "country":  body.get("country", ""),
        "vat_id":   body.get("vat_id", ""),
    }
    inv.shipped_to_json    = shipped_to
    inv.shipped_to_company = shipped_to["company"]
    inv.shipped_to_vat     = shipped_to["vat_id"]
    inv.save(update_fields=["shipped_to_json", "shipped_to_company", "shipped_to_vat"])
    return JsonResponse({"ok": True})


@_staff
def invoice_delete(request, pk):
    inv = get_object_or_404(Invoice, pk=pk)
    if request.method == "POST":
        # Delete file from disk
        if inv.docx_file:
            file_path = Path(settings.MEDIA_ROOT) / str(inv.docx_file)
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
        inv.delete()
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": True})
        messages.success(request, f"Invoice #{inv.invoice_number} видалено.")
        return redirect("invoice_list")
    return render(request, "invoices/confirm_delete.html", {"inv": inv})


# ── Invoice template management ────────────────────────────────────────────

@_staff
def invoice_templates_list(request):
    """GET — return JSON list of available invoice templates."""
    from shipping.services.invoice_service import list_invoice_templates
    return JsonResponse({"templates": list_invoice_templates()})


@_staff
@require_POST
def invoice_template_upload(request):
    """POST multipart — upload a new .docx template."""
    from shipping.services.invoice_service import _tpl_dir, list_invoice_templates
    f = request.FILES.get("template_file")
    if not f:
        return JsonResponse({"error": "Файл не вибрано"}, status=400)
    if not f.name.lower().endswith(".docx"):
        return JsonResponse({"error": "Тільки .docx файли"}, status=400)
    dest = _tpl_dir() / f.name
    with open(dest, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    return JsonResponse({"ok": True, "templates": list_invoice_templates()})


@_staff
def invoice_template_download(request, name):
    """GET — download a template by filename."""
    from shipping.services.invoice_service import (
        _tpl_dir, DEFAULT_TEMPLATE_NAME, DEFAULT_TEMPLATE_PATH,
    )
    if name == DEFAULT_TEMPLATE_NAME:
        path = DEFAULT_TEMPLATE_PATH
    else:
        path = _tpl_dir() / name
    if not path.exists() or ".." in name:
        raise Http404
    return FileResponse(
        open(path, "rb"),
        as_attachment=True,
        filename=name,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@_staff
@require_POST
def invoice_template_activate(request, name):
    """POST — set a custom template as active (or '' to revert to default)."""
    from shipping.services.invoice_service import (
        _tpl_dir, DEFAULT_TEMPLATE_NAME, set_active_template_name, list_invoice_templates,
    )
    if name == DEFAULT_TEMPLATE_NAME:
        set_active_template_name("")
    else:
        if not (_tpl_dir() / name).exists() or ".." in name:
            return JsonResponse({"error": "Шаблон не знайдено"}, status=404)
        set_active_template_name(name)
    return JsonResponse({"ok": True, "templates": list_invoice_templates()})


@_staff
@require_POST
def invoice_template_delete(request, name):
    """POST — delete a custom template (default cannot be deleted)."""
    from shipping.services.invoice_service import (
        _tpl_dir, DEFAULT_TEMPLATE_NAME, get_active_template_name,
        set_active_template_name, list_invoice_templates,
    )
    if name == DEFAULT_TEMPLATE_NAME:
        return JsonResponse({"error": "Дефолтний шаблон видалити неможна"}, status=403)
    if ".." in name:
        return JsonResponse({"error": "Неприпустиме ім'я"}, status=400)
    path = _tpl_dir() / name
    if not path.exists():
        return JsonResponse({"error": "Шаблон не знайдено"}, status=404)
    # If this was the active template, revert to default
    if get_active_template_name() == name:
        set_active_template_name("")
    path.unlink()
    return JsonResponse({"ok": True, "templates": list_invoice_templates()})
