from django.urls import path
from . import views

app_name = 'ai_assistant'

urlpatterns = [
    path('', views.webchat_view, name='webchat'),
    path('chat/', views.chat_api, name='chat_api'),
    path('reset/', views.reset_chat, name='reset_chat'),
]
