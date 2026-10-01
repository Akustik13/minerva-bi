from django.urls import path
from shipping import views_packing_list as v

urlpatterns = [
    path("",                          v.pl_list,              name="packing_list_list"),
    path("new/",                      v.pl_new,               name="packing_list_new"),
    path("<int:pk>/edit/",            v.pl_edit,              name="packing_list_edit"),
    path("<int:pk>/file/<str:kind>/", v.pl_file,              name="packing_list_file"),
    path("<int:pk>/delete/",          v.pl_delete,            name="packing_list_delete"),
    path("template/download/",        v.pl_template_download, name="packing_list_template_download"),
    path("template/upload/",          v.pl_template_upload,   name="packing_list_template_upload"),
    path("template/reset/",           v.pl_template_reset,    name="packing_list_template_reset"),
]
