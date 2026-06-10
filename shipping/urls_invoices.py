from django.urls import path
from shipping import views_invoice as v

urlpatterns = [
    path("",                             v.invoice_list,          name="invoice_list"),
    path("<int:pk>/",                    v.invoice_detail,        name="invoice_detail"),
    path("generate/",                    v.invoice_generate,      name="invoice_generate"),
    path("register/",                    v.invoice_register,      name="invoice_register"),
    path("fetch/<str:dk_order_no>/",     v.invoice_fetch_preview, name="invoice_fetch_preview"),
    path("<int:pk>/download/",           v.invoice_download,      name="invoice_download"),
    path("<int:pk>/delete/",             v.invoice_delete,        name="invoice_delete"),
]
