"""bots/oauth.py — захист 3-legged OAuth DigiKey Marketplace («🔑 Авторизувати») від підробленого повернення.

Початок (DigiKeyConfigAdmin.oauth_start_view) — лише людина, яка може змінювати налаштування DigiKey (can_authorize):
випадковий state у сесії на STATE_TTL секунд іде на DigiKey разом із запитом авторизації; DigiKey повертає його
в /bots/digikey/oauth-callback/?code=…&state=… (OAuth 2.0, документація DigiKey «3 Legged Authorization»).
Повернення (bots.views.digikey_oauth_callback) обмінює code на токени лише для тієї самої людини в тому самому
браузері: state з сесії, порівняння hmac.compare_digest, одноразово (check_state його прибирає). Інакше — сторінка
з поясненням, токени Marketplace не змінюються.
"""
import hmac
import secrets
import time

SESSION_KEY = "digikey_oauth_state"
STATE_TTL = 15 * 60          # секунд: вхід на DigiKey і підтвердження доступу

REASONS = {
    "missing": "Minerva не починала авторизацію DigiKey у цьому браузері (або її вже завершено).",
    "expired": "Минуло понад 15 хвилин від натискання «🔑 Авторизувати».",
    "mismatch": "Відповідь DigiKey не належить до авторизації, яку почали в цьому браузері.",
}


def can_authorize(user) -> bool:
    """Активний співробітник, який може змінювати налаштування DigiKey (суперкористувач, адміністратор Minerva або
    операція «змінювати» в модулі bots — has_perm іде за ролями Minerva, core/permissions.py)."""
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active and user.is_staff
                and user.has_perm("bots.change_digikeyconfig"))


def new_state(session) -> str:
    """Новий state для запиту авторизації; попередній (якщо був) більше не діє."""
    state = secrets.token_urlsafe(32)
    session[SESSION_KEY] = {"value": state, "expires": time.time() + STATE_TTL}
    return state


def check_state(session, given) -> str:
    """'' — state збігається з тим, що видано в цій сесії (і вже використаний); інакше ключ REASONS."""
    saved = session.pop(SESSION_KEY, None)
    if not isinstance(saved, dict) or not saved.get("value"):
        return "missing"
    try:
        expires = float(saved.get("expires") or 0)
    except (TypeError, ValueError):
        expires = 0
    if time.time() > expires:
        return "expired"
    if not given or not hmac.compare_digest(str(saved["value"]).encode("utf-8"), str(given).encode("utf-8")):
        return "mismatch"
    return ""


def forget(session):
    session.pop(SESSION_KEY, None)
