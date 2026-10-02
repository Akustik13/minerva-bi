from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import APIKey, Webhook
from . import webhooks as webhook_service
from .permissions import HasAPIKeyScope
from .filters import (
    SalesOrderFilter, ProductFilter, CustomerFilter, MovementFilter, ShipmentFilter,
)
from .serializers import (
    SalesOrderListSerializer, SalesOrderDetailSerializer, OrderCreateSerializer,
    ProductSerializer, ProductWithStockSerializer, CustomerSerializer,
    StockSerializer, StockCheckSerializer, MovementSerializer, MovementCreateSerializer,
    StockCountSerializer, LocationSerializer, CategorySerializer, ShipmentSerializer,
    WebhookSerializer, WebhookDeliverySerializer, ShopProductSerializer,
)
from sales.models import SalesOrder, SalesOrderLine
from inventory.models import (
    Product, ProductCategory, Location, InventoryTransaction, InventorySettings,
)
from inventory.services import stock as stock_service
from inventory.services.stock import StockError
from crm.models import Customer
from shipping.models import Shipment


def _error(exc: StockError, http_status=status.HTTP_400_BAD_REQUEST):
    body = {"detail": str(exc), "code": exc.code}
    if exc.details:
        body["details"] = exc.details
    return Response(body, status=http_status)


def _key_label(request):
    return f"API: {request.auth.name}" if isinstance(request.auth, APIKey) else "API"


