"""
bots/views.py — OAuth2 callback + Webhook для DigiKey Marketplace
"""
import hashlib
import hmac
import json
import logging

from django.shortcuts import redirect
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


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


# ── DigiKey Webhook ───────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "POST"])
def digikey_webhook(request):
    """
    /bots/digikey/webhook/

    GET  — верифікаційний challenge від DigiKey при реєстрації:
           ?challenge=<string> → повертаємо той самий рядок.

    POST — подія про нове/змінене замовлення.
           Підпис: заголовок X-DigiKey-Signature (HMAC-SHA256 hex).
           Синхронізація запускається у фоновому потоці — відповідаємо 200 одразу.
    """
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()

    # ── GET verification challenge ────────────────────────────────────────────
    if request.method == "GET":
        challenge = request.GET.get("challenge", "")
        if challenge:
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse("DigiKey webhook endpoint OK", content_type="text/plain")

    # ── POST: validate HMAC signature ─────────────────────────────────────────
    if config.webhook_secret:
        sig_header = request.headers.get("X-DigiKey-Signature", "")
        expected = hmac.new(
            config.webhook_secret.encode("utf-8"),
            request.body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            logger.warning("DigiKey webhook: invalid HMAC signature")
            return HttpResponse("Forbidden", status=403)

    # ── Parse payload ─────────────────────────────────────────────────────────
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse("Bad Request", status=400)

    event_type = (
        payload.get("EventType")
        or payload.get("eventType")
        or "unknown"
    )
    order_id = (
        payload.get("OrderId")
        or payload.get("orderId")
        or payload.get("businessId")
        or ""
    )
    logger.info("DigiKey webhook received: event=%s order=%s", event_type, order_id)

    if not config.webhook_enabled:
        return HttpResponse("OK", status=200)

    # ── Sync у фоновому потоці — не блокуємо DigiKey ─────────────────────────
    import threading

    def _sync():
        try:
            from bots.services.digikey import sync_marketplace_orders
            stats = sync_marketplace_orders(config)
            logger.info(
                "DigiKey webhook sync done: +%d orders, +%d lines",
                stats.get("created", 0),
                stats.get("lines_created", 0),
            )
        except Exception:
            logger.exception("DigiKey webhook sync failed")

    threading.Thread(target=_sync, daemon=True).start()
    return HttpResponse("OK", status=200)
