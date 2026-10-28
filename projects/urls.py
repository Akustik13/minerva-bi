from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("api/thread/<int:thread_pk>/", views.thread_view, name="thread"),
    path("api/thread/<int:thread_pk>/link/", views.link_view, name="link"),
    path("api/thread/<int:thread_pk>/create/", views.create_view, name="create"),
    path("api/link/<int:link_pk>/decide/", views.decide_view, name="decide"),
    path("api/link/<int:link_pk>/remove/", views.remove_view, name="remove"),
    path("api/project/<int:pk>/overview/", views.overview_view, name="overview"),
    path("api/project/<int:pk>/rename/", views.rename_view, name="rename"),
    path("api/project/<int:pk>/overview/next-action/", views.overview_next_action_view, name="overview_next_action"),
]
