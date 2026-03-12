from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import SalesOrderViewSet, ProductViewSet, CustomerViewSet

router = DefaultRouter()
router.register("orders",    SalesOrderViewSet, basename="order")
router.register("products",  ProductViewSet,    basename="product")
router.register("customers", CustomerViewSet,   basename="customer")

urlpatterns = [
    path("", include(router.urls)),
]
