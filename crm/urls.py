"""crm/urls.py — AI і хронологія клієнта."""
from django.urls import path
from . import views

urlpatterns = [
    path('customer/<int:customer_pk>/ai-analysis/',
         views.ai_customer_analysis,
         name='crm_ai_analysis'),
    path('customer/<int:customer_pk>/ai-email/',
         views.ai_compose_email_for_customer,
         name='crm_ai_email'),
    path('customer/<int:customer_pk>/timeline/',
         views.customer_timeline_json,
         name='crm_timeline_json'),
]
