import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


def landing_view(request):
    """
    Головна сторінка — лендінг Minerva.
    Якщо залогований staff — автоматично редіректить в /admin/.
    """
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('/admin/')

    from config.models import SystemSettings
    s = SystemSettings.get()

    return render(request, 'landing/index.html', {
        'company_name':     s.company_name,
        'company_tagline':  s.company_tagline,
        'company_email':    s.company_email,
        'company_phone':    s.company_phone,
        'company_telegram': s.company_telegram,
        'site_url':         s.site_url,
        'contact_success':  request.GET.get('contact') == 'ok',
        'contact_error':    request.GET.get('contact') == 'error',
    })


@require_http_methods(["POST"])
def contact_view(request):
    """Обробка форми контакту з лендінгу."""
    from config.models import SystemSettings
    from django.core.mail import send_mail

    s = SystemSettings.get()
    recipient = s.company_email
    if not recipient:
        return redirect('/?contact=error')

    name    = request.POST.get('name', '').strip()
    email   = request.POST.get('email', '').strip()
    company = request.POST.get('company', '').strip()
    message = request.POST.get('message', '').strip()

    try:
        send_mail(
            subject=(
                f'Minerva — Запит від {name}'
                + (f' ({company})' if company else '')
            ),
            message=(
                f'Від: {name}\n'
                f'Email: {email}\n'
                + (f'Компанія: {company}\n' if company else '')
                + f'\n{message}'
            ),
            from_email=s.from_email,
            recipient_list=[recipient],
            fail_silently=False,
        )
        return redirect('/?contact=ok')
    except Exception:
        return redirect('/?contact=error')


# ── Registration ─────────────────────────────────────────────────────────────
#
# Самореєстрація нових фірм — лише коли settings.PUBLIC_REGISTRATION (env MINERVA_PUBLIC_REGISTRATION=1).
# Minerva не розділяє фірми: усі акаунти в одній базі. Тому реєстрація ніколи сама не відкриває доступ —
# акаунт неактивний, без доступу до адмінки й з роллю «Лише перегляд»; посилання з листа лише підтверджує email,
# а доступ (активний, співробітник, роль) відкриває адміністратор вручну в «Користувачах».

CLOSED_REASON = 'Самореєстрацію закрито. Доступ до Minerva видає адміністратор вашої фірми.'
VERIFY_HOURS = 48                  # посилання з листа «Підтвердіть email» діє стільки годин
REGISTER_PER_HOUR = 3              # реєстрацій з однієї адреси за годину
REGISTER_ROLE = 'readonly'         # роль нового акаунта (до рішення адміністратора)


def registration_open() -> bool:
    return bool(getattr(settings, 'PUBLIC_REGISTRATION', False))


def _closed(request):
    from config.models import SystemSettings
    return render(request, 'registration/register_closed.html', {
        'company_name': SystemSettings.get().company_name,
    }, status=404)


def _register_throttled(ip: str) -> bool:
    """Не більше REGISTER_PER_HOUR реєстрацій з однієї адреси за годину (спільний кеш процесів gunicorn)."""
    from core.shared_cache import cache
    key = f'register-ip:{ip or "unknown"}'
    try:
        if cache.add(key, 1, 3600):
            return False
        return cache.incr(key) > REGISTER_PER_HOUR
    except Exception:
        return False


