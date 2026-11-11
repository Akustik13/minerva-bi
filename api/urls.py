from django.urls import path, include
from rest_framework.routers import DefaultRouter

from . import accounts, mcp, media, payments, purchases, search, site_admin
from .views import (
    SalesOrderViewSet, ProductViewSet, CustomerViewSet, StockViewSet, MovementViewSet,
    LocationViewSet, CategoryViewSet, ShipmentViewSet, WebhookViewSet, ShopProductViewSet, ShopShippingView,
    ShopAssistantView, ping,
)

from . import knowledge_views as kv
from . import meetings_views as mv

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
router.register("purchase-orders", purchases.PurchaseOrderViewSet, basename="purchase-order")

account_urls = [
    path("",                  accounts.StatusView.as_view(),      name="api-account-status"),
    path("register/",         accounts.RegisterView.as_view(),    name="api-account-register"),
    path("resend/",           accounts.ResendView.as_view(),      name="api-account-resend"),
    path("verify/",           accounts.VerifyView.as_view(),      name="api-account-verify"),
    path("login/",            accounts.LoginView.as_view(),       name="api-account-login"),
    path("logout/",           accounts.LogoutView.as_view(),      name="api-account-logout"),
    path("password/forgot/",  accounts.ForgotView.as_view(),      name="api-account-forgot"),
    path("password/reset/",   accounts.ResetView.as_view(),       name="api-account-reset"),
    path("me/",               accounts.MeView.as_view(),          name="api-account-me"),
    path("me/password/",      accounts.PasswordView.as_view(),    name="api-account-password"),
    path("me/orders/",        accounts.OrdersView.as_view(),      name="api-account-orders"),
    path("me/orders/<str:number>/", accounts.OrderDetailView.as_view(), name="api-account-order"),
    path("me/orders/<str:number>/pay/", accounts.OrderPayView.as_view(), name="api-account-order-pay"),
    path("me/orders/<str:number>/documents/<str:name>/", accounts.OrderDocumentView.as_view(),
         name="api-account-order-document"),
    path("me/cart/",          accounts.CartView.as_view(),        name="api-account-cart"),
]

urlpatterns = [
    path("ping/", ping, name="api-ping"),
    path("shop/shipping/", ShopShippingView.as_view(), name="api-shop-shipping"),
    path("shop/assistant/", ShopAssistantView.as_view(), name="api-shop-assistant"),
    path("shop/staff/login/", site_admin.StaffLoginView.as_view(), name="api-shop-staff-login"),
    path("shop/site-events/", site_admin.SiteEventView.as_view(), name="api-shop-site-events"),
    path("shop/pair/", site_admin.PairView.as_view(), name="api-shop-pair"),
    path("shop/site-mail/", site_admin.SiteMailView.as_view(), name="api-shop-site-mail"),
    path("shop/search/assist/", search.ShopSearchAssistView.as_view(), name="api-shop-search-assist"),
    path("shop/account/", include(account_urls)),
    path("shop/media/status/", media.MediaStatusView.as_view(), name="api-shop-media-status"),
    path("shop/media/<str:sha>/", media.MediaFileView.as_view(), name="api-shop-media-file"),
    path("shop/payments/", payments.PaymentsStatusView.as_view(), name="api-payments-status"),
    path("shop/payments/stripe/checkout/", payments.StripeCheckoutView.as_view(), name="api-stripe-checkout"),
    path("shop/payments/stripe/sync/", payments.StripeSyncView.as_view(), name="api-stripe-sync"),
    path("payments/stripe/webhook/<slug:slug>/", payments.StripeWebhookView.as_view(), name="api-stripe-webhook"),
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
    # 🎙 Наради — лише вікно запису на RAG-ПК (ключ meetings:write; логіка — meetings/services.py, README розділ 13)
    path("meetings/redeem/", mv.MeetingRedeemView.as_view(), name="meetings-redeem"),
    path("meetings/sync/", mv.MeetingSyncView.as_view(), name="meetings-sync"),
    path("meetings/ping/", mv.MeetingPingView.as_view(), name="meetings-ping"),
    path("meetings/<int:meeting_id>/status/", mv.MeetingStatusView.as_view(), name="meetings-status"),
    path("meetings/<int:meeting_id>/transcript/", mv.MeetingTranscriptView.as_view(), name="meetings-transcript"),
    path("meetings/<int:meeting_id>/moments/", mv.MeetingMomentsView.as_view(), name="meetings-moments"),
    path("mcp/", mcp.MCPView.as_view(), name="mcp"),   # 🤖 MCP-сервер для зовнішніх агентів (agent/mcp.py)
    path("", include(router.urls)),
]
