from django.urls import path
from . import views

app_name = 'calendar_app'

urlpatterns = [
    path('',              views.calendar_view, name='calendar'),
    path('events/',       views.events_json,   name='events_json'),
    path('event/<int:pk>/done/', views.event_done, name='event_done'),
]
