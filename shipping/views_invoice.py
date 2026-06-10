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
    invoices = Invoice.objects.select_related("sales_order", "created_by").order_by("-invoice_number")
    return render(request, "invoices/list.html", {"invoices": invoices})


@_staff
def invoice_detail(request, pk):
    inv = get_object_or_404(Invoice, pk=pk)
    return render(request, "invoices/detail.html", {"inv": inv})


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

    try:
        inv = InvoiceService.generate_from_digikey_order(
            dk_order_no, request.user, manual_data=manual_data
        )
        return JsonResponse({
            "ok": True,
            "invoice_id":     inv.pk,
            "invoice_number": inv.invoice_number,
            "total_amount":   str(inv.total_amount),
            "download_url":   f"/invoices/{inv.pk}/download/",
            "detail_url":     f"/invoices/{inv.pk}/",
        })
    except ValueError as e:
        # API unavailable or order not found — return error so UI can show manual form
        return JsonResponse({"error": str(e), "needs_manual": True}, status=422)
    except Exception as e:
        logger.exception("invoice_generate error for order %s", dk_order_no)
        return JsonResponse({"error": str(e)}, status=500)


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
        return JsonResponse(resp)
    except ValueError as e:
        return JsonResponse({"error": str(e), "needs_manual": True}, status=422)
    except Exception as e:
        logger.exception("invoice_fetch_preview error for %s", dk_order_no)
        return JsonResponse({"error": str(e), "needs_manual": True}, status=500)


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
