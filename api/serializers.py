from rest_framework import serializers
from sales.models import SalesOrder, SalesOrderLine
from inventory.models import Product
from crm.models import Customer


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
                  "shipped_at", "addr_country"]


class SalesOrderDetailSerializer(serializers.ModelSerializer):
    """Full serializer used for retrieve / create / patch."""

    lines = SalesOrderLineSerializer(many=True, required=False)

    class Meta:
        model  = SalesOrder
        fields = "__all__"

    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        order = SalesOrder.objects.create(**validated_data)
        for line_data in lines_data:
            SalesOrderLine.objects.create(order=order, **line_data)
        return order

    def update(self, instance, validated_data):
        # Lines are not updated via PATCH on the order itself; use line endpoints.
        validated_data.pop("lines", None)
        return super().update(instance, validated_data)


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Product
        fields = ["id", "sku", "sku_short", "name", "name_export", "category",
                  "kind", "unit_type", "manufacturer", "purchase_price",
                  "sale_price", "reorder_point", "lead_time_days", "is_active",
                  "hs_code", "country_of_origin", "net_weight_g", "notes"]


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Customer
        fields = ["id", "external_key", "name", "email", "phone", "company",
                  "country", "addr_street", "addr_city", "addr_zip",
                  "segment", "status", "source", "notes",
                  "created_at", "updated_at"]
        read_only_fields = ["external_key", "created_at", "updated_at"]
