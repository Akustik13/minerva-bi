from django.urls import path
from shipping import views_commercial_invoice as v

urlpatterns = [
    path("",                          v.ci_list,              name="commercial_invoice_list"),
    path("new/",                      v.ci_new,               name="commercial_invoice_new"),
    path("<int:pk>/edit/",            v.ci_edit,              name="commercial_invoice_edit"),
    path("<int:pk>/file/<str:kind>/", v.ci_file,              name="commercial_invoice_file"),
    path("<int:pk>/delete/",          v.ci_delete,            name="commercial_invoice_delete"),
    path("template/download/",        v.ci_template_download, name="commercial_invoice_template_download"),
    path("template/upload/",          v.ci_template_upload,   name="commercial_invoice_template_upload"),
    path("template/reset/",           v.ci_template_reset,    name="commercial_invoice_template_reset"),
]
