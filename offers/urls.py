from django.urls import path

from . import views

app_name = "offers"

urlpatterns = [
    path("api/from-thread/<int:thread_pk>/", views.from_thread_view, name="from_thread"),
    path("api/quote-request/<int:pk>/dismiss/", views.dismiss_quote_request_view, name="quote_request_dismiss"),
]
