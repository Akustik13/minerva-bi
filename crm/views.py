"""crm/views.py — AI-аналіз клієнта, генерація email, хронологія (AJAX)."""
import json
import logging
import re

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

logger = logging.getLogger('crm')


def _parse_strategy_response(text: str) -> dict | None:
    """
    Надійний парсинг відповіді AI.
    Підтримує: чистий JSON, ```json ... ```, будь-який вкладений { ... }.
    """
    if not text:
        return None

    # Варіант 1: весь текст є JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Варіант 2: JSON в ```json ... ``` або ``` ... ``` блоці
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Варіант 3: перший { ... } блок у тексті
    m = re.search(r'\{[\s\S]+\}', text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _gather_email_context(customer) -> list:
    """Pull recent EmailMessage records for this customer directly from email_assistant."""
    try:
        from email_assistant.models import EmailMessage
        from django.db.models import Q
        if not getattr(customer, 'email', ''):
            return []
        qs = (EmailMessage.objects
              .filter(Q(from_email__iexact=customer.email) | Q(to_emails__icontains=customer.email))
              .exclude(folder='draft')
              .order_by('-sent_at')[:10])
        return [
            {
                'direction': 'in' if m.folder == 'inbox' else 'out',
                'subject':   m.subject,
                'date':      m.sent_at.strftime('%d.%m.%Y') if m.sent_at else '?',
                'snippet':   (m.body_text or '')[:200],
            }
            for m in qs
        ]
    except Exception:
        return []


def _gather_strategy_context(customer) -> list:
    """Pull active/recent CustomerStrategy steps for this customer."""
    try:
        from strategy.models import CustomerStrategy
        strategies = (CustomerStrategy.objects
                      .filter(customer=customer)
                      .select_related('current_step', 'template')
                      .order_by('-started_at')[:3])
        result = []
        for s in strategies:
            result.append({
                'name':         s.name,
                'status':       s.status,
                'behavior':     s.template.behavior_type if s.template else '',
                'current_step': s.current_step.title if s.current_step else None,
                'started_at':   s.started_at.strftime('%d.%m.%Y') if s.started_at else '?',
            })
        return result
    except Exception:
        return []


def _fetch_customer_shipments(customer) -> list:
    """Відправлення клієнта через його замовлення (прямий запит, без tools)."""
    try:
        from django.db.models import Q
        from shipping.models import Shipment

        q = Q(order__client__icontains=customer.name)
        if getattr(customer, 'email', ''):
            q |= Q(order__email__iexact=customer.email)

        qs = (Shipment.objects
              .filter(q, order__isnull=False)
              .select_related('carrier', 'order')
              .order_by('-created_at')[:10])

        result = []
        for s in qs:
            item = {
                'order_number':    getattr(s.order, 'order_number', str(s.order_id)),
                'carrier':         str(s.carrier),
                'status':          s.get_status_display(),
                'tracking_number': s.tracking_number or '—',
                'delayed':         s.carrier_delayed,
                'created_at':      s.created_at.strftime('%d.%m.%Y'),
            }
            if s.carrier_status_label:
                item['carrier_status'] = s.carrier_status_label
            if s.eta_from or s.eta_to:
                item['eta'] = f"{s.eta_from} – {s.eta_to}"
            if s.delivered_at:
                item['delivered_at'] = s.delivered_at.strftime('%d.%m.%Y')
            if s.error_message:
                item['error'] = s.error_message[:100]
            result.append(item)
        return result
    except Exception:
        return []


@staff_member_required
def ai_customer_analysis(request, customer_pk):
    """
    GET: AI-аналіз клієнта. Scope задається через ?include=customer,orders,emails,shipments
    За замовчуванням аналізуються всі 4 блоки. Результат зберігається в CustomerTimeline.
    """
    from crm.models import Customer, CustomerTimeline

    try:
        customer = Customer.objects.get(pk=customer_pk)
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Клієнт не знайдений'}, status=404)

    # Scope: які блоки включати в аналіз
    include_raw = request.GET.get('include', 'customer,orders,emails,shipments')
    include = {x.strip() for x in include_raw.split(',') if x.strip()}
    extra_prompt = request.GET.get('extra_prompt', '').strip()

    profile = getattr(request.user, 'profile', None)

    customer_data = {}
    orders_data   = {}
    emails_data   = {}
    shipments_data = []

    try:
        from ai_assistant.tools import execute_tool
        if 'customer' in include:
            customer_data = execute_tool(
                'get_customer_info', {'customer_name': customer.name}, profile=profile)
        if 'orders' in include:
            orders_data = execute_tool(
                'get_recent_orders', {'customer_name': customer.name, 'limit': 5}, profile=profile)
        if 'emails' in include:
            emails_data = execute_tool(
                'get_customer_emails', {'customer_name': customer.name, 'limit': 10}, profile=profile)
    except Exception:
        pass

    if 'shipments' in include:
        shipments_data = _fetch_customer_shipments(customer)

    # Direct EmailMessage context (always included when 'emails' in scope)
    email_messages_data = []
    if 'emails' in include:
        email_messages_data = _gather_email_context(customer)

    # Strategy context
    strategy_data = _gather_strategy_context(customer)

    # Будуємо промпт динамічно — тільки з наявних блоків
    parts = [f"Проаналізуй клієнта {customer.name} і дай конкретні рекомендації.\n"]
    if customer_data:
        parts.append(f"Дані клієнта: {json.dumps(customer_data, ensure_ascii=False, default=str)}")
    if orders_data:
        parts.append(f"Останні замовлення: {json.dumps(orders_data, ensure_ascii=False, default=str)}")
    if emails_data:
        parts.append(f"Листування (CRM tool): {json.dumps(emails_data, ensure_ascii=False, default=str)}")
    if email_messages_data:
        parts.append(f"Email переписка (Email асистент): {json.dumps(email_messages_data, ensure_ascii=False, default=str)}")
    if strategy_data:
        parts.append(f"Активні CRM стратегії: {json.dumps(strategy_data, ensure_ascii=False, default=str)}")
    if shipments_data:
        parts.append(f"Відправлення та доставки: {json.dumps(shipments_data, ensure_ascii=False, default=str)}")

    format_items = [
        "1. Короткий профіль (1-2 речення хто цей клієнт)",
        "2. Поточний статус стосунків (активний/відходить/потенційний)",
        "3. Рекомендований наступний крок (конкретна дія)",
        "4. Найкращий час для контакту",
        "5. Тон спілкування (офіційний/дружній/тощо)",
    ]
    if shipments_data:
        format_items.append(
            "6. Стан доставок — чи є незавершені відправлення, затримки, проблеми"
        )
    parts.append("Дай відповідь у форматі:\n" + "\n".join(format_items))
    parts.append("Будь конкретним, без загальних фраз.")
    if extra_prompt:
        parts.append(f"Додаткове завдання від менеджера: {extra_prompt}")

    prompt = "\n\n".join(parts)

    try:
        from strategy.models import AISettings
        use_web_search = AISettings.get().enable_web_search
    except Exception:
        use_web_search = False

    if use_web_search:
        prompt += (
            "\n\nДОДАТКОВО: використай web_search щоб знайти актуальну інформацію "
            "про компанію клієнта — сфера бізнесу, розмір, останні новини. "
            "Це допоможе зробити рекомендацію точнішою."
        )

    try:
        from ai_assistant.service import chat
        analysis = chat(prompt, profile=profile, channel='crm_analysis',
                        telegram_chat_id=f'crm_customer_{customer_pk}',
                        enable_web_search=use_web_search)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    try:
        CustomerTimeline.objects.create(
            customer=customer,
            user=request.user,
            event_type='ai_analysis',
            title='AI аналіз клієнта',
            body=prompt[:500],
            ai_summary=analysis,
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

    data             = json.loads(request.body or '{}')
    purpose          = data.get('purpose', 'follow_up')
    lang             = data.get('language', 'uk')
    extra_prompt     = data.get('extra_prompt', '').strip()
    strategy_context = data.get('strategy_context', '').strip()
    step_title       = data.get('step_title', '').strip()
    profile          = getattr(request.user, 'profile', None)

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

    if step_title:
        instruction += f'\n\nЦей лист є кроком CRM стратегії: "{step_title}".'
    if strategy_context:
        instruction += f'\nКонтекст стратегії: {strategy_context}'
    if extra_prompt:
        instruction += f'\n\nДодаткові вказівки від менеджера: {extra_prompt}'

    try:
        from ai_assistant.service import chat
        email_text = chat(instruction, profile=profile, channel='crm_email',
                          telegram_chat_id=f'crm_customer_{customer_pk}')
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

    customer_email = result.get('customer', {}).get('email', '') or customer.email
    return JsonResponse({
        'ok':             True,
        'email':          email_text,
        'customer_email': customer_email,
    })


def _imap_save_sent(raw_bytes, imap_host, imap_port, imap_use_ssl,
                    imap_user, imap_password, imap_sent_folder):
    """Append a copy of the sent message to the IMAP Sent folder (best-effort)."""
    import imaplib
    folder = (imap_sent_folder or 'INBOX.Sent').strip().strip('"')
    # IMAP requires double-quotes around folder names that contain spaces or non-ASCII
    quoted_folder = f'"{folder}"'
    try:
        cls = imaplib.IMAP4_SSL if imap_use_ssl else imaplib.IMAP4
        M = cls(imap_host, int(imap_port))
        M.login(imap_user, imap_password)
        M.append(quoted_folder, r'\Seen', None, raw_bytes)
        M.logout()
        logger.info("IMAP: saved sent copy to %s@%s/%s", imap_user, imap_host, folder)
    except Exception as e:
        logger.warning("IMAP save-sent failed (%s@%s): %s", imap_user, imap_host, e)


@staff_member_required
@require_POST
def send_customer_email(request, customer_pk):
    """POST: Надіслати email клієнту безпосередньо з CRM-картки."""
    import html as _html
    from django.core.mail import EmailMultiAlternatives, get_connection
    from django.shortcuts import get_object_or_404
    from config.models import NotificationSettings
    from crm.models import Customer, CustomerTimeline

    customer = get_object_or_404(Customer, pk=customer_pk)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    to_raw    = (data.get('to') or '').strip()
    cc_raw    = (data.get('cc') or '').strip()
    subject   = (data.get('subject') or '').strip()
    body_html = (data.get('body') or '').strip()        # HTML from contenteditable
    body_text = (data.get('body_text') or '').strip()   # plain text from innerText
    if not body_text:
        body_text = re.sub(r'<[^>]+>', '', body_html).strip()

    if not to_raw or not body_text:
        return JsonResponse({'error': 'Вкажіть отримувача та текст листа'}, status=400)

    to_list = [e.strip() for e in to_raw.split(',') if e.strip()]
    cc_list = [e.strip() for e in cc_raw.split(',') if e.strip()]

    ns = NotificationSettings.get()

    # Вибір SMTP: особистий профіль юзера → глобальний (NotificationSettings)
    profile = getattr(request.user, 'profile', None)
    if profile and profile.smtp_host and profile.smtp_user:
        smtp_params = dict(
            host=profile.smtp_host,
            port=profile.smtp_port,
            username=profile.smtp_user,
            password=profile.smtp_password,
            use_tls=profile.smtp_use_tls,
            use_ssl=profile.smtp_use_ssl,
        )
        from_email = (profile.smtp_from or profile.smtp_user).strip()
        smtp_source = 'personal'
    elif ns.email_enabled and ns.email_host_user:
        smtp_params = dict(
            host=ns.email_host,
            port=ns.email_port,
            username=ns.email_host_user,
            password=ns.email_host_password,
            use_tls=ns.email_use_tls,
            use_ssl=ns.email_use_ssl,
        )
        from_email = (ns.email_from or ns.email_host_user).strip()
        smtp_source = 'global'
    else:
        return JsonResponse(
            {'error': 'SMTP не налаштований. Заповніть особистий SMTP у профілі '
                      'або увімкніть глобальний (Config → Notifications).'},
            status=400,
        )

    user_name = request.user.get_full_name() or request.user.username

    # Підпис: особистий HTML (профіль юзера) → глобальний plain-text (NotificationSettings)
    personal_sig = getattr(profile, 'smtp_signature', '') or ''
    if personal_sig:
        sig_html = personal_sig.replace('{name}', _html.escape(user_name))
        sig_text = re.sub(r'<[^>]+>', '', sig_html).strip()
    else:
        sig_text = (ns.email_signature_template or 'З повагою,\n{name}').replace('{name}', user_name)
        sig_html = (
            '<div style="font-family:Arial,sans-serif;font-size:13px;'
            'line-height:1.6;white-space:pre-wrap">'
            + _html.escape(sig_text) + '</div>'
        )

    # Plain-text версія (для поштових клієнтів без HTML)
    full_body = body_text + '\n\n' + sig_text

    # HTML версія: тіло листа (вже HTML з редактора) + розділювач + HTML-підпис
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6">'
        + body_html
        + '</div>'
        '<hr style="border:none;border-top:1px solid #ccc;margin:16px 0">'
        + sig_html
    )

    try:
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            **smtp_params,
            fail_silently=False,
        )
        msg = EmailMultiAlternatives(
            subject=subject or f'Лист клієнту {customer.name}',
            body=full_body,
            from_email=from_email,
            to=to_list,
            cc=cc_list or None,
            connection=connection,
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send()
        # Save copy to IMAP Sent folder (best-effort, never blocks the response)
        raw_bytes = msg.message().as_bytes()
        if profile and getattr(profile, 'imap_enabled', False) and profile.imap_host and profile.imap_user:
            _imap_save_sent(
                raw_bytes,
                profile.imap_host, profile.imap_port, profile.imap_use_ssl,
                profile.imap_user, profile.imap_password,
                getattr(profile, 'imap_sent_folder', 'INBOX.Sent'),
            )
        elif ns.imap_enabled and ns.imap_host and ns.imap_user:
            _imap_save_sent(
                raw_bytes,
                ns.imap_host, ns.imap_port, ns.imap_use_ssl,
                ns.imap_user, ns.imap_password,
                getattr(ns, 'imap_sent_folder', 'INBOX.Sent'),
            )
    except Exception as e:
        logger.exception("CRM send_customer_email error: %s", e)
        return JsonResponse({'error': f'Помилка відправки: {e}'}, status=500)

    CustomerTimeline.objects.create(
        customer=customer,
        user=request.user,
        event_type='email_out',
        title=subject or f'Лист клієнту {customer.name}',
        body=full_body,
        ai_summary='',
    )
    return JsonResponse({'ok': True, 'smtp': smtp_source})


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
            'body':       e.body,
            'ai_summary': e.ai_summary or '',
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
    extra_prompt = request.GET.get('extra_prompt', '').strip()

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

    shipments_data = _fetch_customer_shipments(customer)

    prompt = (
        f"Проаналізуй клієнта '{customer.name}' і згенеруй персоналізовану CRM-стратегію.\n\n"
        f"Дані клієнта: {json.dumps(customer_data, ensure_ascii=False, default=str)}\n"
        f"Останні замовлення: {json.dumps(orders_data, ensure_ascii=False, default=str)}\n"
        f"Листування: {json.dumps(emails_data, ensure_ascii=False, default=str)}\n"
        + (f"Відправлення та доставки: {json.dumps(shipments_data, ensure_ascii=False, default=str)}\n"
           if shipments_data else "")
        + "\n"
        "ОБОВ'ЯЗКОВО поверни ТІЛЬКИ валідний JSON без жодного тексту до або після:\n"
        '{\n'
        '  "strategy_name": "назва стратегії",\n'
        '  "behavior_type": "reactivation",\n'
        '  "analysis_summary": "УКРАЇНСЬКОЮ 2-4 речення про клієнта та рекомендацію",\n'
        '  "steps": [\n'
        '    {"step_type": "email", "title": "Перший контакт", "description": "що зробити", "delay_days": 0},\n'
        '    {"step_type": "pause", "title": "Чекати відповідь", "description": "пауза 5 днів", "delay_days": 5},\n'
        '    {"step_type": "call", "title": "Дзвінок", "description": "зателефонувати", "delay_days": 3}\n'
        '  ]\n'
        '}\n\n'
        'behavior_type: одне з reactivation / nurturing / retention / onboarding\n'
        'step_type: одне з email / call / pause / decision\n'
        'delay_days: ціле число (0 або більше)\n'
        'Мінімум 3 кроки, максимум 7. Тільки JSON, без пояснень, без markdown.'
        + (f'\n\nДодаткові вказівки від менеджера: {extra_prompt}' if extra_prompt else '')
    )

    try:
        from ai_assistant.service import generate_structured
        raw = generate_structured(prompt, profile=profile)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    if not raw:
        return JsonResponse({'error': 'Бюджет AI вичерпано або немає відповіді.'}, status=400)

    logger.info('AI strategy raw response for %s: %s', customer.name, raw[:500])

    strategy = _parse_strategy_response(raw)

    if strategy is None:
        logger.error('Failed to parse AI strategy response for %s: %s', customer.name, raw[:300])
        return JsonResponse({
            'error': 'AI не повернув валідний JSON. Спробуйте ще раз.',
            'raw': raw[:400],
        }, status=400)

    steps = strategy.get('steps', [])
    if not steps:
        logger.error('No steps in AI strategy for %s. Parsed keys: %s', customer.name, list(strategy.keys()))
        return JsonResponse({
            'error': f'AI не повернув кроки стратегії. Отримані поля: {list(strategy.keys())}',
            'strategy': strategy,
        }, status=400)

    logger.info('Parsed %d steps for %s', len(steps), customer.name)

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


@staff_member_required
@require_POST
def reset_customer_ai_context(request, customer_pk):
    """POST: Clear per-customer AI conversation history for this customer."""
    from ai_assistant.service import reset_conversation
    profile = getattr(request.user, 'profile', None)
    chat_id = f'crm_customer_{customer_pk}'
    for channel in ('crm_analysis', 'crm_email'):
        reset_conversation(profile, channel=channel, telegram_chat_id=chat_id)
    return JsonResponse({'ok': True})
