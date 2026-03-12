from django.urls import path
from . import views

app_name = "accounting"

urlpatterns = [
    path("invoice/<int:pk>/pdf/", views.invoice_pdf, name="invoice_pdf"),
]
