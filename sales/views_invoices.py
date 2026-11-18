"""🧾 Рахунки (Rechnung) — сторінки /rechnungen/ (логіка — sales/invoice_service.py).

Права — ролі Minerva через has_perm (сторінки поза /admin/, ModuleAccessMiddleware їх не перевіряє):
список, файли, приклад і поточний шаблон — sales.view_salesinvoice; новий — add; зберегти / сформувати — change;
видалити — delete; завантажити свій шаблон / повернути стандартний — sales.change_invoicesettings.
"""
import logging

from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import invoice_service as svc
from .models import SalesOrder
from .models_invoice import LANGS, PAY_KINDS, VAT_MODES, InvoiceSettings, SalesInvoice

logger = logging.getLogger(__name__)
BASE = "/rechnungen/"
LIST_LIMIT = 300
TEMPLATE_MAX = 10 * 1024 * 1024


def _staff(fn):
    return staff_member_required(fn, login_url="/admin/login/")


def _need(request, perm):
    if not request.user.has_perm(perm):
        raise PermissionDenied("Немає права на рахунки")


def _can(request):
    u = request.user
    return {"view": u.has_perm("sales.view_salesinvoice"), "add": u.has_perm("sales.add_salesinvoice"),
            "change": u.has_perm("sales.change_salesinvoice"), "delete": u.has_perm("sales.delete_salesinvoice"),
            "template": u.has_perm("sales.change_invoicesettings"),
            "settings": u.has_perm("sales.view_invoicesettings")}


def _find_order(q):
    q = (q or "").strip()
    if not q:
        return None
    return (SalesOrder.objects.filter(order_number=q).first()
            or SalesOrder.objects.filter(order_number__icontains=q).order_by("-order_date").first())


@_staff
def invoice_list(request):
    _need(request, "sales.view_salesinvoice")
    q = (request.GET.get("order") or "").strip()
    if q:
        order = _find_order(q)
        if order is not None:
            return redirect(f"{BASE}new/?order={order.pk}")
        messages.warning(request, f"Замовлення «{q}» не знайдено.")
    search = (request.GET.get("q") or "").strip()
    qs = SalesInvoice.objects.select_related("sales_order")
    if search:
        qs = qs.filter(Q(number__icontains=search) | Q(order_ref__icontains=search)
                       | Q(sales_order__order_number__icontains=search) | Q(bill_to__icontains=search))
    s = InvoiceSettings.get()
    return render(request, "admin/sales/invoice/list.html", {
        **admin.site.each_context(request), "title": "Рахунки (Rechnung)",
        "invoices": list(qs[:LIST_LIMIT]), "search": search, "limit": LIST_LIMIT, "can": _can(request),
        "template_kind": svc.template_kind(s), "template_vars": svc.TEMPLATE_VARS,
        "company_missing": svc.company_missing(),
        "placeholders": svc.PLACEHOLDERS,
    })


def _form_ctx(request, inv, order=None, pay_kind="invoice", errors=None):
    return {
        **admin.site.each_context(request),
        "title": f"Рахунок {inv.number}" if inv.pk else "Новий рахунок",
        "inv": inv, "order": order or inv.sales_order, "can": _can(request), "errors": errors or [],
        "langs": LANGS, "vat_modes": VAT_MODES, "pay_kind": inv.pay_kind or pay_kind,
        "pay_kinds": PAY_KINDS, "salutations": svc.SALUTATIONS,
        "methods": SalesOrder.PAYMENT_METHOD_CHOICES,
        "texts": svc.all_standard_texts(has_order=bool(order or inv.sales_order_id)), "lines": inv.lines or [],
        "placeholders": svc.PLACEHOLDERS, "number_hint": _number_hint(inv, order),
        "settings_url": "/admin/sales/invoicesettings/",
    }


def _number_hint(inv, order):
    if inv.pk:
        return ""
    if order is not None:
        info = getattr(order, "shop_info", None) if svc._has_shop(order) else None
        if info is not None:
            known = (info.doc_numbers or {}).get("CI")
            return f"Номер магазину: {known}" if known else \
                "Номер магазину CI-<Kundennummer>-NNN видається при збереженні"
        return f"Буде: {InvoiceSettings.get().number_prefix}{order.order_number}"
    return "Буде: префікс + дата"


