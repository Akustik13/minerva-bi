import json
import os
import re

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static
from tabele import views as root_views
import tabele.admin  # noqa: F401 — застосовує кастомний порядок сайдбару

_THEME_PROFILES_DIR = os.path.join(settings.BASE_DIR, 'theme_profiles')


def home_page(request):
    return render(request, 'dashboard/home.html')

def integrations_view(request):
    from config.admin import integrations_hub_view
    return admin.site.admin_view(integrations_hub_view)(request)


@login_required
def theme_profiles_list(request):
    profiles = []
    if os.path.isdir(_THEME_PROFILES_DIR):
        profiles = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(_THEME_PROFILES_DIR)
            if f.endswith('.json')
        )
    return JsonResponse({'profiles': profiles})


@login_required
def theme_profiles_load(request, name):
    if not re.match(r'^[\w-]+$', name):
        return JsonResponse({'error': 'Invalid name'}, status=400)
    path_ = os.path.join(_THEME_PROFILES_DIR, name + '.json')
    if not os.path.isfile(path_):
        return JsonResponse({'error': 'Not found'}, status=404)
    with open(path_, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return JsonResponse(data)


urlpatterns = [
    path("", root_views.landing_view, name="landing"),
    path("contact/", root_views.contact_view, name="contact"),
    path("theme-profiles/", theme_profiles_list, name="theme_profiles_list"),
    path("theme-profiles/<str:name>/", theme_profiles_load, name="theme_profiles_load"),
    path("register/",         root_views.register_view,         name="register"),
    path("register/pending/", root_views.register_pending_view, name="register_pending"),
    path("verify/<str:token>/", root_views.verify_email_view,   name="verify_email"),
    path("verify/success/",   root_views.verify_success_view,   name="verify_success"),
    path("admin/config/integrations/", integrations_view, name="integrations_hub"),
    # ── Password reset (must be BEFORE admin/ to avoid 404 from AdminSite) ──
    path(
        "admin/password_reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.html",
            subject_template_name="registration/password_reset_subject.txt",
            success_url="/admin/password_reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "admin/password_reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "admin/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url="/admin/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "admin/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("admin/", admin.site.urls),
    path("onboarding/", include("config.urls")),
    path("strategy/", include("strategy.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("labels/", include("labels_app.urls")),
    path("accounting/", include("accounting.urls")),
    path("bots/", include("bots.urls")),
    path("api/v1/", include("api.urls")),
    path("api-auth/", include("rest_framework.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = lambda request, exception: render(request, "404.html", status=404)
handler500 = lambda request: render(request, "500.html", status=500)