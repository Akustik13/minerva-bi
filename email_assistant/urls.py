from django.urls import path
from . import views

app_name = 'email_assistant'

urlpatterns = [
    path('',                                          views.inbox_view,          name='inbox'),
    path('thread/<int:thread_pk>/',                   views.thread_view,         name='thread'),
    path('message/<int:message_pk>/',                 views.message_view,        name='message'),
    path('compose/',                                  views.compose_view,        name='compose'),
    path('send/',                                     views.send_api,            name='send_api'),
    path('sync/',                                     views.sync_now,            name='sync_now'),
    path('unread-count/',                             views.unread_count_view,   name='unread_count'),
    path('message/<int:message_pk>/ai-reply/',        views.ai_suggest_reply,    name='ai_reply'),
    path('message/<int:message_pk>/ai-translate/',    views.ai_translate,        name='ai_translate'),
    path('message/<int:message_pk>/delete/',          views.delete_message_view, name='delete_message'),
    path('message/<int:message_pk>/restore/',         views.restore_message_view, name='restore_message'),
    path('thread/<int:thread_pk>/archive/',           views.archive_thread_view,   name='archive_thread'),
    path('thread/<int:thread_pk>/unarchive/',         views.unarchive_thread_view, name='unarchive_thread'),
]
