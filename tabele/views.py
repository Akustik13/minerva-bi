from django.shortcuts import render, redirect
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
