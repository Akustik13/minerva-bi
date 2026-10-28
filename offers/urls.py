from django.urls import path

from . import views

app_name = "offers"

urlpatterns = [
    path("quick/", views.quick_view, name="quick"),
    path("<int:pk>/quick/", views.quick_view, name="quick_edit"),
    path("api/price/", views.price_view, name="price"),
    path("api/customer/<int:pk>/", views.customer_view, name="customer"),
    path("api/from-thread/<int:thread_pk>/", views.from_thread_view, name="from_thread"),
    path("api/message/<int:message_pk>/rescan/", views.rescan_message_view, name="rescan_message"),
    path("api/quote-request/<int:pk>/dismiss/", views.dismiss_quote_request_view, name="quote_request_dismiss"),
]
