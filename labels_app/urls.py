from django.urls import path
from . import views

app_name = 'labels'

urlpatterns = [
    path('',                            views.list_labels,           name='list'),
    path('list/',                       views.list_labels,           name='list_alt'),
    path('serve/<str:sku>/',            views.serve_label,           name='serve'),
    path('status/',                     views.label_status,          name='status'),
    path('upload/',                     views.upload_label,          name='upload'),
    path('delete/<str:sku>/',           views.delete_label,          name='delete'),
    path('preview/',                    views.preview_cable_label,   name='preview'),
    path('generate/',                   views.generate_cable_label,  name='generate'),
    path('product/check/',              views.product_check,         name='product_check'),
    path('product/save/',               views.product_save,          name='product_save'),
    path('product/<str:sku>/',          views.product_label_file,    name='product'),
    # старі адреси етикеток антен (сторінки, відкриті до оновлення)
    path('antenna/check/',              views.product_check),
    path('antenna/save/',               views.product_save),
    path('antenna/<str:sku>/',          views.product_label_file),
    path('order/<int:pk>/',             views.order_label,           name='order'),
]
