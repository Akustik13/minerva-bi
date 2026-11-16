"""🖥 «Пошта Minerva» для Windows — сторінки Minerva для програми (логіка — email_assistant/desktop.py).

  /email/app/              — сторінка в браузері: що це, «⬇ Завантажити», «🔗 Підключити програму на цьому комп'ютері»;
  /email/app/hello/        — без входу: програма перевіряє, що за адресою справді Minerva з поштою;
  /email/app/pair/new/     — POST: одноразовий код і посилання minerva-mail://pair?… (сторінка відкриває програму);
  /email/app/pair/state/   — сторінка опитує, чи програма вже використала код;
  /email/app/pair/?code=   — у вікні програми: «Підключити до акаунта …?» → POST → вхід без пароля → пошта.
"""
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from email_assistant import desktop


def _ip(request):
    from core.utils import client_ip
    return client_ip(request)


@staff_member_required
def desktop_page_view(request):
    from email_assistant.views import _ctx
    return render(request, 'email_assistant/desktop.html', _ctx(request, {
        'title': '🖥 Пошта Minerva для Windows',
        'download_url': desktop.APP_URL, 'release_url': desktop.RELEASE_URL,
        'server_url': request.build_absolute_uri('/').rstrip('/'),
        'in_app': desktop.is_app(request), 'pair_ttl_min': desktop.PAIR_TTL // 60,
    }))


@require_GET
def hello_view(request):
    """Без входу: програма перевіряє адресу («це Minerva з поштою»). Нічого про людей і дані не віддає."""
    return JsonResponse({'minerva': 'mail', 'v': 1, 'pair': True})


@staff_member_required
@require_POST
def pair_new_view(request):
    try:
        data = desktop.new_pairing(request.user, request.build_absolute_uri('/'))
    except desktop.PairError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=e.status)
    return JsonResponse({'ok': True, **data})


@staff_member_required
@require_GET
def pair_state_view(request):
    return JsonResponse(desktop.pair_state(request.user, request.GET.get('key')))


def _name(user) -> str:
    return user.get_full_name() or user.username


def pair_view(request):
    """У вікні програми: GET — «Підключити до акаунта …?», POST — використати код і увійти. Підтвердження потрібне,
    щоб чуже посилання minerva-mail://… не підключило програму до чужого акаунта непомітно."""
    code = (request.POST.get('code') if request.method == 'POST' else request.GET.get('code')) or ''
    ctx = {'code': code, 'in_app': desktop.is_app(request), 'login_url': '/admin/login/?next=/email/'}
    if request.method == 'POST':
        try:
            user = desktop.redeem(code, _ip(request), device=desktop.app_version(request))
        except desktop.PairError as e:
            return render(request, 'email_assistant/desktop_pair.html', {**ctx, 'error': str(e)}, status=e.status)
        user.backend = settings.AUTHENTICATION_BACKENDS[0]
        login(request, user)   # журнал аудиту — сигнал входу (core/signals.py), шлях /email/app/pair/
        desktop.keep_session(request)
        return redirect('/email/')
    if request.method != 'GET':
        return JsonResponse({'ok': False, 'error': 'GET або POST'}, status=405)
    user = desktop.pairing_user(code)
    if user is None:
        return render(request, 'email_assistant/desktop_pair.html', {
            **ctx, 'error': 'Код підключення недійсний, уже використаний або застарів — у браузері на сторінці '
                            '«🖥 Пошта Minerva для Windows» натисніть «🔗 Підключити програму» ще раз.'}, status=410)
    current = request.user if request.user.is_authenticated else None
    return render(request, 'email_assistant/desktop_pair.html', {
        **ctx, 'pair_user': user, 'pair_name': _name(user),
        'current_name': _name(current) if current and current.pk != user.pk else ''})
