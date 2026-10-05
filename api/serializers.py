from decimal import Decimal

from rest_framework import serializers
from sales.models import SalesOrder, SalesOrderLine
from inventory.models import Product, ProductCategory, Location, InventoryTransaction
from crm.models import Customer
from shipping.models import Shipment
from .models import Webhook, WebhookDelivery, WEBHOOK_EVENTS


# ── Склад ─────────────────────────────────────────────────────────────────────

class StockFieldsMixin(serializers.Serializer):
    """Поля залишків — потребують queryset з inventory.services.stock.annotate_stock()."""
    on_hand   = serializers.DecimalField(source="_on_hand",   max_digits=18, decimal_places=3, read_only=True)
    reserved  = serializers.DecimalField(source="_reserved",  max_digits=18, decimal_places=3, read_only=True)
    available = serializers.DecimalField(source="_available", max_digits=18, decimal_places=3, read_only=True)
    incoming  = serializers.DecimalField(source="_incoming",  max_digits=18, decimal_places=3, read_only=True)
    in_stock  = serializers.SerializerMethodField()
    low_stock = serializers.SerializerMethodField()
    last_movement_at = serializers.DateTimeField(source="_last_movement", read_only=True)

    def get_in_stock(self, obj):
        return getattr(obj, "_available", 0) > 0

    def get_low_stock(self, obj):
        return bool(obj.reorder_point) and getattr(obj, "_available", 0) <= obj.reorder_point


class StockSerializer(StockFieldsMixin, serializers.ModelSerializer):
    """Компактний залишок для синхронізації з магазином."""

    class Meta:
        model  = Product
        fields = ["sku", "name", "category", "unit_type", "is_active",
                  "on_hand", "reserved", "available", "incoming",
                  "in_stock", "low_stock", "reorder_point", "lead_time_days",
                  "sale_price", "last_movement_at"]


class StockCheckItemSerializer(serializers.Serializer):
    sku = serializers.CharField()
    qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))


class StockCheckSerializer(serializers.Serializer):
    items = StockCheckItemSerializer(many=True, allow_empty=False)


class MovementSerializer(serializers.ModelSerializer):
    sku      = serializers.CharField(source="product.sku", read_only=True)
    location = serializers.CharField(source="location.code", read_only=True)
    performed_by = serializers.CharField(source="performed_by.username", read_only=True, default=None)

    class Meta:
        model  = InventoryTransaction
        fields = ["id", "tx_type", "sku", "qty", "location", "ref_doc",
                  "external_key", "tx_date", "created_at", "performed_by"]


class MovementCreateSerializer(serializers.Serializer):
    sku      = serializers.CharField()
    tx_type  = serializers.ChoiceField(choices=["Incoming", "Outgoing", "Adjustment"])
    qty      = serializers.DecimalField(max_digits=18, decimal_places=3,
                                        help_text="Incoming/Outgoing — додатне; Adjustment — зі знаком")
    location = serializers.CharField(required=False, allow_blank=True,
                                     help_text="Код локації; за замовчуванням — з налаштувань складу")
    ref_doc  = serializers.CharField(required=False, allow_blank=True, max_length=255)
    external_key = serializers.CharField(required=False, allow_blank=True, max_length=200,
                                         help_text="Ключ ідемпотентності (повтор не дублює рух)")
    tx_date  = serializers.DateTimeField(required=False)


class StockCountSerializer(serializers.Serializer):
    sku      = serializers.CharField()
    qty      = serializers.DecimalField(max_digits=18, decimal_places=3, min_value=Decimal("0"))
    location = serializers.CharField(required=False, allow_blank=True)
    ref_doc  = serializers.CharField(required=False, allow_blank=True, max_length=255)


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Location
        fields = ["id", "code", "name", "location_type", "is_active"]


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model  = ProductCategory
        fields = ["slug", "name", "color", "order"]


# ── Інтернет-магазин ──────────────────────────────────────────────────────────

