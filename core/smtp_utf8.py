"""Вхід на поштовий сервер з паролем не лише з латинських літер («§», «ä», «ß», кирилиця).

smtplib (SMTP.login → auth) і imaplib (login) кодують логін і пароль в ASCII й падають «'ascii' codec can't
encode character '\\xa7'» ще до звернення до сервера. Тут — AUTH PLAIN / LOGIN з UTF-8 (RFC 4616), як роблять
Thunderbird і Outlook; пароль лише з латиниці — стандартний вхід, як раніше.

- `smtp_login(conn, user, password)` — для готового smtplib-з'єднання (email_assistant/smtp_client.py);
- `EmailBackend` (`BACKEND`) — Django SMTP-бекенд з таким входом: сповіщення, звіти, пошта магазину, нагадування;
- `imap_login(conn, user, password)` — IMAP (AUTHENTICATE PLAIN у UTF-8).
"""
import base64
import smtplib

from django.core.mail.backends.smtp import EmailBackend as _DjangoSMTPBackend

BACKEND = 'core.smtp_utf8.EmailBackend'
NO_PLAIN = ('Сервер не приймає AUTH PLAIN / LOGIN — пароль не лише з латинських літер так не передати. '
            'Змініть пароль скриньки на латиницю й цифри.')


def is_ascii(*values) -> bool:
    try:
        ''.join(str(v or '') for v in values).encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def _b64(text) -> str:
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


def _utf8_auth(conn, user, password):
    conn.ehlo_or_helo_if_needed()
    if not conn.has_extn('auth'):
        raise smtplib.SMTPNotSupportedError('SMTP AUTH extension not supported by server.')
    methods = conn.esmtp_features['auth'].upper().split()
    if 'PLAIN' in methods:
        code, resp = conn.docmd('AUTH', 'PLAIN ' + _b64(f'\0{user}\0{password}'))
    elif 'LOGIN' in methods:
        code, resp = conn.docmd('AUTH', 'LOGIN ' + _b64(user))
        if code == 334:
            code, resp = conn.docmd(_b64(password))
    else:
        raise smtplib.SMTPException(NO_PLAIN)
    if code in (235, 503):        # 503 — уже автентифіковано
        return code, resp
    raise smtplib.SMTPAuthenticationError(code, resp)


def smtp_login(conn, user, password):
    """Вхід SMTP: ASCII — звичайний conn.login; інакше — AUTH PLAIN / LOGIN у UTF-8."""
    if is_ascii(user, password):
        return conn.login(user, password)
    return _utf8_auth(conn, user, password)


class _UTF8Login:
    def login(self, user, password, *, initial_response_ok=True):
        if is_ascii(user, password):
            return super().login(user, password, initial_response_ok=initial_response_ok)
        return _utf8_auth(self, user, password)


class SMTP(_UTF8Login, smtplib.SMTP):
    pass


class SMTP_SSL(_UTF8Login, smtplib.SMTP_SSL):
    pass


class EmailBackend(_DjangoSMTPBackend):
    """Django SMTP-бекенд, що приймає пароль не лише з латинських літер."""

    @property
    def connection_class(self):
        return SMTP_SSL if self.use_ssl else SMTP


def imap_login(conn, user, password):
    """Вхід IMAP: ASCII — звичайний conn.login; інакше — AUTHENTICATE PLAIN у UTF-8."""
    if is_ascii(user, password):
        return conn.login(user, password)
    return conn.authenticate('PLAIN', lambda _challenge: f'\0{user}\0{password}'.encode('utf-8'))


# ── 🔌 Перевірити з'єднання (без надсилання листа) ────────────────────────────

def check_smtp(host, port, *, use_ssl=False, use_tls=False, user='', password='', timeout=15) -> dict:
    """Підключитись, увімкнути шифрування й увійти — без листа. → {ok, steps: [(ok, text)], hint}."""
    import socket
    import ssl
    steps, hint = [], ''
    host, port = (host or '').strip(), int(port or (465 if use_ssl else 587))
    mode = 'SSL' if use_ssl else ('STARTTLS' if use_tls else 'без шифрування')
    if not host:
        return {'ok': False, 'steps': [(False, 'Не вказано SMTP Host')], 'hint': ''}
    conn = None
    try:
        try:
            conn = (SMTP_SSL if use_ssl else SMTP)(host, port, timeout=timeout)
            steps.append((True, f"З'єднання з {host}:{port} ({'SSL' if use_ssl else 'без SSL'})"))
            if use_tls and not use_ssl:
                conn.starttls()
                steps.append((True, 'Шифрування STARTTLS увімкнено'))
        except ssl.SSLError as e:
            steps.append((False, f"SSL/TLS: {e}"))
            hint = ('Режим не відповідає порту: для 465 — «SSL», для 587 — «TLS (STARTTLS)», не обидва.'
                    if 'WRONG_VERSION_NUMBER' in str(e).upper() else '')
            return {'ok': False, 'steps': steps, 'hint': hint}
        except socket.gaierror:
            steps.append((False, f'Адресу сервера «{host}» не знайдено'))
            return {'ok': False, 'steps': steps, 'hint': 'Перевірте SMTP Host (напр. smtp.ionos.de).'}
        except (socket.timeout, TimeoutError):
            steps.append((False, f'{host}:{port} не відповідає за {timeout} с'))
            return {'ok': False, 'steps': steps, 'hint': 'Перевірте порт (465 — SSL, 587 — STARTTLS) і чи не '
                                                         'блокує його мережа сервера.'}
        except ConnectionRefusedError:
            steps.append((False, f"{host} відхилив з'єднання на порту {port}"))
            return {'ok': False, 'steps': steps, 'hint': 'Перевірте порт: 465 — SSL, 587 — STARTTLS.'}
        conn.ehlo_or_helo_if_needed()
        auth = conn.esmtp_features.get('auth', '').upper().split()
        if not user:
            steps.append((True, 'Вхід не налаштовано (SMTP Login порожній)'))
            return {'ok': True, 'steps': steps, 'hint': ''}
        try:
            smtp_login(conn, user, password)
            steps.append((True, f'Вхід як {user} — успішно'
                                + ('' if is_ascii(user, password) else ' (пароль не лише з латиниці — вхід у UTF-8)')))
        except smtplib.SMTPAuthenticationError as e:
            msg = e.smtp_error.decode('utf-8', 'replace') if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            steps.append((False, f'Вхід як {user}: сервер відмовив ({e.smtp_code} {msg[:150]})'))
            hint = 'Логін або пароль не підходить. Gmail — потрібен App Password.'
            return {'ok': False, 'steps': steps, 'hint': hint}
        except (smtplib.SMTPNotSupportedError, smtplib.SMTPException) as e:
            steps.append((False, f'Вхід як {user}: {e}'))
            return {'ok': False, 'steps': steps,
                    'hint': '' if auth else 'Сервер не пропонує вхід (AUTH) у цьому режимі — спробуйте SSL / STARTTLS.'}
        return {'ok': True, 'steps': steps, 'hint': f'Режим: {mode}. Листи надсилатимуться так само.'}
    except Exception as e:  # noqa: BLE001 — показати, а не впасти
        steps.append((False, f'{type(e).__name__}: {e}'[:300]))
        return {'ok': False, 'steps': steps, 'hint': hint}
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:  # noqa: BLE001
                pass
