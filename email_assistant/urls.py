from django.urls import path
from . import views, views_categories, views_sort

app_name = 'email_assistant'

urlpatterns = [
    path('',                                             views.inbox_view,           name='inbox'),
    # Full-page standalone views (open separately)
    path('thread/<int:thread_pk>/',                      views.thread_view,          name='thread'),
    path('thread/<int:thread_pk>/knowledge/',            views.thread_knowledge_view, name='thread_knowledge'),
    path('message/<int:message_pk>/rag-search/',         views.rag_search_view,      name='rag_search'),
    path('rag-search/<uuid:job_id>/',                    views.rag_search_poll_view, name='rag_search_poll'),
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
    # «📋 Підготувати відповідь»: бриф → лист у фоні (reply_brief.py)
    path('message/<int:message_pk>/ai-prepare/',         views.ai_prepare_view,      name='ai_prepare'),
    path('message/<int:message_pk>/brief/',              views.message_brief_view,   name='message_brief'),
    path('brief/<int:brief_pk>/',                         views.reply_brief_view,     name='reply_brief'),
    path('brief/<int:brief_pk>/compose/',                 views.reply_brief_compose_view, name='reply_brief_compose'),
    path('brief/<int:brief_pk>/rewrite/',                 views.reply_brief_rewrite_view, name='reply_brief_rewrite'),
    path('message/<int:message_pk>/ai-translate/',       views.ai_translate,         name='ai_translate'),
    path('message/<int:message_pk>/history/',            views.history_view,         name='history'),
    # Actions
    path('message/<int:message_pk>/star/',               views.toggle_star_view,     name='toggle_star'),
    path('message/<int:message_pk>/spam/',               views.toggle_spam_view,     name='toggle_spam'),
    path('message/<int:message_pk>/delete/',             views.delete_message_view,  name='delete_message'),
    path('message/<int:message_pk>/restore/',            views.restore_message_view, name='restore_message'),
    path('thread/<int:thread_pk>/archive/',              views.archive_thread_view,  name='archive_thread'),
    path('thread/<int:thread_pk>/unarchive/',            views.unarchive_thread_view, name='unarchive_thread'),
    # HTML body for iframe
    path('message/<int:message_pk>/html/',               views.message_html_view,    name='message_html'),
    # Attachments
    path('message/<int:message_pk>/attachment/<int:index>/', views.attachment_download_view, name='email_attachment'),
    # IMAP custom folders
    path('imap-folders/',                                views.list_imap_folders_view,  name='imap_folders'),
    path('sync-imap-folder/',                            views.sync_imap_folder_view,   name='sync_imap_folder'),
    path('sync-all-folders/',                            views.sync_all_folders_view,   name='sync_all_folders'),
    # IMAP folder management
    path('imap-folder/create/',                          views.imap_create_folder_view, name='imap_create_folder'),
    path('imap-folder/rename/',                          views.imap_rename_folder_view, name='imap_rename_folder'),
    path('imap-folder/delete/',                          views.imap_delete_folder_view, name='imap_delete_folder'),
    # Email export / import
    path('export/',                                      views.export_emails_view,      name='export_emails'),
    path('import/',                                      views.import_emails_view,      name='import_emails'),
    # Scheduled send
    path('schedule/',                                    views.schedule_email_api,      name='schedule_email'),
    # AI compose
    path('ai-generate/',                                 views.ai_generate_email,       name='ai_generate'),
    path('ai-grammar/',                                  views.ai_grammar_check,        name='ai_grammar'),
    # CRM contacts for autocomplete
    path('crm-contacts/',                                views.crm_contacts_view,       name='crm_contacts'),
    # Mark read / unread
    path('message/<int:message_pk>/read/',               views.mark_read_api,           name='mark_read'),
    path('message/<int:message_pk>/unread/',             views.mark_unread_api,         name='mark_unread'),
    # Віднести лист до категорії (запам'ятовується для відправника / домену)
    path('message/<int:message_pk>/category/',          views.set_category_view,       name='set_category'),
    path('message/<int:message_pk>/work-category/',     views.set_work_category_view,  name='set_work_category'),
    path('sort/',                                       views_sort.sort_view,          name='sort'),
    path('category/<int:pk>/',                          views_categories.category_info_view,   name='work_category_info'),
    path('message/<int:message_pk>/customer/',          views_categories.message_customer_view, name='message_customer'),
    path('customers/',                                  views_categories.customers_search_view, name='customers_search'),
    path('message/<int:message_pk>/why/',               views_categories.message_why_view,     name='message_why'),
    path('category/<int:pk>/action/',                   views_categories.category_action_view, name='work_category_action'),
    # Quick settings toggles (e.g. mark read on server)
    path('logo/<str:domain>/',                          views.sender_logo_view,        name='sender_logo'),
    path('settings/toggle/',                             views.settings_toggle_view,    name='settings_toggle'),
    # Email rules
    path('rules/',                                       views.rules_list,              name='rules_list'),
    path('rules/create/',                                views.create_rule_api,         name='create_rule'),
    path('rules/<int:rule_pk>/delete/',                  views.delete_rule,             name='delete_rule'),
    path('message/<int:message_pk>/create-rule/',        views.create_rule_from_message, name='create_rule_from_message'),
    # Create calendar task from email
    path('message/<int:message_pk>/create-task/',        views.create_task_from_email,  name='create_task_from_email'),
    # Bulk actions
    path('messages/bulk/',                               views.bulk_action_view,        name='bulk_action'),
    # Translate arbitrary text (reply draft)
    path('translate-text/',                              views.translate_text_view,     name='translate_text'),
]
