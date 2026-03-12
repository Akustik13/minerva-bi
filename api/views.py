from rest_framework import viewsets, status
from rest_framework.response import Response

from .permissions import HasAPIKeyScope
from .filters import SalesOrderFilter, ProductFilter, CustomerFilter
from .serializers import (
    SalesOrderListSerializer, SalesOrderDetailSerializer,
    ProductSerializer, CustomerSerializer,
)
from sales.models import SalesOrder
from inventory.models import Product
from crm.models import Customer


class NoDeleteMixin:
    """Blocks DELETE on all endpoints — returns HTTP 405."""

    def destroy(self, request, *args, **kwargs):
        return Response(
            {"detail": "DELETE не підтримується."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class SalesOrderViewSet(NoDeleteMixin, viewsets.ModelViewSet):
    resource_scope     = 'orders'
    queryset           = SalesOrder.objects.all().order_by("-order_date", "-id")
    permission_classes = [HasAPIKeyScope]
    filterset_class    = SalesOrderFilter
    ordering_fields    = ["order_date", "total_price", "status"]

    def get_serializer_class(self):
        if self.action == "list":
            return SalesOrderListSerializer
        return SalesOrderDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "retrieve":
            return qs.prefetch_related("lines__product")
        return qs


class ProductViewSet(NoDeleteMixin, viewsets.ModelViewSet):
    resource_scope     = 'products'
    queryset           = Product.objects.all().order_by("sku")
    serializer_class   = ProductSerializer
    permission_classes = [HasAPIKeyScope]
    filterset_class    = ProductFilter
    ordering_fields    = ["sku", "sale_price", "purchase_price"]


class CustomerViewSet(NoDeleteMixin, viewsets.ModelViewSet):
    resource_scope     = 'customers'
    queryset           = Customer.objects.all().order_by("-created_at")
    serializer_class   = CustomerSerializer
    permission_classes = [HasAPIKeyScope]
    filterset_class    = CustomerFilter
    ordering_fields    = ["name", "country", "created_at"]
