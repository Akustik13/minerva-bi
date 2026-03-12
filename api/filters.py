import django_filters
from sales.models import SalesOrder
from inventory.models import Product
from crm.models import Customer


class SalesOrderFilter(django_filters.FilterSet):
    date_from = django_filters.DateFilter(field_name="order_date", lookup_expr="gte")
    date_to   = django_filters.DateFilter(field_name="order_date", lookup_expr="lte")

    class Meta:
        model  = SalesOrder
        fields = ["status", "source", "date_from", "date_to"]


class ProductFilter(django_filters.FilterSet):
    class Meta:
        model  = Product
        fields = ["category", "is_active"]


class CustomerFilter(django_filters.FilterSet):
    class Meta:
        model  = Customer
        fields = ["segment", "country"]