class NoDeleteMixin:
    """Blocks DELETE on all endpoints — returns HTTP 405."""

    def destroy(self, request, *args, **kwargs):
        return Response(
            {"detail": "DELETE не підтримується."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


# ── Службові ──────────────────────────────────────────────────────────────────

@api_view(["GET"])
def ping(request):
    """Перевірка підключення: повертає назву ключа і його права."""
    key = request.auth if isinstance(request.auth, APIKey) else None
    return Response({
        "ok": True,
        "server_time": timezone.now(),
        "key": key.name if key else None,
        "scopes": key.scopes if key else [],
        "default_source": key.default_source if key else None,
        "user": None if key else getattr(request.user, "username", None),
    })


# ── Склад ─────────────────────────────────────────────────────────────────────

class StockViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /stock/              — залишки всіх товарів (фільтри: sku=A,B, category, search,
                                in_stock, low_stock, changed_since, location)
    GET  /stock/{sku}/        — залишок одного товару + розбивка по локаціях
    POST /stock/check/        — перевірка наявності для кошика
    POST /stock/count/        — інвентаризація (встановити фактичний залишок)
    """
    resource_scope   = "stock"
    action_scopes    = {"check": "read"}
    serializer_class = StockSerializer
    filterset_class  = ProductFilter
    ordering_fields  = ["sku", "name", "_available", "_on_hand", "_last_movement"]
    lookup_field     = "sku"
    lookup_value_regex = r"[^/]+"

    def get_queryset(self):
        location = None
        code = self.request.query_params.get("location")
        if code:
            location = Location.objects.filter(code=code).first()
        return stock_service.annotate_stock(Product.objects.all(), location=location).order_by("sku")

    def get_object(self):
        sku = self.kwargs["sku"]
        product = stock_service.resolve_sku(sku)
        if not product:
            from django.http import Http404
            raise Http404(f"Товар «{sku}» не знайдено.")
        return self.get_queryset().get(pk=product.pk)

    def retrieve(self, request, *args, **kwargs):
        obj = self.get_object()
        data = self.get_serializer(obj).data
        data["locations"] = stock_service.location_breakdown(obj)
        from inventory.utils import get_buildable_qty
        data["buildable"] = get_buildable_qty(obj)
        return Response(data)

    @action(detail=False, methods=["post"])
    def check(self, request):
        s = StockCheckSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        items = stock_service.check_availability(s.validated_data["items"])
        return Response({"ok": all(i["ok"] for i in items), "items": items})

    @action(detail=False, methods=["post"])
    def count(self, request):
        """Інвентаризація: тіло — один об'єкт або список {sku, qty, location?, ref_doc?}."""
        many = isinstance(request.data, list)
        s = StockCountSerializer(data=request.data, many=many)
        s.is_valid(raise_exception=True)
        rows = s.validated_data if many else [s.validated_data]
        results = []
        try:
            with transaction.atomic():
                for row in rows:
                    product = stock_service.resolve_sku(row["sku"])
                    if not product:
                        raise StockError(f"Товар «{row['sku']}» не знайдено.", code="sku_not_found")
                    location = stock_service.get_location(row.get("location"))
                    tx, previous = stock_service.set_stock_level(
                        product=product, qty=row["qty"], location=location,
                        ref_doc=row.get("ref_doc") or "", performed_by=None,
                    )
                    results.append({
                        "sku": product.sku, "location": location.code,
                        "previous": previous, "counted": row["qty"],
                        "delta": row["qty"] - previous,
                        "transaction_id": tx.pk if tx else None,
                    })
        except StockError as e:
            return _error(e)
        return Response({"results": results})


class MovementViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      mixins.CreateModelMixin, viewsets.GenericViewSet):
    """
    GET  /stock-movements/    — журнал рухів (фільтри: sku, tx_type, location, date_from,
                                date_to, created_after, ref_doc)
    POST /stock-movements/    — прихід / списання / коригування
    """
    resource_scope   = "stock"
    serializer_class = MovementSerializer
    filterset_class  = MovementFilter
    ordering_fields  = ["tx_date", "created_at", "qty"]

    def get_queryset(self):
        return (InventoryTransaction.objects
                .select_related("product", "location", "performed_by")
                .order_by("-tx_date", "-id"))

    def create(self, request, *args, **kwargs):
        s = MovementCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        product = stock_service.resolve_sku(d["sku"])
        if not product:
            return Response({"detail": f"Товар «{d['sku']}» не знайдено.", "code": "sku_not_found"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            location = stock_service.get_location(d.get("location"))
            tx, created = stock_service.create_movement(
                product=product, tx_type=d["tx_type"], qty=d["qty"], location=location,
                ref_doc=d.get("ref_doc") or _key_label(request),
                external_key=d.get("external_key") or "", tx_date=d.get("tx_date"),
            )
        except StockError as e:
            code = status.HTTP_409_CONFLICT if e.code == "insufficient_stock" else status.HTTP_400_BAD_REQUEST
            return _error(e, code)
        body = MovementSerializer(tx).data
        body["available_after"] = stock_service.get_available(product)
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class LocationViewSet(viewsets.ReadOnlyModelViewSet):
    resource_scope   = "stock"
    queryset         = Location.objects.all().order_by("code")
    serializer_class = LocationSerializer
    filterset_fields = ["is_active", "location_type"]
    pagination_class = None


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    resource_scope   = "products"
    queryset         = ProductCategory.objects.all().order_by("order", "name")
    serializer_class = CategorySerializer
    lookup_field     = "slug"
    pagination_class = None


# ── Інтернет-магазин ──────────────────────────────────────────────────────────

class ShopProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Каталог інтернет-магазину: активні товари з «Показувати в магазині».
    Товар без ціни теж віддається (price = null) — сайт показує «ціна за запитом».
    GET /shop/products/ (фільтри як у /stock/: sku, category, search, in_stock, changed_since)
    GET /shop/products/{sku}/
    """
    resource_scope   = "products"
    serializer_class = ShopProductSerializer
    filterset_class  = ProductFilter
    ordering_fields  = ["sku", "name", "category"]
    lookup_field     = "sku"
    lookup_value_regex = r"[^/]+"

    def get_queryset(self):
        qs = Product.objects.filter(shop_visible=True, is_active=True)
        return stock_service.annotate_stock(qs).order_by("category", "sku")


# ── Товари ────────────────────────────────────────────────────────────────────

class ProductViewSet(NoDeleteMixin, viewsets.ModelViewSet):
    """
    Каталог товарів. ?with_stock=1 — додає залишки (on_hand/reserved/available…)
    """
    resource_scope     = 'products'
    filterset_class    = ProductFilter
    ordering_fields    = ["sku", "sale_price", "purchase_price"]

    def _with_stock(self):
        return self.request.method == "GET" and \
            self.request.query_params.get("with_stock") in ("1", "true", "yes")

    def get_queryset(self):
        qs = Product.objects.all().order_by("sku")
        # Фільтри in_stock/low_stock/changed_since потребують анотацій
        if self.request.method == "GET":
            qs = stock_service.annotate_stock(qs)
        return qs

    def get_serializer_class(self):
        return ProductWithStockSerializer if self._with_stock() else ProductSerializer


# ── Замовлення ────────────────────────────────────────────────────────────────

class SalesOrderViewSet(NoDeleteMixin, viewsets.ModelViewSet):
    """
    POST /orders/              — створити замовлення (рядки за SKU); повтор з тим самим
                                 source+order_number повертає існуюче (200, created=false)
    POST /orders/{id}/cancel/  — скасувати і повернути товар на склад
    """
    resource_scope     = 'orders'
    queryset           = SalesOrder.objects.all().order_by("-order_date", "-id")
    permission_classes = [HasAPIKeyScope]
    filterset_class    = SalesOrderFilter
    ordering_fields    = ["order_date", "total_price", "status"]

    def get_serializer_class(self):
        if self.action == "list":
            return SalesOrderListSerializer
        if self.action == "create":
            return OrderCreateSerializer
        return SalesOrderDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action in ("retrieve", "cancel"):
            return qs.prefetch_related("lines__product", "shipments__carrier")
        return qs

    def _detail(self, order, **extra):
        order = SalesOrder.objects.prefetch_related(
            "lines__product", "shipments__carrier").get(pk=order.pk)
        data = SalesOrderDetailSerializer(order, context=self.get_serializer_context()).data
        data.update(extra)
        return data

    def create(self, request, *args, **kwargs):
        s = OrderCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = dict(s.validated_data)
        lines_in    = d.pop("lines")
        check_stock = d.pop("check_stock", False)
        is_shop     = d.pop("shop", False)
        if is_shop:
            d.pop("total_price", None)  # магазин: сума рахується лише з цін Minerva
            d.setdefault("payment_status", "unpaid")

        key = request.auth if isinstance(request.auth, APIKey) else None
        d["source"] = (d.get("source") or (key.default_source if key else "") or "api").strip()
        d.setdefault("order_date", timezone.localdate())
        currency = d.get("currency") or "EUR"
        d["currency"] = currency

        existing = SalesOrder.objects.filter(source=d["source"], order_number=d["order_number"]).first()
        if existing:
            return Response(self._detail(existing, created=False), status=status.HTTP_200_OK)

        # Розпізнавання SKU
        resolved, errors = [], []
        for i, ln in enumerate(lines_in):
            if ln.get("product"):
                product = Product.objects.filter(pk=ln["product"]).first()
                raw = ln.get("sku") or (product.sku if product else str(ln["product"]))
            else:
                raw = ln["sku"]
                product = stock_service.resolve_sku(raw)
            if not product:
                errors.append({"line": i, "sku": raw, "error": "Товар не знайдено"})
            elif not product.is_fractional_unit() and ln["qty"] != int(ln["qty"]):
                errors.append({"line": i, "sku": raw, "error": "Кількість має бути цілою (штуки)"})
            elif is_shop and not (product.shop_visible and product.is_active):
                errors.append({"line": i, "sku": raw, "error": "Товар недоступний в інтернет-магазині"})
            elif is_shop and product.shop_effective_price is None:
                errors.append({"line": i, "sku": raw, "error": "Ціна не задана — товар лише за запитом"})
            else:
                resolved.append((product, raw, ln))
        if errors:
            return Response({"detail": "Помилки в рядках замовлення.", "code": "invalid_lines",
                             "lines": errors}, status=status.HTTP_400_BAD_REQUEST)

        # Магазин сам вирішує (check_stock=false → замовлення «під замовлення» при нульовому залишку);
        # для інших джерел діє також заборона від'ємного залишку з налаштувань складу.
        must_check = check_stock or (not is_shop and not InventorySettings.get().allow_negative_stock)
        if d.get("affects_stock", True) and must_check:
            avail = stock_service.check_availability(
                [{"sku": p.sku, "qty": ln["qty"]} for p, _, ln in resolved])
            short = [a for a in avail if not a["ok"]]
            if short:
                return Response({"detail": "Недостатньо товару на складі.", "code": "insufficient_stock",
                                 "items": short}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            order = SalesOrder(**d)
            order.status_source = _key_label(request)
            order.save()
            total = Decimal("0")
            for product, raw, ln in resolved:
                unit = product.shop_effective_price if is_shop else ln.get("unit_price")
                if unit is None:
                    unit = product.sale_price
                line_total = (unit * ln["qty"]) if unit is not None else None
                SalesOrderLine.objects.create(
                    order=order, product=product, sku_raw=raw, qty=ln["qty"],
                    unit_price=unit, total_price=line_total, currency=currency,
                )
                total += line_total or 0
            if d.get("total_price") is None:
                order.total_price = total
                order.save(update_fields=["total_price"])

        return Response(self._detail(order, created=True), status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        serializer.save(status_source=_key_label(self.request))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status in ("shipped", "delivered"):
            return Response({"detail": f"Замовлення вже має статус «{order.status}» — "
                                       f"скасування неможливе.", "code": "already_shipped"},
                            status=status.HTTP_409_CONFLICT)
        restock = str(request.data.get("restock", "true")).lower() not in ("0", "false", "no")
        released = 0
        with transaction.atomic():
            if order.status != "cancelled":
                order.status = "cancelled"
                order.status_source = _key_label(request)
                order.save(update_fields=["status", "status_source"])
            if restock and order.affects_stock:
                released = stock_service.release_order_stock(order)
        return Response(self._detail(order, restocked_transactions=released))


# ── Доставка ──────────────────────────────────────────────────────────────────

class ShipmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Відправлення і трекінг (фільтри: order, order_number, source, status, tracking_number)."""
    resource_scope   = "shipments"
    serializer_class = ShipmentSerializer
    filterset_class  = ShipmentFilter
    ordering_fields  = ["created_at", "submitted_at", "delivered_at"]

    def get_queryset(self):
        return Shipment.objects.select_related("order", "carrier").order_by("-created_at")


# ── Вебхуки ───────────────────────────────────────────────────────────────────

class WebhookViewSet(viewsets.ModelViewSet):
    """
    Вебхуки, зареєстровані ЦИМ ключем (магазин бачить лише свої).
    POST /webhooks/{id}/test/        — надіслати подію ping і повернути результат
    GET  /webhooks/{id}/deliveries/  — останні 50 доставок
    """
    resource_scope   = "webhooks"
    serializer_class = WebhookSerializer
    pagination_class = None

    def get_queryset(self):
        key = self.request.auth if isinstance(self.request.auth, APIKey) else None
        qs = Webhook.objects.all() if key is None else Webhook.objects.filter(api_key=key)
        return qs.order_by("id")

    def perform_create(self, serializer):
        key = self.request.auth if isinstance(self.request.auth, APIKey) else None
        serializer.save(api_key=key)

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        hook = self.get_object()
        (d,) = webhook_service.emit("ping", {"message": "Minerva webhook test", "webhook": hook.name},
                                    only=hook, sync=True)
        d.refresh_from_db()
        return Response(WebhookDeliverySerializer(d).data,
                        status=status.HTTP_200_OK if d.status == "success" else status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"])
    def deliveries(self, request, pk=None):
        hook = self.get_object()
        return Response(WebhookDeliverySerializer(hook.deliveries.all()[:50], many=True).data)


# ── CRM ───────────────────────────────────────────────────────────────────────

class CustomerViewSet(NoDeleteMixin, viewsets.ModelViewSet):
    resource_scope     = 'customers'
    queryset           = Customer.objects.all().order_by("-created_at")
    serializer_class   = CustomerSerializer
    permission_classes = [HasAPIKeyScope]
    filterset_class    = CustomerFilter
    ordering_fields    = ["name", "country", "created_at"]
