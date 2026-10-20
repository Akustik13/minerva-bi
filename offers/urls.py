from django.urls import path

from . import views

app_name = "offers"

urlpatterns = [
    path("api/from-thread/<int:thread_pk>/", views.from_thread_view, name="from_thread"),
]
