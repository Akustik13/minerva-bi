from django.urls import path
from . import views

app_name = "bots"

urlpatterns = [
    path("digikey/oauth-callback/", views.digikey_oauth_callback, name="digikey_oauth_callback"),
    path("digikey/webhook/",        views.digikey_webhook,         name="digikey_webhook"),
    path("digikey/packlist/<int:order_pk>/", views.digikey_packlist, name="digikey_packlist"),
]
