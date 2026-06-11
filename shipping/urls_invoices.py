from django.urls import path
from shipping import views_invoice as v

urlpatterns = [
    path("",                             v.invoice_list,          name="invoice_list"),
    path("<int:pk>/",                    v.invoice_detail,        name="invoice_detail"),
    path("next-number/",                 v.invoice_next_number,   name="invoice_next_number"),
    path("generate/",                    v.invoice_generate,      name="invoice_generate"),
    path("register/",                    v.invoice_register,      name="invoice_register"),
    path("fetch/<str:dk_order_no>/",     v.invoice_fetch_preview, name="invoice_fetch_preview"),
    path("<int:pk>/pdf/",                v.invoice_pdf_view,      name="invoice_pdf"),
    path("<int:pk>/download/",           v.invoice_download,      name="invoice_download"),
    path("<int:pk>/delete/",             v.invoice_delete,        name="invoice_delete"),
    path("<int:pk>/update-recipient/",   v.invoice_update_recipient,   name="invoice_update_recipient"),
    path("dk-orders/",                   v.invoice_dk_orders,           name="invoice_dk_orders"),
    path("check-number/",                v.invoice_check_number,        name="invoice_check_number"),
    path("template-sample/",             v.invoice_template_sample,    name="invoice_template_sample"),
    # Template management
    path("templates/",                   v.invoice_templates_list,     name="invoice_templates_list"),
    path("templates/upload/",            v.invoice_template_upload,    name="invoice_template_upload"),
    path("templates/<str:name>/download/", v.invoice_template_download, name="invoice_template_download"),
    path("templates/<str:name>/activate/", v.invoice_template_activate, name="invoice_template_activate"),
    path("templates/<str:name>/delete/",   v.invoice_template_delete,   name="invoice_template_delete"),
]
