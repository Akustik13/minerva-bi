from django.urls import path
from . import views

urlpatterns = [
    path('my-settings/', views.my_settings_view, name='my_settings'),
    path('set-language/<str:lang_code>/', views.set_language_view, name='set_language'),
    path('ui-dict/<str:lang>.js', views.ui_dict_view, name='ui_dict'),
]
