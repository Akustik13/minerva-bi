"""
bots/views.py — OAuth2 callback для DigiKey Marketplace (3-legged)
"""
from django.shortcuts import redirect
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta


def digikey_oauth_callback(request):
    """
    Обробляє redirect від DigiKey після авторизації користувача.
    URL: /bots/digikey/oauth-callback/
    Параметри: ?code=... або ?error=...
    """
    error = request.GET.get("error")
    code  = request.GET.get("code")

    if error:
        request.session["digikey_oauth_error"] = (
            f"DigiKey повернув помилку: {error} — {request.GET.get('error_description', '')}"
        )
        return redirect("/admin/bots/digikeyconfig/1/change/")

    if not code:
        return HttpResponse("Missing code parameter", status=400)

    from bots.models import DigiKeyConfig
    from bots.services.digikey import exchange_code_for_tokens, DigiKeyAPIError

    config       = DigiKeyConfig.get()
    redirect_uri = request.session.pop("digikey_oauth_redirect_uri", None)

    if not redirect_uri:
        # Fallback: побудувати URI знову
        redirect_uri = request.build_absolute_uri("/bots/digikey/oauth-callback/")

    try:
        data       = exchange_code_for_tokens(config, code, redirect_uri)
        token      = data["access_token"]
        expires_in = int(data.get("expires_in", 600))
        refresh    = data.get("refresh_token", "")

        DigiKeyConfig.objects.filter(pk=1).update(
            marketplace_access_token=token,
            marketplace_refresh_token=refresh,
            marketplace_token_expires_at=timezone.now() + timedelta(seconds=expires_in - 30),
        )
        request.session["digikey_oauth_success"] = True
    except DigiKeyAPIError as e:
        request.session["digikey_oauth_error"] = str(e)
    except Exception as e:
        request.session["digikey_oauth_error"] = f"{type(e).__name__}: {e}"

    return redirect("/admin/bots/digikeyconfig/1/change/")
