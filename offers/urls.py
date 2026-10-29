from django.urls import path

from . import views

app_name = "offers"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("<int:pk>/delete/", views.delete_view, name="delete"),
    path("quick/", views.quick_view, name="quick"),
    path("<int:pk>/quick/", views.quick_view, name="quick_edit"),
    path("api/currency/", views.currency_view, name="currency"),
    path("api/products/", views.products_view, name="products"),
    path("api/price/", views.price_view, name="price"),
    path("template/download/", views.template_download_view, name="template_download"),
    path("template/upload/", views.template_upload_view, name="template_upload"),
    path("template/reset/", views.template_reset_view, name="template_reset"),
    path("template/sample/", views.template_sample_view, name="template_sample"),
    path("api/customer/create/", views.customer_create_view, name="customer_create"),
    path("api/customer/<int:pk>/", views.customer_view, name="customer"),
    path("api/from-thread/<int:thread_pk>/", views.from_thread_view, name="from_thread"),
    path("api/message/<int:message_pk>/rescan/", views.rescan_message_view, name="rescan_message"),
    path("api/quote-request/<int:pk>/dismiss/", views.dismiss_quote_request_view, name="quote_request_dismiss"),
]
