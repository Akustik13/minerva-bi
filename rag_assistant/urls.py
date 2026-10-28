from django.urls import path

from . import views, views_entity

app_name = "rag_assistant"

urlpatterns = [
    path("avatar.png", views.avatar, name="avatar"),
    path("api/state/", views.state, name="state"),
    path("api/conversations/new/", views.conversation_new, name="conversation_new"),
    path("api/conversations/<int:pk>/", views.conversation_detail, name="conversation_detail"),
    path("api/conversations/<int:pk>/delete/", views.conversation_delete, name="conversation_delete"),
    path("api/conversations/<int:pk>/ask/", views.ask, name="ask"),
    path("api/messages/<int:pk>/", views.message, name="message"),
    path("api/entity/help/<int:pk>/", views_entity.entity_help_state, name="entity_help"),
    path("api/entity/<str:kind>/<int:pk>/latest/", views_entity.entity_latest, name="entity_latest"),
    path("api/entity/<str:kind>/<int:pk>/<str:mode>/", views_entity.entity_start, name="entity_start"),
]
