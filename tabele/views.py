import secrets

from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods


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

def register_view(request):
    """Registration page for new clients."""
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('/admin/')

    from core.forms import ClientRegistrationForm
    from core.models import TenantAccount, AuditLog
    from config.models import SystemSettings

    if request.method == 'POST':
        form = ClientRegistrationForm(request.POST)
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
                is_staff=True,
                is_active=True,
            )

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

            # Assign admin role
            from core.models import UserProfile
            from core.utils import apply_role_defaults
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = 'admin'
            profile.save()
            apply_role_defaults(profile)

            # Send verification email
            _send_verification_email(user, tenant, token, s)

            # Log registration
            AuditLog.log(
                action='create',
                module='registration',
                model_name='TenantAccount',
                object_id=tenant.pk,
                object_repr=str(tenant),
                extra={
                    'event': 'new_client_registered',
                    'company': data['company_name'],
                    'email': data['email'],
                    'package': data['package'],
                },
            )

            # Notify vendor
            _notify_vendor_new_client(tenant, s)

            return redirect('/register/pending/')
    else:
        initial_package = request.GET.get('package', 'starter')
        form = ClientRegistrationForm(initial={'package': initial_package})

    from config.models import SystemSettings
    s = SystemSettings.get()
    return render(request, 'registration/register.html', {
        'form': form,
        'company_name': s.company_name,
    })


def register_pending_view(request):
    from config.models import SystemSettings
    s = SystemSettings.get()
    return render(request, 'registration/register_pending.html', {
        'company_name': s.company_name,
    })


def verify_email_view(request, token):
    from core.models import TenantAccount, AuditLog
    from config.models import SystemSettings

    try:
        tenant = TenantAccount.objects.get(
            verification_token=token,
            email_verified=False,
        )
    except TenantAccount.DoesNotExist:
        return render(request, 'registration/verify_error.html', {
            'reason': 'Посилання недійсне або вже використане.',
        })

    tenant.email_verified     = True
    tenant.status             = 'trial'
    tenant.activated_at       = timezone.now()
    tenant.verification_token = ''
    tenant.save()

    AuditLog.log(
        action='settings',
        module='registration',
        model_name='TenantAccount',
        object_id=tenant.pk,
        object_repr=str(tenant),
        extra={
            'event': 'email_verified',
            'company': tenant.company_name,
            'package': tenant.package,
            'trial_ends': str(tenant.trial_ends_at),
        },
    )

    if tenant.owner_user:
        login(request, tenant.owner_user,
              backend='core.auth_backend.EmailOrUsernameBackend')

    s = SystemSettings.get()
    _send_welcome_email(tenant, s)

    return redirect('/verify/success/')


def verify_success_view(request):
    from config.models import SystemSettings
    s = SystemSettings.get()
    return render(request, 'registration/verify_success.html', {
        'company_name': s.company_name,
    })


# ── Email helpers ─────────────────────────────────────────────────────────────

def _send_verification_email(user, tenant, token, settings_obj):
    from django.core.mail import send_mail
    verify_url = f'{settings_obj.site_url}/verify/{token}/'
    try:
        send_mail(
            subject=f'{settings_obj.company_name} — Підтвердіть email',
            message=(
                f'Вітаємо, {tenant.owner_name}!\n\n'
                f'Ви зареєструвались в {settings_obj.company_name}.\n\n'
                f'Підтвердіть email перейшовши за посиланням:\n'
                f'{verify_url}\n\n'
                f'Посилання дійсне 48 годин.\n\n'
                f'Якщо ви не реєструвались — проігноруйте цей лист.'
            ),
            from_email=settings_obj.from_email,
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        pass


def _send_welcome_email(tenant, settings_obj):
    from django.core.mail import send_mail
    try:
        trial_str = tenant.trial_ends_at.strftime('%d.%m.%Y') if tenant.trial_ends_at else '—'
        send_mail(
            subject=f'Ласкаво просимо до {settings_obj.company_name}!',
            message=(
                f'Вітаємо, {tenant.owner_name}!\n\n'
                f'Ваш акаунт активовано. Пробний період: до {trial_str}.\n\n'
                f'Пакет: {tenant.get_package_display()}\n\n'
                f'Увійти в систему:\n{settings_obj.site_url}/admin/\n\n'
                f'Email: {tenant.owner_email}\n'
                f'Пароль: той що ви вказали при реєстрації\n\n'
                + (f'Telegram: {settings_obj.company_telegram}'
                   if settings_obj.company_telegram else
                   f'Email: {settings_obj.company_email}')
            ),
            from_email=settings_obj.from_email,
            recipient_list=[tenant.owner_email],
            fail_silently=True,
        )
    except Exception:
        pass


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


def _notify_vendor_new_client(tenant, settings_obj):
    from django.core.mail import send_mail
    vendor_email = settings_obj.company_email
    if not vendor_email:
        return
    try:
        send_mail(
            subject=f'Новий клієнт: {tenant.company_name}',
            message=(
                f'Зареєструвався новий клієнт:\n\n'
                f'Компанія: {tenant.company_name}\n'
                f'Власник:  {tenant.owner_name} <{tenant.owner_email}>\n'
                f'Пакет:    {tenant.get_package_display()}\n'
                f'Країна:   {tenant.company_country}\n\n'
                f'Керувати: {settings_obj.site_url}/admin/core/tenantaccount/'
            ),
            from_email=settings_obj.from_email,
            recipient_list=[vendor_email],
            fail_silently=True,
        )
    except Exception:
        pass
