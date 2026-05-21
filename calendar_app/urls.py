from django.urls import path
from . import views

app_name = 'calendar_app'

urlpatterns = [
    path('',                                views.calendar_view,       name='calendar'),
    path('events/',                         views.events_json,         name='events_json'),
    path('events/bulk/',                    views.events_bulk_api,     name='events_bulk'),
    path('event/<int:pk>/',                 views.event_detail_api,    name='event_detail'),
    path('event/<int:pk>/done/',            views.event_done,          name='event_done'),
    path('event/<int:pk>/toggle/',          views.event_toggle_done,   name='event_toggle'),
    path('event/<int:pk>/set-type/',        views.event_set_type,      name='event_set_type'),
    path('categories/',                     views.category_list_api,   name='category_list'),
    path('categories/create/',              views.category_create_api, name='category_create'),
    path('categories/<int:cat_pk>/delete/', views.category_delete_api, name='category_delete'),
    path('ai-chat/',                         views.calendar_ai_chat,    name='ai_chat'),
    path('pending-push/',                   views.pending_push_view,   name='pending_push'),
    path('settings/',                       views.settings_view,       name='settings'),
]
