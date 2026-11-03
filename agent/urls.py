from django.urls import path

from agent import views

app_name = 'agent'

urlpatterns = [
    path('mail/<int:pk>/',       views.suggest_view, name='mail_suggest'),
    path('mail/<int:pk>/try/',   views.try_view,     name='mail_try'),
    path('mail/<int:pk>/save/',  views.save_view,    name='mail_save'),
]
