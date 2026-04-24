from django.urls import path
from . import views

app_name = 'ai_assistant'

urlpatterns = [
    path('', views.webchat_view, name='webchat'),
    path('chat/', views.chat_api, name='chat_api'),
    path('reset/', views.reset_chat, name='reset_chat'),
    path('history/', views.history_api, name='history_api'),
    path('diagnostic/', views.tools_diagnostic_view, name='diagnostic'),
    path('diagnostic/run/', views.run_tool_diagnostic, name='diagnostic_run'),
]
