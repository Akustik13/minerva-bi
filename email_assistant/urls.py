from django.urls import path
from . import views

app_name = 'email_assistant'

urlpatterns = [
    path('',                                             views.inbox_view,           name='inbox'),
    # Full-page standalone views (open separately)
    path('thread/<int:thread_pk>/',                      views.thread_view,          name='thread'),
    path('message/<int:message_pk>/',                    views.message_view,         name='message'),
    # AJAX preview fragments (3-panel inbox)
    path('thread/<int:thread_pk>/preview/',              views.thread_preview_view,  name='thread_preview'),
    path('message/<int:message_pk>/preview/',            views.message_preview_view, name='message_preview'),
    # Compose
    path('compose/',                                     views.compose_view,         name='compose'),
    path('send/',                                        views.send_api,             name='send_api'),
    # Sync & counts
    path('sync/',                                        views.sync_now,             name='sync_now'),
    path('unread-count/',                                views.unread_count_view,    name='unread_count'),
    # AI
    path('message/<int:message_pk>/ai-reply/',           views.ai_suggest_reply,     name='ai_reply'),
    path('message/<int:message_pk>/ai-translate/',       views.ai_translate,         name='ai_translate'),
    # Actions
    path('message/<int:message_pk>/star/',               views.toggle_star_view,     name='toggle_star'),
    path('message/<int:message_pk>/spam/',               views.toggle_spam_view,     name='toggle_spam'),
    path('message/<int:message_pk>/delete/',             views.delete_message_view,  name='delete_message'),
    path('message/<int:message_pk>/restore/',            views.restore_message_view, name='restore_message'),
    path('thread/<int:thread_pk>/archive/',              views.archive_thread_view,  name='archive_thread'),
    path('thread/<int:thread_pk>/unarchive/',            views.unarchive_thread_view, name='unarchive_thread'),
    # HTML body for iframe
    path('message/<int:message_pk>/html/',               views.message_html_view,    name='message_html'),
    # IMAP custom folders
    path('imap-folders/',                                views.list_imap_folders_view,  name='imap_folders'),
    path('sync-imap-folder/',                            views.sync_imap_folder_view,   name='sync_imap_folder'),
    path('sync-all-folders/',                            views.sync_all_folders_view,   name='sync_all_folders'),
    # Scheduled send
    path('schedule/',                                    views.schedule_email_api,      name='schedule_email'),
    # AI compose
    path('ai-generate/',                                 views.ai_generate_email,       name='ai_generate'),
    path('ai-grammar/',                                  views.ai_grammar_check,        name='ai_grammar'),
    # CRM contacts for autocomplete
    path('crm-contacts/',                                views.crm_contacts_view,       name='crm_contacts'),
]