def register_view(request):
    """Registration page for new clients (лише коли самореєстрацію увімкнено)."""
    if not registration_open():
        return _closed(request)
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('/admin/')

    from core.forms import ClientRegistrationForm
    from core.models import TenantAccount, AuditLog
    from core.utils import client_ip
    from config.models import SystemSettings

    if request.method == 'POST':
        form = ClientRegistrationForm(request.POST)
        ip = client_ip(request) or ''
        if form.is_valid() and _register_throttled(ip):
            form.add_error(None, 'Забагато реєстрацій з вашої адреси. Спробуйте пізніше.')
        if form.is_valid():
            data = form.cleaned_data
            s    = SystemSettings.get()
            token = secrets.token_urlsafe(32)

            # Unique username derived from email prefix
            base = data['email'].split('@')[0].lower()
            username, i = base, 1
            while User.objects.filter(username=username).exists():
                username = f'{base}{i}'
                i += 1

            name_parts = data['owner_name'].split()
            user = User.objects.create_user(
                username=username,
                email=data['email'],
                password=data['password1'],
                first_name=name_parts[0] if name_parts else '',
                last_name=' '.join(name_parts[1:]),
                is_staff=False,
                is_active=False,  # головний замок: неактивний не увійде (is_staff core/signals.py ставить усім);
            )                     # активним його робить лише адміністратор у «Користувачах»

            trial_end = timezone.now().date() + timezone.timedelta(days=30)
            tenant = TenantAccount.objects.create(
                company_name=data['company_name'],
                company_country=data['company_country'],
                contact_phone=data.get('contact_phone', ''),
                owner_email=data['email'],
                owner_name=data['owner_name'],
                owner_user=user,
                package=data['package'],
                status='pending',
                trial_ends_at=trial_end,
                verification_token=token,
                verification_sent_at=timezone.now(),
            )
            tenant.activate_modules()

            # Найменша роль: адміністратор вирішить, що відкрити (раніше — «Адміністратор» з усім доступом)
            from core.models import UserProfile
            from core.utils import apply_role_defaults
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = REGISTER_ROLE
            apply_role_defaults(profile)
            profile.save()

            _send_verification_email(user, tenant, token, s)

            AuditLog.log(
                action='create',
                module='registration',
                model_name='TenantAccount',
                object_id=tenant.pk,
                object_repr=str(tenant),
                request=request,
                extra={
                    'event': 'new_client_registered',
                    'company': data['company_name'],
                    'email': data['email'],
                    'package': data['package'],
                    'ip': ip,
                },
            )

            _notify_vendor(tenant, s, ip=ip)

            return redirect('/register/pending/')
    else:
        initial_package = request.GET.get('package', 'starter')
        form = ClientRegistrationForm(initial={'package': initial_package})

    s = SystemSettings.get()
    return render(request, 'registration/register.html', {
        'form': form,
        'company_name': s.company_name,
    })


def register_pending_view(request):
    if not registration_open():
        return _closed(request)
    from config.models import SystemSettings
    s = SystemSettings.get()
    return render(request, 'registration/register_pending.html', {
        'company_name': s.company_name,
    })


def _verify_error(request, reason, status=400):
    return render(request, 'registration/verify_error.html', {'reason': reason}, status=status)


def verify_email_view(request, token):
    """Посилання з листа: лише підтверджує email. Доступу не відкриває й не входить — це робить адміністратор."""
    if not registration_open():
        return _verify_error(request, CLOSED_REASON, status=404)

    from core.models import TenantAccount, AuditLog
    from config.models import SystemSettings

    tenant = TenantAccount.objects.filter(verification_token=token, email_verified=False).first() if token else None
    if tenant is None:
        return _verify_error(request, 'Посилання недійсне або вже використане.')
    sent = tenant.verification_sent_at
    if sent is None or sent < timezone.now() - timedelta(hours=VERIFY_HOURS):
        return _verify_error(request, 'Посилання застаріло. Зверніться до адміністратора.')

    tenant.email_verified     = True
    tenant.verification_token = ''
    tenant.save(update_fields=['email_verified', 'verification_token'])

    AuditLog.log(
        action='settings',
        module='registration',
        model_name='TenantAccount',
        object_id=tenant.pk,
        object_repr=str(tenant),
        request=request,
        extra={
            'event': 'email_verified',
            'company': tenant.company_name,
            'package': tenant.package,
        },
    )

    from core.utils import client_ip
    _notify_vendor(tenant, SystemSettings.get(), ip=client_ip(request) or '', verified=True)
    return redirect('/verify/success/')


def verify_success_view(request):
    from config.models import SystemSettings
    s = SystemSettings.get()
    return render(request, 'registration/verify_success.html', {
        'company_name': s.company_name,
    })


# ── Email helpers ─────────────────────────────────────────────────────────────

def _one_line(text) -> str:
    """Тема листа з даних форми: без переносів рядка (інакше Django відкидає лист — BadHeaderError)."""
    return ' '.join(str(text or '').split())[:150]


