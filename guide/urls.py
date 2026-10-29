from django.urls import path

from . import views

app_name = "guide"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/search/", views.search_api, name="search"),
    path("<path:slug>/", views.page, name="page"),
]
