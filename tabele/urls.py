from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static
import tabele.admin  # noqa: F401 — застосовує кастомний порядок сайдбару

def home_page(request):
    return render(request, 'dashboard/home.html')

def integrations_view(request):
    from config.admin import integrations_hub_view
    return admin.site.admin_view(integrations_hub_view)(request)


urlpatterns = [
    path("", home_page, name="home"),
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