def _send_verification_email(user, tenant, token, settings_obj):
    from django.core.mail import send_mail
    verify_url = f'{settings_obj.site_url}/verify/{token}/'
    try:
        send_mail(
            subject=_one_line(f'{settings_obj.company_name} — Підтвердіть email'),
            message=(
                f'Вітаємо, {tenant.owner_name}!\n\n'
                f'Ви зареєструвались в {settings_obj.company_name}.\n\n'
                f'Підтвердіть email перейшовши за посиланням:\n'
                f'{verify_url}\n\n'
                f'Посилання дійсне {VERIFY_HOURS} годин. Доступ до системи відкриє адміністратор.\n\n'
                f'Якщо ви не реєструвались — проігноруйте цей лист.'
            ),
            from_email=settings_obj.from_email,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.warning('Registration: verification mail to %s failed: %s', user.email, e)


def manifest_view(request):
    """Динамічний manifest.json — підставляє назву компанії з SystemSettings."""
    from config.models import SystemSettings
    s = SystemSettings.get()
    name = s.company_name or "Minerva BI"
    return JsonResponse({
        "name": name,
        "short_name": name[:12],
        "description": s.company_tagline or "Business Intelligence система",
        "start_url": "/admin/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "theme_color": "#0e1018",
        "background_color": "#080a0f",
        "lang": "uk",
        "icons": [
            {"src": "/static/icons/icon-72.png",  "sizes": "72x72",   "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-96.png",  "sizes": "96x96",   "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-128.png", "sizes": "128x128", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
        "shortcuts": [
            {"name": "Dashboard",  "url": "/admin/"},
            {"name": "Замовлення", "url": "/admin/sales/salesorder/"},
        ],
        "categories": ["business", "productivity"],
    }, content_type='application/manifest+json')


def vendor_recipients(settings_obj, ns) -> list:
    """Хто дізнається про реєстрацію: «Контактний email» системних налаштувань + отримувачі системних сповіщень."""
    from dashboard.notifications import configured_recipients
    out = []
    for email in [settings_obj.company_email] + configured_recipients(ns):
        email = (email or '').strip()
        if email and email.lower() not in {e.lower() for e in out}:
            out.append(email)
    return out


def _notify_vendor(tenant, settings_obj, ip='', verified=False):
    """Лист і Telegram адміністратору про нову реєстрацію / підтверджений email. Дані з форми — екрановані
    (Telegram відкидає повідомлення з «<», «&»), тема — одним рядком; збій — у журнал, а не мовчки."""
    from django.core.mail import send_mail
    from config.models import NotificationSettings
    from dashboard.notifications import esc, _send_telegram

    try:
        ns = NotificationSettings.get()
    except Exception as e:
        logger.warning('Registration: notification settings unavailable: %s', e)
        return

    state = ('Email підтверджено — доступ закрито, доки ви його не відкриєте' if verified
             else 'Очікує підтвердження email · доступу немає')
    title = 'Email підтвердив' if verified else 'Новий акаунт через реєстрацію'
    body = (
        f'{title}:\n\n'
        f'Компанія: {tenant.company_name}\n'
        f'Власник:  {tenant.owner_name} <{tenant.owner_email}>\n'
        f'Пакет:    {tenant.get_package_display()}\n'
        f'Країна:   {tenant.company_country}\n'
        f'IP:       {ip or "невідомий"}\n\n'
        f'Статус:   {state}\n\n'
        f'Не знаєте цю фірму — видаліть акаунт:\n'
        f'{settings_obj.site_url}/admin/core/tenantaccount/\n'
        f'{settings_obj.site_url}/admin/auth/user/?is_active__exact=0'
    )

    recipients = vendor_recipients(settings_obj, ns)
    if recipients:
        try:
            send_mail(
                subject=_one_line(f'⚠️ Minerva — {title}: {tenant.company_name} ({tenant.owner_email})'),
                message=body,
                from_email=settings_obj.from_email,
                recipient_list=recipients,
                fail_silently=False,
            )
        except Exception as e:
            logger.warning('Registration: vendor mail failed: %s', e)
    else:
        logger.warning('Registration: no recipient — set «Контактний email» or notification recipients')

    if ns.telegram_enabled and ns.telegram_bot_token and ns.telegram_chat_id:
        tg_text = (
            f'⚠️ <b>{esc(title)}</b>\n'
            f'🏢 {esc(tenant.company_name)}\n'
            f'👤 {esc(tenant.owner_name)} — {esc(tenant.owner_email)}\n'
            f'📦 {esc(tenant.get_package_display())}\n'
            f'🌍 {esc(tenant.company_country)}\n'
            f'🌐 IP: <code>{esc(ip or "невідомий")}</code>\n'
            f'🔒 {esc(state)}'
        )
        try:
            _send_telegram(ns, tg_text)
        except Exception as e:
            logger.warning('Registration: vendor Telegram failed: %s', e)