@_staff
def invoice_new(request):
    _need(request, "sales.add_salesinvoice")
    order = None
    pk = request.GET.get("order") or request.POST.get("order_id")
    if pk:
        order = get_object_or_404(SalesOrder, pk=pk)
        existing = order.sales_invoices.order_by("-pk").first()
        if existing is not None and request.method != "POST" and not request.GET.get("again"):
            messages.info(request, f"ℹ️ Для замовлення {order.order_number} уже є рахунок {existing.number} — "
                                   f"відкрито його. Новий: «＋ Ще один рахунок» внизу.")
            return redirect(f"{BASE}{existing.pk}/")
    data = svc.initial_data(order)
    inv = SalesInvoice(**{k: v for k, v in data.items()})
    kind = svc.pay_kind(order.payment_method or "", order.payment_status or "") if order is not None else "invoice"
    if request.method == "POST":
        _need(request, "sales.change_salesinvoice")
        inv.number = ""
        inv.created_by = request.user
        typed = (request.POST.get("number") or "").strip()
        if typed and SalesInvoice.objects.filter(number=typed).exists():
            messages.error(request, f"❌ Номер «{typed}» уже має інший рахунок.")
            return render(request, "admin/sales/invoice/form.html", _form_ctx(request, inv, order, kind))
        inv.number = typed or svc.unique_number(svc.number_for(order))
        return _save(request, inv, order, kind)
    return render(request, "admin/sales/invoice/form.html", _form_ctx(request, inv, order, kind))


def _save(request, inv, order, kind):
    try:
        svc.save_form(inv, request.POST)
    except svc.InvoiceError as e:
        messages.error(request, f"❌ {e}")
        return render(request, "admin/sales/invoice/form.html", _form_ctx(request, inv, order, kind))
    try:
        svc.generate(inv)
    except Exception as e:  # noqa: BLE001 — збережено, документ не сформовано: показати причину
        logger.exception("invoice %s generation failed", inv.pk)
        messages.error(request, f"❌ Рахунок збережено, але документ не сформовано: {e}")
        return redirect(f"{BASE}{inv.pk}/")
    msg = f"✅ Рахунок {inv.number} сформовано"
    msg += " (DOCX + PDF)." if inv.pdf_file else " (лише DOCX — на сервері немає LibreOffice для PDF)."
    if inv.sales_order_id:
        msg += " Копія — у «Документах замовлення»."
    messages.success(request, msg)
    return redirect(f"{BASE}{inv.pk}/")


@_staff
@require_POST
def invoice_from_order(request, order_pk):
    """«⚡ Одразу сформувати» (кнопка «🧾 Rechnung» у картці замовлення): рахунок із даних замовлення без форми,
    назад у картку. Рахунок уже є — відкривається він."""
    order = get_object_or_404(SalesOrder, pk=order_pk)
    card = reverse("admin:sales_salesorder_change", args=[order.pk])
    if not (request.user.has_perm("sales.add_salesinvoice") and request.user.has_perm("sales.change_salesinvoice")):
        messages.error(request, "❌ Немає права створювати рахунки.")
        return redirect(card)
    try:
        inv, created = svc.quick_from_order(order, request.user)
    except Exception as e:  # noqa: BLE001 — рахунок збережено, документ — ні: показати причину у формі
        logger.exception("quick invoice for order %s failed", order.pk)
        inv = order.sales_invoices.order_by("-pk").first()
        messages.error(request, f"❌ Рахунок не сформовано: {e}")
        return redirect(f"{BASE}{inv.pk}/" if inv else card)
    if not created:
        messages.info(request, f"ℹ️ Для замовлення {order.order_number} уже є рахунок {inv.number} — відкрито його.")
        return redirect(f"{BASE}{inv.pk}/")
    msg = f"✅ Рахунок {inv.number} сформовано"
    msg += " (DOCX + PDF)" if inv.pdf_file else " (лише DOCX — на сервері немає LibreOffice для PDF)"
    messages.success(request, msg + " і додано в документи замовлення. Змінити — «🧾 Rechnung …» → форма.")
    return redirect(card)


