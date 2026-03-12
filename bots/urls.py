from django.urls import path
from . import views

app_name = "bots"

urlpatterns = [
    path("digikey/oauth-callback/", views.digikey_oauth_callback, name="digikey_oauth_callback"),
]
