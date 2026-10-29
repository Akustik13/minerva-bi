"""REST API закупівель: замовлення постачальникам, які зробила зовнішня система (CRM).

    GET   /purchase-orders/                       — список (status, source, external_id, supplier, sku,
                                                    tracking_number, open=1, changed_since)
    GET   /purchase-orders/{ref}/                 — одне: PO-12 або ext:<номер у вашій системі>
    POST  /purchase-orders/                       — передати замовлення (повтор з тим самим external_id оновлює)
    PATCH /purchase-orders/{ref}/                 — змінити (дати, номер постачальника, позиції, статус)
    POST  /purchase-orders/{ref}/shipments/       — трек-номер / стан відправлення
    POST  /purchase-orders/{ref}/deliveries/      — «доставлено» (повтор того самого — duplicate, без дубля на складі)
    POST  /purchase-orders/{ref}/cancel/          — скасувати
    POST  /purchase-orders/shipments/ і /deliveries/ — те саме з external_id у тілі (номер зі «/»)

Права ключа: purchases:read / purchases:write. Логіка — inventory/services/purchases.py.
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from decimal import Decimal

from django.http import Http404
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import PurchaseOrder
from inventory.services import purchases as service
from inventory.services.purchases import PurchaseError

from . import webhooks as webhook_service
from .models import APIKey

DEFAULT_SOURCE = "crm"
OPEN = (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIAL)


def _num(value):
    if value is None:
        return None
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


def _supplier(s):
    if s is None:
        return None
    return {"id": s.pk, "name": s.name, "email": s.email, "contact_person": s.contact_person,
            "phone": s.phone, "country": s.addr_country}


def purchase_data(po: PurchaseOrder) -> dict:
    """Закупівля для відповіді API і вебхука purchase_order.updated."""
    lines = sorted(po.lines.all(), key=lambda l: l.pk)
    cancelled = po.status == PurchaseOrder.Status.CANCELLED
    rows = []
    for l in lines:
        sku = l.product.sku if l.product_id else ""
        rows.append({
            "line_id": l.external_line_id, "sku": l.external_sku or sku, "product_sku": sku or None,
            "in_minerva": bool(l.product_id), "description": l.description,
            "qty_ordered": _num(l.qty_ordered), "qty_received": _num(l.qty_received),
            "qty_open": 0 if cancelled else _num(max(l.qty_ordered - l.qty_received, Decimal("0"))),
            "unit_price": _num(l.unit_price), "currency": l.currency, "notes": l.notes,
        })
    shipments = sorted(po.incoming_shipments.all(), key=lambda s: s.pk)
    receipts = sorted(po.receipts.all(), key=lambda r: r.pk)
    return {
        "id": po.code, "code": po.code, "source": po.external_source, "external_id": po.external_id,
        "status": po.status, "status_label": po.get_status_display(),
        "supplier": _supplier(po.supplier), "supplier_order_no": po.supplier_order_no,
        "order_date": po.order_date, "expected_date": po.expected_date, "received_date": po.received_date,
        "currency": po.currency, "total": _num(po.total_price), "notes": po.notes,
        "carrier": po.shipping_service, "tracking_number": po.tracking_number,
        "delivered_reported_at": po.delivered_reported_at,
        "qty_ordered": sum((r["qty_ordered"] or 0 for r in rows), 0),
        "qty_received": sum((r["qty_received"] or 0 for r in rows), 0),
        "qty_open": sum((r["qty_open"] or 0 for r in rows), 0),
        "lines": rows,
        "shipments": [{
            "tracking_number": s.tracking_number, "carrier": s.carrier_name, "tracking_url": s.tracking_url,
            "status": s.status, "status_label": s.get_status_display(), "carrier_status": s.carrier_status_label,
            "expected_date": s.expected_date, "shipped_date": s.shipped_date, "received_date": s.received_date,
            "last_tracked_at": s.last_tracked_at,
        } for s in shipments],
        "deliveries": [{
            "delivery_id": r.delivery_key, "delivered_at": r.delivered_at, "tracking_number": r.tracking_number,
            "booked": r.booked, "lines": r.lines, "source": r.source, "created_at": r.created_at,
        } for r in receipts],
        "created_at": po.created_at, "updated_at": po.updated_at,
    }


def _error(exc: PurchaseError):
    body = {"detail": str(exc), "code": exc.code}
    if exc.details:
        body["details"] = exc.details
    return Response(body, status=exc.status)


def _body(request) -> dict:
    if not isinstance(request.data, dict):
        raise PurchaseError("Очікується об'єкт JSON.", code="bad_body")
    return request.data


def _source(request) -> str:
    """Яка зовнішня система: source у тілі / адресі → «Джерело замовлень» ключа → crm."""
    data = request.data if isinstance(request.data, dict) else {}
    key = request.auth if isinstance(request.auth, APIKey) else None
    raw = data.get("source") or request.query_params.get("source") or (key.default_source if key else "")
    return slugify(str(raw or DEFAULT_SOURCE))[:32] or DEFAULT_SOURCE


def _flag(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


class PurchaseOrderViewSet(viewsets.GenericViewSet):
    resource_scope = "purchases"
    lookup_field = "ref"
    lookup_value_regex = r"[^/]+"
    filter_backends = []
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = (PurchaseOrder.objects.select_related("supplier")
              .prefetch_related("lines__product", "incoming_shipments", "receipts")
              .order_by("-updated_at", "-id"))
        p = self.request.query_params
        if p.get("status"):
            qs = qs.filter(status__in=[s.strip() for s in p["status"].split(",") if s.strip()])
        if _flag(p.get("open", "")):
            qs = qs.filter(status__in=OPEN)
        if p.get("source"):
            qs = qs.filter(external_source=p["source"])
        if p.get("external_id"):
            qs = qs.filter(external_id=p["external_id"])
        if p.get("supplier"):
            qs = qs.filter(supplier__name__icontains=p["supplier"])
        if p.get("sku"):
            sku = p["sku"].strip()
            qs = qs.filter(lines__product__sku__iexact=sku) | qs.filter(lines__external_sku__iexact=sku)
            qs = qs.distinct()
        if p.get("tracking_number"):
            n = p["tracking_number"].strip()
            qs = (qs.filter(tracking_number__iexact=n) | qs.filter(incoming_shipments__tracking_number__iexact=n)
                  ).distinct()
        if p.get("changed_since"):
            raw = p["changed_since"]
            since = parse_datetime(raw.replace(" ", "+"))
            if since is None and parse_date(raw):
                since = datetime.combine(parse_date(raw), dtime.min)
            if since is None:
                raise PurchaseError("changed_since: очікується дата або дата й час ISO 8601.", code="bad_date")
            if timezone.is_naive(since):
                since = timezone.make_aware(since)
            qs = qs.filter(updated_at__gte=since)
        return qs

    def _get(self, ref, for_write=False) -> PurchaseOrder:
        po = service.find(_source(self.request), ref)
        if po is None:
            raise Http404(f"Закупівлю «{ref}» не знайдено.")
        if for_write and not po.external_source:
            raise PurchaseError("Закупівлю створено в Minerva — через API змінюються лише замовлення, "
                                "які передала зовнішня система.", code="not_external", status=403)
        return po

    def _by_body(self, data) -> PurchaseOrder:
        if data.get("external_id"):
            return self._get(f"ext:{data['external_id']}", for_write=True)
        if data.get("code") or data.get("id"):
            return self._get(str(data.get("code") or data.get("id")), for_write=True)
        raise PurchaseError("Потрібен external_id (або code) закупівлі.", code="external_id_required")

    def _reply(self, po, http=status.HTTP_200_OK, **extra):
        po = PurchaseOrder.objects.select_related("supplier") \
            .prefetch_related("lines__product", "incoming_shipments", "receipts").get(pk=po.pk)
        return Response({**purchase_data(po), **extra}, status=http)

    # ── читання ──
    def list(self, request):
        try:
            qs = self.get_queryset()
            page = self.paginate_queryset(qs)
        except PurchaseError as e:
            return _error(e)
        return self.get_paginated_response([purchase_data(po) for po in page])

    def retrieve(self, request, ref=None):
        return self._reply(self._get(ref))

    # ── запис ──
    def create(self, request):
        try:
            data = _body(request)
            source = _source(request)
            with webhook_service.purchases_changed_by(source):
                po, created, warnings = service.upsert(data, source, strict=_flag(data.get("strict", "")))
        except PurchaseError as e:
            return _error(e)
        return self._reply(po, status.HTTP_201_CREATED if created else status.HTTP_200_OK,
                           created=created, warnings=warnings)

    def partial_update(self, request, ref=None):
        try:
            data = _body(request)
            po = self._get(ref, for_write=True)
            with webhook_service.purchases_changed_by(po.external_source):
                po, _created, warnings = service.upsert(data, po.external_source, po=po,
                                                        strict=_flag(data.get("strict", "")))
        except PurchaseError as e:
            return _error(e)
        return self._reply(po, created=False, warnings=warnings)

    def _shipment(self, po, data):
        with webhook_service.purchases_changed_by(po.external_source):
            service.upsert_shipment(po, data, po.external_source)
        return self._reply(po)

    def _delivery(self, po, data):
        with webhook_service.purchases_changed_by(po.external_source):
            result = service.deliver(po, data, po.external_source)
        result.pop("status", None)
        return self._reply(po, status.HTTP_200_OK if result["duplicate"] else status.HTTP_201_CREATED,
                           delivery=result)

    @action(detail=True, methods=["post"])
    def shipments(self, request, ref=None):
        try:
            return self._shipment(self._get(ref, for_write=True), _body(request))
        except PurchaseError as e:
            return _error(e)

    @action(detail=True, methods=["post"])
    def deliveries(self, request, ref=None):
        try:
            return self._delivery(self._get(ref, for_write=True), _body(request))
        except PurchaseError as e:
            return _error(e)

    @action(detail=True, methods=["post"])
    def cancel(self, request, ref=None):
        try:
            po = self._get(ref, for_write=True)
            data = request.data if isinstance(request.data, dict) else {}
            with webhook_service.purchases_changed_by(po.external_source):
                po = service.cancel(po, str(data.get("reason") or "")[:300])
        except PurchaseError as e:
            return _error(e)
        return self._reply(po)

    @action(detail=False, methods=["post"], url_path="shipments")
    def shipments_by_ref(self, request):
        try:
            data = _body(request)
            return self._shipment(self._by_body(data), data)
        except PurchaseError as e:
            return _error(e)

    @action(detail=False, methods=["post"], url_path="deliveries")
    def deliveries_by_ref(self, request):
        try:
            data = _body(request)
            return self._delivery(self._by_body(data), data)
        except PurchaseError as e:
            return _error(e)
