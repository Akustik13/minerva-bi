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
    path('customer/<int:customer_pk>/ai-strategy/',
         views.ai_suggest_strategy,
         name='crm_ai_strategy'),
    path('customer/<int:customer_pk>/ai-strategy/apply/',
         views.ai_apply_strategy,
         name='crm_ai_strategy_apply'),
    path('customer/<int:customer_pk>/send-email/',
         views.send_customer_email,
         name='crm_send_email'),
    path('customer/<int:customer_pk>/reset-ai/',
         views.reset_customer_ai_context,
         name='crm_reset_ai'),
]
