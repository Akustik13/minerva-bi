from django.urls import path
from . import views

app_name = 'labels'

urlpatterns = [
    path('serve/<str:sku>/',  views.serve_label,  name='serve'),
    path('status/',           views.label_status,  name='status'),
    path('upload/',           views.upload_label,  name='upload'),
    path('list/',             views.list_labels,   name='list'),
]
