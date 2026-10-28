from django.urls import path
from . import sync, views, views_cleanup

app_name = 'calendar_app'

urlpatterns = [
    path('',                                views.calendar_view,       name='calendar'),
    path('events/',                         views.events_json,         name='events_json'),
    path('events/bulk/',                    views.events_bulk_api,     name='events_bulk'),
    path('event/<int:pk>/',                 views.event_detail_api,    name='event_detail'),
    path('event/<int:pk>/done/',            views.event_done,          name='event_done'),
    path('event/<int:pk>/toggle/',          views.event_toggle_done,   name='event_toggle'),
    path('event/<int:pk>/set-type/',        views.event_set_type,      name='event_set_type'),
    path('event/<int:pk>/not-needed/',      views_cleanup.event_not_needed, name='event_not_needed'),
    path('blocks/preview/',                 views_cleanup.block_preview, name='block_preview'),
    path('blocks/',                         views_cleanup.block_create, name='block_create'),
    path('cleanup/',                        views_cleanup.cleanup_view, name='cleanup'),
    path('categories/',                     views.category_list_api,   name='category_list'),
    path('categories/create/',              views.category_create_api, name='category_create'),
    path('categories/<int:cat_pk>/delete/', views.category_delete_api, name='category_delete'),
    path('ai-chat/',                         views.calendar_ai_chat,    name='ai_chat'),
    path('pending-push/',                   views.pending_push_view,   name='pending_push'),
    path('settings/',                       views.settings_view,       name='settings'),
    path('invites/<int:pk>/<str:action>/',  views.invite_decide,       name='invite_decide'),
    path('deadlines/',                      views.deadlines_view,      name='deadlines'),
    path('connections/',                    views.connections_view,    name='connections'),
    path('full/',                           views.full_view,           name='full'),
    path('full/events/',                    views.full_events,         name='full_events'),
    path('feed/<str:token>.ics',            views.calendar_feed,       name='feed'),
    path('connections/oauth/<str:provider>/start/',    sync.oauth_start,    name='oauth_start'),
    path('connections/oauth/<str:provider>/callback/', sync.oauth_callback, name='oauth_callback'),
    path('connections/sync/',               sync.sync_action,          name='sync_action'),
]
