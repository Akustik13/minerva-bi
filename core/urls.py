from django.urls import path
from . import views

urlpatterns = [
    path('my-settings/', views.my_settings_view, name='my_settings'),
]
