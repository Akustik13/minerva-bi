"""🧠 Розбір пошти: групи листів без своєї категорії (правила + AI) → ✓ прийняти / ✕ не групувати / 🙈 не рахувати.
Логіка — sorting.py."""
import datetime

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from email_assistant import sorting


def _date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _ai_ready() -> bool:
    try:
        from strategy.models import AISettings
        return bool(AISettings.get().anthropic_api_key)
    except Exception:  # noqa: BLE001
        return False


def _ids(values):
    return [int(v) for v in values if str(v).isdigit()]


@staff_member_required
def sort_view(request):
    from email_assistant.models import MailCategory, MailSortDecision, MailSortRun
    user = request.user
    if request.method == 'POST':
        p = request.POST
        action = p.get('action')
        if action == 'start':
            today = timezone.localdate()
            date_to = min(_date(p.get('to')) or today, today)
            date_from = _date(p.get('from')) or date_to - datetime.timedelta(days=29)
            if date_from > date_to:
                date_from = date_to
            run = sorting.start(user, date_from, date_to, use_ai=p.get('ai') == '1' and _ai_ready())
            return redirect(f'{request.path}?run={run.pk}')
        run = get_object_or_404(MailSortRun, pk=p.get('run') or 0, user=user)
        key = p.get('key', '')
        back = f'{request.path}?run={run.pk}'
        try:
            if action == 'accept':
                existing = MailCategory.objects.filter(pk=p.get('existing') or 0).first()
                cat, changed = sorting.accept(user, run, key, (p.get('name') or '').strip(),
                                              p.getlist('sender'), p.getlist('word'), _ids(p.getlist('mail')), existing)
                messages.success(request, f'✓ {cat.label}: правила збережено, листів у категорії: {changed}. '
                                          'Нова пошта сюди піде сама.')
            elif action == 'reject':
                sorting.reject(user, run, key)
                messages.info(request, '✕ Більше не пропонуватиму цю групу.')
            elif action == 'ignore':
                sorting.ignore_time(user, run, key, _ids(p.getlist('mail')))
                messages.info(request, '🙈 Час на ці листи не рахується (✉️ Час на пошту → «проігноровані»).')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(back)

    run = None
    if str(request.GET.get('run', '')).isdigit():
        run = MailSortRun.objects.filter(pk=int(request.GET['run']), user=user).first()
    run = run or MailSortRun.objects.filter(user=user).first()
    today = timezone.localdate()
    days = request.GET.get('days')
    date_to = run.date_to if run and not days else today
    date_from = run.date_from if run and not days else today - datetime.timedelta(days=int(days or 30) - 1)
    from email_assistant.models import EmailMessage
    pending = EmailMessage.objects.filter(account__user=user, is_spam=False, is_deleted=False, work_category=None).count()
    return render(request, 'email_assistant/sort.html', {
        'title': '🧠 Розбір пошти', 'run': run, 'groups': run.groups if run else [],
        'date_from': date_from, 'date_to': date_to, 'ai_ready': _ai_ready(), 'pending': pending,
        'categories': list(MailCategory.objects.filter(is_active=True)),
        'decided': MailSortDecision.objects.filter(user=user).count(),
        'cats_url': '/admin/email_assistant/mailcategory/',
        'is_nav_sidebar_enabled': True,
    })