@_staff
def invoice_edit(request, pk):
    _need(request, "sales.view_salesinvoice")
    inv = get_object_or_404(SalesInvoice.objects.select_related("sales_order"), pk=pk)
    if request.method == "POST":
        _need(request, "sales.change_salesinvoice")
        return _save(request, inv, inv.sales_order, inv.pay_kind)
    order = inv.sales_order
    return render(request, "admin/sales/invoice/form.html", _form_ctx(request, inv, inv.sales_order, inv.pay_kind))


@_staff
def invoice_file(request, pk, kind):
    _need(request, "sales.view_salesinvoice")
    if kind not in ("pdf", "docx"):
        raise Http404
    inv = get_object_or_404(SalesInvoice, pk=pk)
    f = inv.pdf_file if kind == "pdf" else inv.docx_file
    if not f:
        raise Http404("Файл ще не сформовано")
    return FileResponse(f.open("rb"), as_attachment=kind != "pdf", filename=f"{svc.file_base(inv)}.{kind}")


@_staff
@require_POST
def invoice_delete(request, pk):
    _need(request, "sales.delete_salesinvoice")
    inv = get_object_or_404(SalesInvoice, pk=pk)
    for f in (inv.docx_file, inv.pdf_file):
        if f:
            try:
                f.delete(save=False)
            except OSError as e:   # файл зайнятий / уже зник — запис однаково видаляємо
                logger.warning("invoice %s: file not deleted: %s", inv.number, e)
    number = inv.number
    inv.delete()
    messages.success(request, f"🗑️ Рахунок {number} видалено (копії в документах замовлення лишились).")
    return redirect(BASE)


@_staff
def template_download(request):
    _need(request, "sales.view_salesinvoice")
    kind = svc.template_kind()
    name = {"custom": "rechnung_template_custom.docx", "blank": "rechnung_template_blank.docx"}.get(
        kind, "rechnung_template.docx")
    return FileResponse(open(svc.template_path(), "rb"), as_attachment=True, filename=name)


@_staff
def template_sample(request):
    """Приклад рахунку на поточному шаблоні (DOCX; ?pdf=1 — PDF, якщо є LibreOffice)."""
    _need(request, "sales.view_salesinvoice")
    data = svc._render(svc.template_path(), svc.build_context(svc.sample_invoice()))
    if request.GET.get("pdf"):
        from documents.service import _convert_to_pdf
        try:
            pdf = _convert_to_pdf(data)
        except Exception:  # noqa: BLE001
            pdf = None
        if pdf:
            resp = HttpResponse(pdf, content_type="application/pdf")
            resp["Content-Disposition"] = 'inline; filename="Rechnung_Beispiel.pdf"'
            return resp
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp["Content-Disposition"] = 'attachment; filename="Rechnung_Beispiel.docx"'
    return resp


@_staff
@require_POST
def template_upload(request):
    _need(request, "sales.change_invoicesettings")
    f = request.FILES.get("template")
    if not f or not f.name.lower().endswith(".docx") or f.size > TEMPLATE_MAX:
        messages.error(request, "❌ Потрібен файл .docx (до 10 МБ).")
        return redirect(f"{BASE}#template")
    data = f.read()
    try:
        svc.check_template(data)
    except Exception as e:  # noqa: BLE001 — показати, що не так у змінних
        messages.error(request, f"❌ Шаблон не прийнято — помилка в змінних: {e}")
        return redirect(f"{BASE}#template")
    from django.core.files.base import ContentFile
    s = InvoiceSettings.get()
    if s.template:
        s.template.delete(save=False)
    s.template.save("rechnung_custom.docx", ContentFile(data), save=True)
    messages.success(request, "✅ Власний шаблон завантажено й перевірено на прикладі. Нові рахунки — з нього.")
    return redirect(f"{BASE}#template")


@_staff
@require_POST
def template_reset(request):
    _need(request, "sales.change_invoicesettings")
    s = InvoiceSettings.get()
    if s.template:
        s.template.delete(save=False)
        s.template = ""
        s.save(update_fields=["template"])
    messages.success(request, "↩️ Повернуто шаблон на бланку Commercial Invoice.")
    return redirect(f"{BASE}#template")
