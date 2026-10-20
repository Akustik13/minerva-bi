from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SalesOrderViewSet, ProductViewSet, CustomerViewSet, StockViewSet, MovementViewSet,
    LocationViewSet, CategoryViewSet, ShipmentViewSet, WebhookViewSet, ShopProductViewSet, ShopShippingView, ping,
)

from . import knowledge_views as kv

router = DefaultRouter()
router.register("stock",           StockViewSet,      basename="stock")
router.register("stock-movements", MovementViewSet,   basename="stock-movement")
router.register("locations",       LocationViewSet,   basename="location")
router.register("categories",      CategoryViewSet,   basename="category")
router.register("shop/products",   ShopProductViewSet, basename="shop-product")
router.register("products",        ProductViewSet,    basename="product")
router.register("orders",          SalesOrderViewSet, basename="order")
router.register("shipments",       ShipmentViewSet,   basename="shipment")
router.register("customers",       CustomerViewSet,   basename="customer")
router.register("webhooks",        WebhookViewSet,    basename="webhook")

urlpatterns = [
    path("ping/", ping, name="api-ping"),
    path("shop/shipping/", ShopShippingView.as_view(), name="api-shop-shipping"),
    # База знань для RAG (api/schemas/knowledge)
    path("knowledge/changes/", kv.ChangesView.as_view(), name="knowledge-changes"),
    path("knowledge/documents/<str:document_id>/", kv.DocumentView.as_view(), name="knowledge-document"),
    path("knowledge/documents/<str:document_id>/attachments/<int:index>/", kv.AttachmentView.as_view(),
         name="knowledge-attachment"),
    path("knowledge/manifest/snapshots/", kv.SnapshotCreateView.as_view(), name="knowledge-snapshots"),
    path("knowledge/manifest/snapshots/<uuid:snapshot_id>/", kv.SnapshotPageView.as_view(),
         name="knowledge-snapshot"),
    path("knowledge/status/", kv.StatusView.as_view(), name="knowledge-status"),
    path("knowledge/summary/", kv.SummaryView.as_view(), name="knowledge-summary"),
    path("", include(router.urls)),
]