class ShopProductSerializer(serializers.ModelSerializer):
    """Каталог магазину ключа: лише те, що потрібно сайту (без закупівельних цін).
    Потребує queryset з Prefetch(..., to_attr="_listings") позицій цього магазину."""
    price     = serializers.SerializerMethodField()
    available = serializers.DecimalField(source="_available", max_digits=18, decimal_places=3, read_only=True)
    incoming  = serializers.DecimalField(source="_incoming", max_digits=18, decimal_places=3, read_only=True)
    in_stock  = serializers.SerializerMethodField()
    image_url     = serializers.SerializerMethodField()
    datasheet_url = serializers.SerializerMethodField()
    last_movement_at = serializers.DateTimeField(source="_last_movement", read_only=True)
    price_breaks  = serializers.SerializerMethodField(help_text="[{min_qty, unit_price}] — ціна за шт. від кількості")
    offer  = serializers.SerializerMethodField(help_text="Діюча акція: {percent, until, regular_price, "
                                                         "regular_price_breaks} або null")
    is_new = serializers.SerializerMethodField(help_text="Новинка (бейдж «Neu», угорі каталогу)")

    class Meta:
        model  = Product
        fields = ["sku", "name", "name_export", "category", "unit_type", "manufacturer",
                  "price", "price_breaks", "offer", "is_new", "available", "incoming", "in_stock", "lead_time_days",
                  "net_weight_g", "image_url", "datasheet_url", "last_movement_at"]

    @staticmethod
    def _listing(obj):
        rows = getattr(obj, "_listings", None)
        return rows[0] if rows else None

    def get_price(self, obj):
        from shop.services import price_breaks
        rows = price_breaks(self._listing(obj)) if self._listing(obj) else []
        return float(rows[0]["unit_price"]) if rows else None

    def get_offer(self, obj):
        from shop.services import offer_info
        listing = self._listing(obj)
        info = offer_info(listing) if listing else None
        if not info:
            return None
        return {"percent": float(info["percent"]), "until": info["until"],
                "regular_price": float(info["regular_price"]),
                "regular_price_breaks": [{"min_qty": r["min_qty"], "unit_price": float(r["unit_price"])}
                                         for r in info["regular_price_breaks"]]}

    def get_is_new(self, obj):
        from shop.services import is_new
        listing = self._listing(obj)
        return bool(listing and is_new(listing))

    def get_price_breaks(self, obj):
        from shop.services import price_breaks
        listing = self._listing(obj)
        if not listing:
            return []
        return [{"min_qty": r["min_qty"], "unit_price": float(r["unit_price"])} for r in price_breaks(listing)]

    def get_in_stock(self, obj):
        return getattr(obj, "_available", 0) > 0

    def _abs(self, url):
        request = self.context.get("request")
        if url and request and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url or None

    def get_image_url(self, obj):
        return self._abs(obj.image_display_url)

    def get_datasheet_url(self, obj):
        return self._abs(obj.datasheet_display_url)


# ── Товари ────────────────────────────────────────────────────────────────────

class ProductSerializer(serializers.ModelSerializer):
    image_url     = serializers.SerializerMethodField()
    datasheet_url = serializers.SerializerMethodField()

    class Meta:
        model  = Product
        fields = ["id", "sku", "sku_short", "name", "name_export", "category",
                  "kind", "unit_type", "manufacturer", "purchase_price",
                  "sale_price", "reorder_point", "lead_time_days", "is_active",
                  "hs_code", "country_of_origin", "net_weight_g", "notes",
                  "image_url", "datasheet_url"]

    def _abs(self, url):
        request = self.context.get("request")
        if url and request and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url or None

    def get_image_url(self, obj):
        return self._abs(obj.image_display_url)

    def get_datasheet_url(self, obj):
        return self._abs(obj.datasheet_display_url)


class ProductWithStockSerializer(StockFieldsMixin, ProductSerializer):
    class Meta(ProductSerializer.Meta):
        fields = ProductSerializer.Meta.fields + [
            "on_hand", "reserved", "available", "incoming",
            "in_stock", "low_stock", "last_movement_at",
        ]


# ── Доставка ──────────────────────────────────────────────────────────────────

def carrier_tracking_url(carrier_type, tn):
    """Публічне посилання трекінгу (та сама логіка, що в ShipmentAdmin)."""
    if not tn:
        return None
    ct = (carrier_type or "").lower()
    if ct == "ups" or tn.startswith("1Z"):
        return f"https://www.ups.com/track?tracknum={tn}&requester=WT/trackdetails"
    if ct == "dhl" or tn.startswith(("JD", "00")):
        return f"https://www.dhl.com/en/express/tracking.html?AWB={tn}&brand=DHL"
    if ct == "fedex":
        return f"https://www.fedex.com/apps/fedextrack/?tracknumbers={tn}"
    return None


class ShipmentSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.order_number", read_only=True, default=None)
    order_source = serializers.CharField(source="order.source", read_only=True, default=None)
    carrier      = serializers.CharField(source="carrier.name", read_only=True)
    carrier_type = serializers.CharField(source="carrier.carrier_type", read_only=True)
    tracking_url = serializers.SerializerMethodField()

    class Meta:
        model  = Shipment
        fields = ["id", "order", "order_source", "order_number", "status",
                  "carrier", "carrier_type", "carrier_service",
                  "tracking_number", "tracking_url", "carrier_status_label",
                  "carrier_delayed", "eta_from", "eta_to", "delivered_at",
                  "recipient_name", "recipient_country",
                  "created_at", "submitted_at"]

    def get_tracking_url(self, obj):
        return carrier_tracking_url(obj.carrier.carrier_type, obj.tracking_number)


