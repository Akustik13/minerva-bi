from django.urls import path
from . import views

app_name = 'calendar_app'

urlpatterns = [
    path('',                           views.calendar_view,     name='calendar'),
    path('events/',                    views.events_json,       name='events_json'),
    path('event/<int:pk>/',            views.event_detail_api,  name='event_detail'),
    path('event/<int:pk>/done/',       views.event_done,        name='event_done'),
    path('event/<int:pk>/toggle/',     views.event_toggle_done, name='event_toggle'),
    path('pending-push/',              views.pending_push_view, name='pending_push'),
    path('settings/',                  views.settings_view,     name='settings'),
]
