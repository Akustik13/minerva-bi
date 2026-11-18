from django.urls import path

from sales import views_invoices as v

urlpatterns = [
    path("",                      v.invoice_list,      name="sales_invoice_list"),
    path("new/",                  v.invoice_new,       name="sales_invoice_new"),
    path("<int:pk>/",             v.invoice_edit,      name="sales_invoice_edit"),
    path("<int:pk>/file/<str:kind>/", v.invoice_file,  name="sales_invoice_file"),
    path("<int:pk>/delete/",      v.invoice_delete,    name="sales_invoice_delete"),
    path("template/download/",    v.template_download, name="sales_invoice_template_download"),
    path("template/sample/",      v.template_sample,   name="sales_invoice_template_sample"),
    path("template/upload/",      v.template_upload,   name="sales_invoice_template_upload"),
    path("template/reset/",       v.template_reset,    name="sales_invoice_template_reset"),
]
