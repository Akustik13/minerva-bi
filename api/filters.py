import django_filters
from django.db.models import F, Q
from sales.models import SalesOrder
from inventory.models import Product, InventoryTransaction
from crm.models import Customer
from shipping.models import Shipment


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """?sku=A,B,C"""


class SalesOrderFilter(django_filters.FilterSet):
    date_from    = django_filters.DateFilter(field_name="order_date", lookup_expr="gte")
    date_to      = django_filters.DateFilter(field_name="order_date", lookup_expr="lte")
    order_number = django_filters.CharFilter(field_name="order_number")
    email        = django_filters.CharFilter(field_name="email", lookup_expr="iexact")

    class Meta:
        model  = SalesOrder
        fields = ["status", "source", "order_number", "email", "date_from", "date_to"]


class ProductFilter(django_filters.FilterSet):
    """Фільтри товарів/залишків. low_stock/in_stock потребують annotate_stock()."""
    sku       = CharInFilter(field_name="sku", lookup_expr="in")
    search    = django_filters.CharFilter(method="filter_search")
    in_stock  = django_filters.BooleanFilter(method="filter_in_stock")
    low_stock = django_filters.BooleanFilter(method="filter_low_stock")
    changed_since = django_filters.IsoDateTimeFilter(
        field_name="_last_movement", lookup_expr="gte",
        help_text="Лише товари з рухами після цієї дати (інкрементальна синхронізація)",
    )
    updated_since = django_filters.IsoDateTimeFilter(
        field_name="updated_at", lookup_expr="gte",
        help_text="Лише товари, картку яких змінено після цієї дати (атрибути, ціни, статус…)",
    )
    lifecycle_status = django_filters.CharFilter(field_name="lifecycle_status")
    attr = django_filters.CharFilter(
        method="filter_attr",
        help_text="Технічний атрибут: «Назва:значення» (значення — входження, без регістру) або «Назва» "
                  "(параметр є). Можна кілька: ?attr=Antenna Type:PCB&attr=RF Family/Standard:Bluetooth",
    )

    class Meta:
        model  = Product
        fields = ["sku", "category", "kind", "is_active", "search",
                  "in_stock", "low_stock", "changed_since", "updated_since", "lifecycle_status", "attr"]

    def filter_attr(self, qs, name, value):
        for item in self.request.GET.getlist("attr") if self.request is not None else [value]:
            key, sep, val = item.partition(":")
            key, val = key.strip(), val.strip()
            if not key or "__" in key:
                continue
            if sep and val:
                qs = qs.filter(**{f"tech_attributes__{key}__icontains": val})
            else:
                qs = qs.filter(tech_attributes__has_key=key)
        return qs

    def filter_search(self, qs, name, value):
        return qs.filter(Q(sku__icontains=value) | Q(name__icontains=value)
                         | Q(sku_short__icontains=value) | Q(manufacturer__icontains=value))

    def filter_in_stock(self, qs, name, value):
        return qs.filter(_available__gt=0) if value else qs.filter(_available__lte=0)

    def filter_low_stock(self, qs, name, value):
        low = Q(reorder_point__gt=0, _available__lte=F("reorder_point"))
        return qs.filter(low) if value else qs.exclude(low)


class MovementFilter(django_filters.FilterSet):
    sku       = CharInFilter(field_name="product__sku", lookup_expr="in")
    location  = django_filters.CharFilter(field_name="location__code")
    date_from = django_filters.IsoDateTimeFilter(field_name="tx_date", lookup_expr="gte")
    date_to   = django_filters.IsoDateTimeFilter(field_name="tx_date", lookup_expr="lte")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gt")
    ref_doc   = django_filters.CharFilter(field_name="ref_doc", lookup_expr="icontains")

    class Meta:
        model  = InventoryTransaction
        fields = ["tx_type", "sku", "location", "date_from", "date_to", "created_after", "ref_doc"]


class ShipmentFilter(django_filters.FilterSet):
    order_number    = django_filters.CharFilter(field_name="order__order_number")
    source          = django_filters.CharFilter(field_name="order__source")
    tracking_number = django_filters.CharFilter(field_name="tracking_number")
    created_after   = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")

    class Meta:
        model  = Shipment
        fields = ["status", "order", "order_number", "source", "tracking_number", "created_after"]


class CustomerFilter(django_filters.FilterSet):
    class Meta:
        model  = Customer
        fields = ["segment", "country"]
