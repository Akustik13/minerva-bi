from django.shortcuts import redirect

# Шляхи, які НЕ редіректяться на onboarding
EXEMPT_PREFIXES = [
    "/onboarding/",
    "/admin/login/",
    "/admin/logout/",
    "/admin/jsi18n/",
    "/static/",
    "/media/",
    "/favicon.ico",
    "/api/",
    "/api-auth/",
]


class OnboardingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.user.is_staff
            and not any(request.path.startswith(p) for p in EXEMPT_PREFIXES)
        ):
            try:
                from config.models import SystemSettings
                cfg = SystemSettings.get()
                if not cfg.is_onboarding_complete:
                    return redirect("/onboarding/")
            except Exception:
                pass  # БД ще не мігрована або інша помилка — не блокувати
        return self.get_response(request)
