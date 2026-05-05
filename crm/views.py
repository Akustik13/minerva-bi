"""crm/views.py — AI-аналіз клієнта, генерація email, хронологія (AJAX)."""
import json
import re

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


@staff_member_required
def ai_suggest_strategy(request, customer_pk):
    """
    GET: AI генерує персоналізовану стратегію для клієнта.
    Повертає JSON зі структурою стратегії (без збереження в БД).
    """
    from crm.models import Customer, CustomerTimeline

    try:
        customer = Customer.objects.get(pk=customer_pk)
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Клієнт не знайдений'}, status=404)

    profile = getattr(request.user, 'profile', None)

    try:
        from ai_assistant.tools import execute_tool
        customer_data = execute_tool('get_customer_info',
                                     {'customer_name': customer.name}, profile=profile)
        orders_data   = execute_tool('get_recent_orders',
                                     {'customer_name': customer.name, 'limit': 5}, profile=profile)
        emails_data   = execute_tool('get_customer_emails',
                                     {'customer_name': customer.name, 'limit': 10}, profile=profile)
    except Exception:
        customer_data = {}
        orders_data   = {}
        emails_data   = {}

    prompt = (
        f"Проаналізуй клієнта '{customer.name}' і згенеруй персоналізовану CRM-стратегію.\n\n"
        f"Дані клієнта: {json.dumps(customer_data, ensure_ascii=False, default=str)}\n"
        f"Останні замовлення: {json.dumps(orders_data, ensure_ascii=False, default=str)}\n"
        f"Листування: {json.dumps(emails_data, ensure_ascii=False, default=str)}\n\n"
        "Поверни ТІЛЬКИ валідний JSON (без markdown, без ```) у такому форматі:\n"
        '{"strategy_name":"...", "behavior_type":"reactivation|nurturing|retention|onboarding",'
        '"analysis_summary":"УКРАЇНСЬКОЮ 2-4 речення про клієнта та рекомендацію",'
        '"steps":[{"step_type":"email|call|pause|decision","title":"...","description":"...","delay_days":0}]}'
        "\n\nКількість кроків: від 3 до 7. Кроки мають бути конкретними і персоналізованими."
    )

    try:
        from ai_assistant.service import chat, reset_conversation
        reset_conversation(profile, 'crm_strategy')
        raw = chat(prompt, profile=profile, channel='crm_strategy')
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    # Parse JSON: direct → regex fallback → graceful fallback
    strategy = None
    try:
        strategy = json.loads(raw)
    except Exception:
        m = re.search(r'\{[\s\S]+\}', raw)
        if m:
            try:
                strategy = json.loads(m.group(0))
            except Exception:
                pass

    if strategy is None:
        strategy = {
            'strategy_name': f'Стратегія для {customer.name}',
            'behavior_type': 'nurturing',
            'analysis_summary': raw[:500],
            'steps': [],
        }

    try:
        CustomerTimeline.objects.create(
            customer=customer,
            user=request.user,
            event_type='ai_analysis',
            title=f"AI стратегія: {strategy.get('strategy_name', '')}",
            body=strategy.get('analysis_summary', '')[:500],
        )
    except Exception:
        pass

    return JsonResponse({'ok': True, 'strategy': strategy})


@staff_member_required
@require_POST
def ai_apply_strategy(request, customer_pk):
    """
    POST: Зберегти AI-згенеровану стратегію і запустити її для клієнта.
    Body JSON: {strategy_name, behavior_type, steps: [...]}
    """
    from django.utils import timezone
    from crm.models import Customer
    from strategy.models import StrategyTemplate, TemplateStep, CustomerStrategy, CustomerStep

    try:
        customer = Customer.objects.get(pk=customer_pk)
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Клієнт не знайдений'}, status=404)

    data = json.loads(request.body or '{}')

    strategy_name = data.get('strategy_name', f'AI стратегія для {customer.name}')
    behavior_type = data.get('behavior_type', 'nurturing')
    steps         = data.get('steps', [])

    if not steps:
        return JsonResponse(
            {'error': 'AI не повернув кроки стратегії. Спробуйте перегенерувати.'},
            status=400,
        )

    # Validate behavior_type
    valid_bt = [c[0] for c in StrategyTemplate.BehaviorType.choices]
    if behavior_type not in valid_bt:
        behavior_type = 'nurturing'

    # Validate step_type values
    valid_st = [c[0] for c in TemplateStep.StepType.choices]

    tmpl = None
    cs   = None
    try:
        # 1. Створити шаблон
        tmpl = StrategyTemplate.objects.create(
            name=strategy_name,
            behavior_type=behavior_type,
            is_ai_generated=True,
        )

        # 2. Створити кроки шаблону
        for i, step in enumerate(steps):
            st = step.get('step_type', 'email')
            if st not in valid_st:
                st = 'email'
            TemplateStep.objects.create(
                template=tmpl,
                step_type=st,
                title=step.get('title', f'Крок {i + 1}'),
                description=step.get('description', ''),
                delay_days=int(step.get('delay_days', 0)),
                order=i,
            )

        # 3. Створити CustomerStrategy вручну
        now = timezone.now()
        cs = CustomerStrategy.objects.create(
            customer=customer,
            template=tmpl,
            name=strategy_name,
            status=CustomerStrategy.Status.ACTIVE,
            started_at=now,
        )

        # 4. Створити ВСІ CustomerStep одразу (AI стратегія — повний план відомий зразу)
        tmpl_steps = list(tmpl.steps.order_by('order'))
        cumulative_days = 0
        first_cstep = None
        for i, step_data in enumerate(steps):
            cumulative_days += int(step_data.get('delay_days', 0))
            tmpl_step = tmpl_steps[i] if i < len(tmpl_steps) else None
            cstep = CustomerStep.objects.create(
                strategy=cs,
                template_step=tmpl_step,
                step_type=(tmpl_step.step_type if tmpl_step
                           else step_data.get('step_type', 'email')),
                title=(tmpl_step.title if tmpl_step
                       else step_data.get('title', f'Крок {i + 1}')),
                description=(tmpl_step.description if tmpl_step
                             else step_data.get('description', '')),
                scheduled_at=now + timezone.timedelta(days=cumulative_days),
            )
            if first_cstep is None:
                first_cstep = cstep

        # 5. Встановити поточний крок
        if first_cstep:
            cs.current_step = first_cstep
            cs.next_action_at = first_cstep.scheduled_at
            cs.save(update_fields=['current_step', 'next_action_at'])

    except Exception as e:
        # Очистити в правильному порядку: спочатку cs (PROTECT на tmpl), потім tmpl
        if cs is not None:
            cs.delete()
        if tmpl is not None:
            tmpl.delete()
        return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({
        'ok': True,
        'strategy_id': cs.pk,
        'strategy_url': f'/admin/strategy/customerstrategy/{cs.pk}/change/',
    })
