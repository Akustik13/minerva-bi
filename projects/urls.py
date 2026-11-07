from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("api/thread/<int:thread_pk>/", views.thread_view, name="thread"),
    path("api/thread/<int:thread_pk>/link/", views.link_view, name="link"),
    path("api/thread/<int:thread_pk>/create/", views.create_view, name="create"),
    path("api/link/<int:link_pk>/decide/", views.decide_view, name="decide"),
    path("api/link/<int:link_pk>/remove/", views.remove_view, name="remove"),
    path("api/project/<int:pk>/overview/", views.overview_view, name="overview"),
    path("api/project/<int:pk>/rename/", views.rename_view, name="rename"),
    path("api/project/<int:pk>/overview/next-action/", views.overview_next_action_view, name="overview_next_action"),
    # 🧭 Пульт проєкту (projects/cockpit.py)
    path("api/project/<int:pk>/cockpit/", views.cockpit_view, name="cockpit"),
    path("api/project/<int:pk>/highlights/", views.highlight_view, name="highlight"),
    path("api/highlight/<int:hid>/delete/", views.highlight_delete_view, name="highlight_delete"),
    path("api/project/<int:pk>/links/<int:link_pk>/decide/", views.link_decide_view, name="link_decide"),
    path("api/project/<int:pk>/scan/", views.scan_view, name="scan"),
    # 📅 План (projects/plan.py)
    path("api/project/<int:pk>/plan/template/", views.plan_template_view, name="plan_template"),
    path("api/project/<int:pk>/plan/step/", views.plan_step_view, name="plan_step"),
    path("api/project/<int:pk>/plan/step/<int:sid>/delete/", views.plan_step_delete_view, name="plan_step_delete"),
    path("api/project/<int:pk>/plan/phase/", views.plan_phase_view, name="plan_phase"),
    path("api/project/<int:pk>/plan/phase/<int:fid>/delete/", views.plan_phase_delete_view, name="plan_phase_delete"),
    path("api/project/<int:pk>/plan/baseline/", views.plan_baseline_view, name="plan_baseline"),
    path("api/project/<int:pk>/plan/save-template/", views.plan_save_template_view, name="plan_save_template"),
    path("api/project/<int:pk>/plan/clear/", views.plan_clear_view, name="plan_clear"),
    # ⚠️ Ризики й 📝 тижневий статус (projects/risks.py, health.py)
    path("api/project/<int:pk>/risk/", views.risk_view, name="risk"),
    path("api/project/<int:pk>/risk/<int:rid>/delete/", views.risk_delete_view, name="risk_delete"),
    path("api/project/<int:pk>/status/", views.status_view, name="status"),
    path("api/project/<int:pk>/status/draft/", views.status_draft_view, name="status_draft"),
    # 🧠 AI-помічник проєкту (projects/ai_scan.py, ai_plan.py, forgotten.py, delegate.py)
    path("api/project/<int:pk>/ai/scan/", views.ai_scan_view, name="ai_scan"),
    path("api/project/<int:pk>/ai/suggestion/<int:sid>/", views.ai_suggestion_view, name="ai_suggestion"),
    path("api/project/<int:pk>/ai/plan/", views.ai_plan_view, name="ai_plan"),
    path("api/project/<int:pk>/ai/plan/apply/", views.ai_plan_apply_view, name="ai_plan_apply"),
    path("api/project/<int:pk>/forgotten/", views.forgotten_view, name="forgotten"),
    path("api/project/<int:pk>/forgotten/decide/", views.forgotten_decide_view, name="forgotten_decide"),
    path("api/project/<int:pk>/forgotten/<uuid:job_id>/", views.forgotten_poll_view, name="forgotten_poll"),
    path("api/project/<int:pk>/plan/step/<int:sid>/agent/", views.step_agent_view, name="step_agent"),
]
