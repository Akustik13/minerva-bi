import json
import os
import re

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static
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
    path("", home_page, name="home"),
    path("theme-profiles/", theme_profiles_list, name="theme_profiles_list"),
    path("theme-profiles/<str:name>/", theme_profiles_load, name="theme_profiles_load"),
    path("admin/config/integrations/", integrations_view, name="integrations_hub"),
    path("admin/", admin.site.urls),
    path("onboarding/", include("config.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("labels/", include("labels_app.urls")),
    path("accounting/", include("accounting.urls")),
    path("bots/", include("bots.urls")),
    path("api/v1/", include("api.urls")),
    path("api-auth/", include("rest_framework.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = lambda request, exception: render(request, "404.html", status=404)
handler500 = lambda request: render(request, "500.html", status=500)