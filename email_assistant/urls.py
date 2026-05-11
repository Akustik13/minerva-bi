from django.urls import path
from . import views

app_name = 'email_assistant'

urlpatterns = [
    path('',                                   views.inbox_view,       name='inbox'),
    path('thread/<int:thread_pk>/',            views.thread_view,      name='thread'),
    path('message/<int:message_pk>/',          views.message_view,     name='message'),
    path('compose/',                           views.compose_view,     name='compose'),
    path('send/',                              views.send_api,         name='send_api'),
    path('sync/',                              views.sync_now,         name='sync_now'),
    path('message/<int:message_pk>/ai-reply/', views.ai_suggest_reply, name='ai_reply'),
    path('message/<int:message_pk>/ai-translate/', views.ai_translate, name='ai_translate'),
]
