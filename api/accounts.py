"""
Особистий кабінет покупців магазину — API для сайту (логіка: shop/accounts.py).

Сайт (PHP) — посередник: ключ API лише на сервері сайту, покупець отримує від сайту cookie (HttpOnly)
з ключем сесії; сайт передає його в Minerva заголовком  X-Customer-Session.

  GET  shop/account/?link_de=…&link_en=…  {enabled, password_min_length}; адреси кабінету запам'ятовуються
  POST shop/account/register/          {email, password, company, name, phone?, vat_id?, …, confirm_b2b, lang, link}
  POST shop/account/resend/            {email, link, lang}            — повторний лист підтвердження
  POST shop/account/verify/            {token, password, remember}    → сесія
  POST shop/account/login/             {email, password, remember}    → сесія
  POST shop/account/logout/            (X-Customer-Session)
  POST shop/account/password/forgot/   {email, link, lang}
  POST shop/account/password/reset/    {token, password, remember}    → сесія
  GET|PATCH|DELETE shop/account/me/    профіль (DELETE {password} — видалити акаунт)
  POST shop/account/me/password/       {current_password, new_password}
  GET  shop/account/me/orders/         замовлення й запити покупця
  GET  shop/account/me/orders/<number>/
  GET|PUT shop/account/me/cart/        {items: {sku: qty}}

Права ключа: accounts:read (GET), accounts:write (решта). Магазин має бути з «Особистий кабінет покупців».
Відповіді register / resend / forgot однакові, чи є акаунт; помилки — {detail, code, …} (переклад на сайті).
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from shop import accounts
from shop.accounts import AccountError

from .models import APIKey

SESSION_HEADER = "HTTP_X_CUSTOMER_SESSION"


def _session_payload(customer, raw, session):
    return {"session": raw, "expires_at": session.expires_at, "remember": session.remember,
            "customer": accounts.profile(customer)}


class AccountView(APIView):
    resource_scope = "accounts"
    customer_required = False
    shop = customer = session = None

    def dispatch_account(self, request, handler):
        key = request.auth if isinstance(request.auth, APIKey) else None
        if key is None:
            return Response({"detail": "Лише для ключа API сайту.", "code": "api_key_required"},
                            status=status.HTTP_403_FORBIDDEN)
        from shop.services import shop_for_key
        self.shop = shop_for_key(key)
        if self.shop is None:
            return Response({"detail": "Для ключа не налаштовано магазин.", "code": "no_shop"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not self.shop.accounts_enabled:
            return Response({"detail": "Особистий кабінет у магазині вимкнено.", "code": "accounts_disabled"},
                            status=status.HTTP_403_FORBIDDEN)
        if self.customer_required:
            found = accounts.authenticate(self.shop, request.META.get(SESSION_HEADER, ""))
            if not found:
                return Response({"detail": "Потрібен вхід.", "code": "not_authenticated"},
                                status=status.HTTP_401_UNAUTHORIZED)
            self.customer, self.session = found
        data = request.data if isinstance(request.data, dict) else {}
        try:
            return handler(request, data)
        except AccountError as e:
            body = {"detail": e.message, "code": e.code, **e.extra}
            resp = Response(body, status=e.status)
            if e.extra.get("retry_after"):
                resp["Retry-After"] = str(e.extra["retry_after"])
            return resp


def _ok(**extra):
    return Response({"ok": True, **extra})


# ── Без входу ────────────────────────────────────────────────────────────────

class StatusView(AccountView):
    """Чи ввімкнено кабінет у магазині ключа (сайт показує / ховає вхід)."""

    def get(self, request):
        key = request.auth if isinstance(request.auth, APIKey) else None
        if key is None:
            return Response({"detail": "Лише для ключа API сайту.", "code": "api_key_required"},
                            status=status.HTTP_403_FORBIDDEN)
        from shop.services import shop_for_key
        shop = shop_for_key(key)
        if shop is not None and shop.accounts_enabled:  # сайт повідомляє адреси кабінету (для листів з адмінки)
            accounts.remember_links(shop, {"de": request.query_params.get("link_de", ""),
                                           "en": request.query_params.get("link_en", "")})
        return Response({"enabled": bool(shop and shop.accounts_enabled),
                         "password_min_length": accounts.MIN_PASSWORD})


class RegisterView(AccountView):
    def post(self, request):
        def run(req, d):
            accounts.register(self.shop, d, d.get("link"))
            return Response({"ok": True, "status": "verification_sent"}, status=status.HTTP_202_ACCEPTED)
        return self.dispatch_account(request, run)


class ResendView(AccountView):
    def post(self, request):
        def run(req, d):
            accounts.resend_verification(self.shop, d.get("email"), d.get("link"), d.get("lang", ""))
            return Response({"ok": True, "status": "verification_sent"}, status=status.HTTP_202_ACCEPTED)
        return self.dispatch_account(request, run)


class VerifyView(AccountView):
    def post(self, request):
        def run(req, d):
            customer, raw, session = accounts.verify_email(self.shop, d.get("token"), d.get("password"),
                                                           bool(d.get("remember")))
            return Response(_session_payload(customer, raw, session))
        return self.dispatch_account(request, run)


class LoginView(AccountView):
    def post(self, request):
        def run(req, d):
            customer, raw, session = accounts.login(self.shop, d.get("email"), d.get("password"),
                                                    bool(d.get("remember")))
            return Response(_session_payload(customer, raw, session))
        return self.dispatch_account(request, run)


class LogoutView(AccountView):
    def post(self, request):
        def run(req, d):
            found = accounts.authenticate(self.shop, req.META.get(SESSION_HEADER, ""))
            accounts.logout(found[1] if found else None)
            return _ok()
        return self.dispatch_account(request, run)


class ForgotView(AccountView):
    def post(self, request):
        def run(req, d):
            accounts.forgot_password(self.shop, d.get("email"), d.get("link"), d.get("lang", ""))
            return Response({"ok": True, "status": "reset_sent"}, status=status.HTTP_202_ACCEPTED)
        return self.dispatch_account(request, run)


class ResetView(AccountView):
    def post(self, request):
        def run(req, d):
            customer, raw, session = accounts.reset_password(self.shop, d.get("token"), d.get("password"),
                                                             bool(d.get("remember")))
            return Response(_session_payload(customer, raw, session))
        return self.dispatch_account(request, run)


# ── Після входу (X-Customer-Session) ─────────────────────────────────────────

class MeView(AccountView):
    customer_required = True

    def get(self, request):
        return self.dispatch_account(request, lambda req, d: Response(accounts.profile(self.customer)))

    def patch(self, request):
        def run(req, d):
            return Response(accounts.profile(accounts.update_profile(self.customer, d)))
        return self.dispatch_account(request, run)

    def delete(self, request):
        def run(req, d):
            accounts.delete_account(self.customer, d.get("password"))
            return _ok(status="deleted")
        return self.dispatch_account(request, run)


class PasswordView(AccountView):
    customer_required = True

    def post(self, request):
        def run(req, d):
            accounts.change_password(self.customer, self.session, d.get("current_password"), d.get("new_password"))
            return _ok()
        return self.dispatch_account(request, run)


class OrdersView(AccountView):
    customer_required = True

    def get(self, request):
        def run(req, d):
            rows = [accounts.order_summary(i) for i in accounts.orders_qs(self.customer)[:200]]
            return Response({"count": len(rows), "results": rows})
        return self.dispatch_account(request, run)


class OrderDetailView(AccountView):
    customer_required = True

    def get(self, request, number):
        return self.dispatch_account(
            request, lambda req, d: Response(accounts.order_detail(accounts.find_order(self.customer, number))))


class CartView(AccountView):
    customer_required = True

    def get(self, request):
        return self.dispatch_account(request, lambda req, d: Response(accounts.cart_payload(self.customer)))

    def put(self, request):
        return self.dispatch_account(request, lambda req, d: Response(accounts.save_cart(self.customer, d.get("items"))))
