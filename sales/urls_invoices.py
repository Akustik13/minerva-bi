from django.urls import path

from sales import views_invoices as v

urlpatterns = [
    path("",                      v.invoice_list,      name="sales_invoice_list"),
    path("new/",                  v.invoice_new,       name="sales_invoice_new"),
    path("from-order/<int:order_pk>/", v.invoice_from_order, name="sales_invoice_from_order"),
    path("<int:pk>/",             v.invoice_edit,      name="sales_invoice_edit"),
    path("<int:pk>/file/<str:kind>/", v.invoice_file,  name="sales_invoice_file"),
    path("<int:pk>/delete/",      v.invoice_delete,    name="sales_invoice_delete"),
    path("<int:pk>/regenerate/",  v.invoice_regenerate, name="sales_invoice_regenerate"),
    path("auto/",                 v.auto_toggle,       name="sales_invoice_auto"),
    path("test-mail/",            v.test_mail,         name="sales_invoice_test_mail"),
    path("template/download/",    v.template_download, name="sales_invoice_template_download"),
    path("template/sample/",      v.template_sample,   name="sales_invoice_template_sample"),
    path("template/upload/",      v.template_upload,   name="sales_invoice_template_upload"),
    path("template/reset/",       v.template_reset,    name="sales_invoice_template_reset"),
]
