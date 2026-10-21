from django.urls import path, include
from rest_framework.routers import DefaultRouter

from . import accounts
from .views import (
    SalesOrderViewSet, ProductViewSet, CustomerViewSet, StockViewSet, MovementViewSet,
    LocationViewSet, CategoryViewSet, ShipmentViewSet, WebhookViewSet, ShopProductViewSet, ShopShippingView, ping,
)

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
    path("me/cart/",          accounts.CartView.as_view(),        name="api-account-cart"),
]

urlpatterns = [
    path("ping/", ping, name="api-ping"),
    path("shop/shipping/", ShopShippingView.as_view(), name="api-shop-shipping"),
    path("shop/account/", include(account_urls)),
    path("", include(router.urls)),
]
