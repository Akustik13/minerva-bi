from django.urls import path
from . import views

urlpatterns = [
    path("", views.onboarding, name="onboarding"),
    path("demo/", views.demo, name="demo"),
    path("delete-demo/", views.delete_demo, name="delete_demo"),
    path("clear-system/", views.clear_system, name="clear_system"),
]
