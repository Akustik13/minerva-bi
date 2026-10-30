"""«📈 Ціни й продажі» в картці товару й сторінка «📄 Звіт по товару» (логіка — inventory/services/product_report.py).

GET …/product/<pk>/report/[?months=12|24|36] — звіт; …/report/csv/ — продажі по місяцях і історія цін у CSV.
Право — перегляд товару (view або change).
"""
from __future__ import annotations

import csv
import logging
import re
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from .models import Product
from .services import product_report

logger = logging.getLogger(__name__)


def _months(request) -> int:
    try:
        months = int(request.GET.get("months") or product_report.MONTHS)
    except (TypeError, ValueError):
        return product_report.MONTHS
    return months if months in product_report.MONTH_CHOICES else product_report.MONTHS


def _product(model_admin, request, pk) -> Product:
    product = get_object_or_404(Product, pk=pk)
    # ProductAdmin (AuditableMixin) покладається на middleware модулів; тут — ще й право Minerva / Django
    if not (model_admin.has_view_permission(request, product) and request.user.has_perm("inventory.view_product")):
        raise PermissionDenied
    return product


def card_context(product) -> dict:
    """Дані для блоку «📈 Ціни й продажі» в картці; збій — блок з повідомленням, картка відкривається."""
    try:
        data = product_report.build(product, tables=False)
    except Exception:       # noqa: BLE001 — картка товару важливіша за графіки
        logger.exception("product report card %s", product.pk)
        return {"error": True}
    data["report_url"] = reverse("admin:inventory_product_report", args=[product.pk])
    return data


def report_view(model_admin, request, pk):
    product = _product(model_admin, request, pk)
    months = _months(request)
    data = product_report.build(product, months=months)
    return render(request, "admin/inventory/product/report.html", {
        **model_admin.admin_site.each_context(request),
        "title": f"Звіт по товару {product.sku}", "opts": Product._meta, "original": product, "r": data,
        "month_choices": product_report.MONTH_CHOICES,
        "change_url": reverse("admin:inventory_product_change", args=[product.pk]),
        "csv_url": reverse("admin:inventory_product_report_csv", args=[product.pk]) + f"?months={months}",
    })


def _cell(value):
    """Числа — з десятковою комою (Excel з німецькими / українськими налаштуваннями)."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f").replace(".", ",")
    if isinstance(value, float):
        return str(value).replace(".", ",")
    return value


def report_csv_view(model_admin, request, pk):
    product = _product(model_admin, request, pk)
    data = product_report.build(product, months=_months(request), tables=False)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", product.sku)[:80] or str(product.pk)
    resp["Content-Disposition"] = f'attachment; filename="{name}-report-{data["today"]:%Y%m%d}.csv"'
    resp.write(chr(0xFEFF))   # BOM — Excel відкриває UTF-8 без «кракозябр»
    writer = csv.writer(resp, delimiter=";")
    for row in product_report.csv_rows(data):
        writer.writerow([_cell(v) for v in row])
    return resp
