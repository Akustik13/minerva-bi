from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SalesOrderViewSet, ProductViewSet, CustomerViewSet, StockViewSet, MovementViewSet,
    LocationViewSet, CategoryViewSet, ShipmentViewSet, WebhookViewSet, ShopProductViewSet, ShopShippingView, RagProductViewSet, AttributeFacetsView, ping,
)

router = DefaultRouter()
router.register("stock",           StockViewSet,      basename="stock")
router.register("stock-movements", MovementViewSet,   basename="stock-movement")
router.register("locations",       LocationViewSet,   basename="location")
router.register("categories",      CategoryViewSet,   basename="category")
router.register("shop/products",   ShopProductViewSet, basename="shop-product")
router.register("rag/products",    RagProductViewSet,  basename="rag-product")
router.register("products",        ProductViewSet,    basename="product")
router.register("orders",          SalesOrderViewSet, basename="order")
router.register("shipments",       ShipmentViewSet,   basename="shipment")
router.register("customers",       CustomerViewSet,   basename="customer")
router.register("webhooks",        WebhookViewSet,    basename="webhook")

urlpatterns = [
    path("ping/", ping, name="api-ping"),
    path("shop/shipping/", ShopShippingView.as_view(), name="api-shop-shipping"),
    path("attributes/", AttributeFacetsView.as_view(), name="api-attributes"),
    path("", include(router.urls)),
]