# ── Замовлення ────────────────────────────────────────────────────────────────

class SalesOrderLineSerializer(serializers.ModelSerializer):
    product_sku = serializers.CharField(source="product.sku", read_only=True)

    class Meta:
        model  = SalesOrderLine
        fields = ["id", "product", "product_sku", "sku_raw", "qty",
                  "unit_price", "total_price", "currency"]


class SalesOrderListSerializer(serializers.ModelSerializer):
    """Compact serializer used for list endpoint (no nested lines)."""

    class Meta:
        model  = SalesOrder
        fields = ["id", "source", "order_number", "order_date", "status",
                  "client", "total_price", "currency", "affects_stock",
                  "shipped_at", "tracking_number", "addr_country"]


class SalesOrderDetailSerializer(serializers.ModelSerializer):
    """Full serializer used for retrieve / patch."""

    lines     = SalesOrderLineSerializer(many=True, read_only=True)
    shipments = ShipmentSerializer(many=True, read_only=True)

    class Meta:
        model  = SalesOrder
        fields = "__all__"
        read_only_fields = ["source", "order_number", "customer_key", "status_source"]


class OrderLineInputSerializer(serializers.Serializer):
    sku        = serializers.CharField(required=False, help_text="SKU або аліас товару")
    product    = serializers.IntegerField(required=False, help_text="ID товару (альтернатива sku)")
    qty        = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get("sku") and not attrs.get("product"):
            raise serializers.ValidationError("Вкажіть 'sku' або 'product'.")
        return attrs


class OrderCreateSerializer(serializers.ModelSerializer):
    """
    Створення замовлення зовнішнім магазином. Рядки — за SKU.
    Логіка створення — в views.SalesOrderViewSet.create (потрібен контекст ключа).
    """
    source      = serializers.CharField(required=False, allow_blank=True, max_length=32)
    lines       = OrderLineInputSerializer(many=True, allow_empty=False)
    check_stock = serializers.BooleanField(required=False, default=False, write_only=True,
                                           help_text="Відхилити (409), якщо товару недостатньо")
    note        = serializers.CharField(required=False, allow_blank=True, max_length=500,
                                        write_only=True, source="internal_note")
    shop        = serializers.BooleanField(
        required=False, default=False, write_only=True,
        help_text="Замовлення магазину ключа: лише видимі позиції цього магазину, ціни (зі ступенями) — "
                  "завжди з Minerva, unit_price/total_price ігноруються",
    )
    quote       = serializers.BooleanField(
        required=False, default=False, write_only=True,
        help_text="Запит пропозиції: тип документа QUOTE, без списання складу і без цін; "
                  "невідомі SKU записуються в нотатку",
    )

    class Meta:
        model  = SalesOrder
        fields = [
            "source", "order_number", "order_date", "document_type", "affects_stock",
            "status", "shipping_deadline", "currency", "total_price",
            "shipping_cost", "shipping_currency", "shipping_courier",
            "client", "contact_name", "email", "phone", "buyer_vat_id",
            "addr_street", "addr_city", "addr_zip", "addr_state", "addr_country",
            "ship_name", "ship_company", "ship_phone", "ship_email", "ship_vat_id",
            "shipping_address", "note", "lines", "check_stock", "shop", "quote",
            "payment_method", "payment_status", "payment_reference",
        ]
        validators = []  # дублікат (source, order_number) обробляється ідемпотентно у view

    def validate_addr_country(self, v):
        return (v or "").upper()


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Customer
        fields = ["id", "external_key", "name", "email", "phone", "company",
                  "country", "addr_street", "addr_city", "addr_zip",
                  "segment", "status", "source", "notes",
                  "created_at", "updated_at"]
        read_only_fields = ["external_key", "created_at", "updated_at"]


# ── Вебхуки ───────────────────────────────────────────────────────────────────

class WebhookSerializer(serializers.ModelSerializer):
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=[e for e, _ in WEBHOOK_EVENTS]),
        required=False, help_text="Порожньо = всі події",
    )

    class Meta:
        model  = Webhook
        fields = ["id", "name", "url", "events", "order_sources", "is_active", "secret",
                  "created_at", "last_delivery_at", "last_status_code", "consecutive_failures"]
        read_only_fields = ["secret", "created_at", "last_delivery_at", "last_status_code",
                            "consecutive_failures"]

    def validate_url(self, v):
        if not v.lower().startswith(("https://", "http://")):
            raise serializers.ValidationError("Лише http(s) URL.")
        return v


class WebhookDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model  = WebhookDelivery
        fields = ["id", "event", "status", "attempts", "response_code", "response_body",
                  "next_attempt_at", "created_at", "delivered_at", "payload"]
