"""crm/views.py — AI-аналіз клієнта, генерація email, хронологія (AJAX)."""
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST


@staff_member_required
def ai_customer_analysis(request, customer_pk):
    """
    GET: AI-аналіз клієнта.  Результат зберігається в CustomerTimeline.
    """
    from crm.models import Customer, CustomerTimeline

    try:
        customer = Customer.objects.get(pk=customer_pk)
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Клієнт не знайдений'}, status=404)

    profile = getattr(request.user, 'profile', None)

    try:
        from ai_assistant.tools import execute_tool
        customer_data = execute_tool(
            'get_customer_info',
            {'customer_name': customer.name},
            profile=profile,
        )
        orders_data = execute_tool(
            'get_recent_orders',
            {'customer_name': customer.name, 'limit': 5},
            profile=profile,
        )
    except Exception:
        customer_data = {}
        orders_data = {}

    prompt = (
        f"Проаналізуй клієнта {customer.name} і дай конкретні рекомендації.\n\n"
        f"Дані клієнта: {json.dumps(customer_data, ensure_ascii=False, default=str)}\n"
        f"Останні замовлення: {json.dumps(orders_data, ensure_ascii=False, default=str)}\n\n"
        "Дай відповідь у форматі:\n"
        "1. Короткий профіль (1-2 речення хто цей клієнт)\n"
        "2. Поточний статус стосунків (активний/відходить/потенційний)\n"
        "3. Рекомендований наступний крок (конкретна дія)\n"
        "4. Найкращий час для контакту\n"
        "5. Тон спілкування (офіційний/дружній/тощо)\n\n"
        "Будь конкретним, без загальних фраз."
    )

    try:
        from ai_assistant.service import chat
        analysis = chat(prompt, profile=profile, channel='crm_analysis')
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    try:
        CustomerTimeline.objects.create(
            customer=customer,
            user=request.user,
            event_type='ai_analysis',
            title='AI аналіз клієнта',
            body=prompt[:500],
            ai_summary=analysis[:1000],
        )
    except Exception:
        pass

    return JsonResponse({
        'ok':       True,
        'analysis': analysis,
        'customer': customer.name,
    })


@staff_member_required
@require_POST
def ai_compose_email_for_customer(request, customer_pk):
    """
    POST: Скласти email для клієнта.
    Body JSON: {"purpose": "follow_up|...", "language": "uk|de|en"}
    """
    from crm.models import Customer

    try:
        customer = Customer.objects.get(pk=customer_pk)
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Клієнт не знайдений'}, status=404)

    data         = json.loads(request.body or '{}')
    purpose      = data.get('purpose', 'follow_up')
    lang         = data.get('language', 'uk')
    extra_prompt = data.get('extra_prompt', '').strip()
    profile      = getattr(request.user, 'profile', None)

    try:
        from ai_assistant.tools import execute_tool
        result = execute_tool(
            'compose_email',
            {
                'customer_name': customer.name,
                'purpose':       purpose,
                'language':      lang,
            },
            profile=profile,
        )
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

    instruction = result.get('instruction')
    if not instruction:
        return JsonResponse({'ok': False, 'error': 'Помилка генерації'})

    if extra_prompt:
        instruction += f'\n\nДодаткові вказівки від менеджера: {extra_prompt}'

    try:
        from ai_assistant.service import chat
        email_text = chat(instruction, profile=profile, channel='crm_email')
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

    customer_email = result.get('customer', {}).get('email', '') or customer.email
    return JsonResponse({
        'ok':             True,
        'email':          email_text,
        'customer_email': customer_email,
    })


@staff_member_required
def customer_timeline_json(request, customer_pk):
    """GET: хронологія клієнта | POST: додати нотатку/нагадування."""
    from crm.models import CustomerTimeline, Customer

    if request.method == 'POST':
        data = json.loads(request.body or '{}')
        try:
            customer = Customer.objects.get(pk=customer_pk)
            remind_at = None
            if data.get('remind_at'):
                from django.utils.dateparse import parse_datetime
                remind_at = parse_datetime(data['remind_at'])

            CustomerTimeline.objects.create(
                customer=customer,
                user=request.user,
                event_type=data.get('event_type', 'note'),
                title=data.get('title', '')[:300],
                body=data.get('body', ''),
                remind_at=remind_at,
            )
            return JsonResponse({'ok': True})
        except Exception as e:
            return JsonResponse({'ok': False, 'error': str(e)})

    # GET
    events = (CustomerTimeline.objects
              .filter(customer_id=customer_pk)
              .select_related('user')
              .order_by('-is_pinned', '-created_at')[:50])

    return JsonResponse({'events': [
        {
            'id':         e.pk,
            'type':       e.event_type,
            'type_label': e.get_event_type_display(),
            'title':      e.title,
            'body':       e.body[:200],
            'ai_summary': e.ai_summary[:300] if e.ai_summary else '',
            'user':       (e.user.get_full_name() or e.user.username
                           if e.user else 'Система'),
            'date':       e.created_at.strftime('%d.%m.%Y %H:%M'),
            'is_pinned':  e.is_pinned,
            'remind_at':  (e.remind_at.strftime('%d.%m.%Y %H:%M')
                           if e.remind_at else None),
        }
        for e in events
    ]})
