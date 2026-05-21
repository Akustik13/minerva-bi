"""Calendar AI assistant — tool schemas, handlers, chat loop."""
import json
from django.utils import timezone


# ── Tool schemas for Anthropic API ────────────────────────────────────────────

CALENDAR_AI_TOOLS = [
    {
        "name": "list_events",
        "description": (
            "Список подій з можливістю фільтрації. "
            "Повертає події за вказаний період (прострочені завжди включені якщо не вказано інше)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "Кількість днів вперед (0=сьогодні, 7=тиждень, 30=місяць). Default 7.",
                    "default": 7,
                },
                "include_done": {
                    "type": "boolean",
                    "description": "Включити вже виконані події. Default false.",
                    "default": False,
                },
                "event_type": {
                    "type": "string",
                    "description": "Фільтр: deadline / meeting / reminder / email_follow_up / other. Порожньо = всі.",
                },
                "search": {
                    "type": "string",
                    "description": "Пошук по назві або опису (нечутливо до регістру).",
                },
            },
        },
    },
    {
        "name": "find_stale_deadlines",
        "description": (
            "Знайти прострочені й більше не актуальні дедлайни — події типу deadline або email_follow_up, "
            "дата яких вже минула і вони не виконані. "
            "Використовуй перш ніж пропонувати очищення."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Ключове слово для звуження (наприклад 'DigiKey', 'замовлення', 'відвантаження').",
                },
            },
        },
    },
    {
        "name": "mark_events_done",
        "description": "Позначити вказані події як виконані (не видаляє, просто закриває).",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Список ID подій для позначення виконаними.",
                },
            },
            "required": ["event_ids"],
        },
    },
    {
        "name": "delete_events",
        "description": "Видалити вказані події назавжди. Використовуй тільки після явного підтвердження юзером.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Список ID подій для видалення.",
                },
            },
            "required": ["event_ids"],
        },
    },
    {
        "name": "create_event",
        "description": "Створити нову подію в календарі.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string", "description": "Назва події."},
                "start_at":    {"type": "string", "description": "Дата і час ISO 8601: YYYY-MM-DDTHH:MM:SS"},
                "event_type":  {
                    "type": "string",
                    "enum": ["deadline", "meeting", "reminder", "email_follow_up", "other"],
                    "description": "Тип події. Default: reminder.",
                    "default": "reminder",
                },
                "description": {"type": "string", "description": "Опис або деталі."},
                "remind_before_minutes": {
                    "type": "integer",
                    "description": "За скільки хвилин нагадати. Default: 60.",
                    "default": 60,
                },
            },
            "required": ["title", "start_at"],
        },
    },
    {
        "name": "get_weekly_summary",
        "description": "Короткий огляд: майбутні події на тиждень, кількість прострочених, виконаних сьогодні.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ── Tool handlers ─────────────────────────────────────────────────────────────

def execute_calendar_tool(tool_name: str, tool_input: dict, user) -> dict:
    handlers = {
        'list_events':         _list_events,
        'find_stale_deadlines': _find_stale_deadlines,
        'mark_events_done':    _mark_events_done,
        'delete_events':       _delete_events,
        'create_event':        _create_event,
        'get_weekly_summary':  _get_weekly_summary,
    }
    fn = handlers.get(tool_name)
    if not fn:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return fn(tool_input, user)
    except Exception as e:
        return {"error": str(e)}


def _list_events(inp, user):
    from calendar_app.models import CalendarEvent
    from datetime import timedelta

    now        = timezone.now()
    days_ahead = int(inp.get('days_ahead', 7))
    incl_done  = bool(inp.get('include_done', False))
    ev_type    = inp.get('event_type', '')
    search     = inp.get('search', '')

    qs = CalendarEvent.objects.filter(
        user=user,
        start_at__lte=now + timedelta(days=days_ahead),
    )
    if not incl_done:
        qs = qs.filter(is_done=False)
    if ev_type:
        qs = qs.filter(event_type=ev_type)
    if search:
        from django.db.models import Q
        qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))

    rows = []
    for ev in qs.select_related('crm_customer').order_by('start_at')[:50]:
        rows.append({
            "id":          ev.pk,
            "title":       ev.title,
            "start":       ev.start_at.strftime('%d.%m.%Y %H:%M'),
            "type":        ev.event_type,
            "is_done":     ev.is_done,
            "is_overdue":  ev.start_at < now and not ev.is_done,
            "customer":    str(ev.crm_customer) if ev.crm_customer_id else None,
            "description": ev.description[:100] if ev.description else None,
            "has_email":   bool(ev.email_message_id),
        })
    return {"total": len(rows), "overdue": sum(1 for r in rows if r['is_overdue']), "events": rows}


def _find_stale_deadlines(inp, user):
    from calendar_app.models import CalendarEvent

    now     = timezone.now()
    keyword = inp.get('keyword', '')

    qs = CalendarEvent.objects.filter(
        user=user, is_done=False, start_at__lt=now,
        event_type__in=['deadline', 'email_follow_up'],
    )
    if keyword:
        from django.db.models import Q
        qs = qs.filter(Q(title__icontains=keyword) | Q(description__icontains=keyword))

    rows = []
    for ev in qs.order_by('start_at')[:30]:
        rows.append({
            "id":          ev.pk,
            "title":       ev.title,
            "start":       ev.start_at.strftime('%d.%m.%Y %H:%M'),
            "overdue_days": (now - ev.start_at).days,
            "type":        ev.event_type,
            "has_email":   bool(ev.email_message_id),
            "description": ev.description[:80] if ev.description else None,
        })
    return {
        "stale_count": len(rows),
        "events":      rows,
        "suggestion":  "Рекомендую позначити як виконані (mark_events_done) події що вже не актуальні.",
    }


def _mark_events_done(inp, user):
    from calendar_app.models import CalendarEvent

    pks = [int(x) for x in inp.get('event_ids', []) if str(x).isdigit()]
    if not pks:
        return {"error": "event_ids is empty"}
    n = CalendarEvent.objects.filter(pk__in=pks, user=user).update(is_done=True)
    return {"marked_done": n, "success": True,
            "message": f"Позначено виконаними: {n} подій"}


def _delete_events(inp, user):
    from calendar_app.models import CalendarEvent

    pks = [int(x) for x in inp.get('event_ids', []) if str(x).isdigit()]
    if not pks:
        return {"error": "event_ids is empty"}
    n, _ = CalendarEvent.objects.filter(pk__in=pks, user=user).delete()
    return {"deleted": n, "success": True,
            "message": f"Видалено: {n} подій"}


def _create_event(inp, user):
    from calendar_app.models import CalendarEvent
    from django.utils.dateparse import parse_datetime
    from django.utils.timezone import make_aware, is_naive

    title     = str(inp.get('title', ''))[:300]
    start_str = str(inp.get('start_at', ''))
    ev_type   = inp.get('event_type', 'reminder')
    desc      = str(inp.get('description', ''))[:1000]
    remind    = int(inp.get('remind_before_minutes', 60))

    if not title:
        return {"error": "title required"}

    try:
        dt = parse_datetime(start_str)
        if dt is None:
            raise ValueError
        if is_naive(dt):
            dt = make_aware(dt)
    except Exception:
        return {"error": f"Cannot parse start_at: '{start_str}'. Use YYYY-MM-DDTHH:MM:SS"}

    VALID = {'deadline', 'meeting', 'reminder', 'email_follow_up', 'other'}
    if ev_type not in VALID:
        ev_type = 'reminder'

    ev = CalendarEvent.objects.create(
        user=user, title=title, event_type=ev_type,
        start_at=dt, description=desc,
        remind_minutes_before=max(0, min(10080, remind)),
    )
    return {
        "success":     True,
        "event_id":    ev.pk,
        "title":       ev.title,
        "start":       ev.start_at.strftime('%d.%m.%Y %H:%M'),
        "calendar_url": f"/calendar/?year={ev.start_at.year}&month={ev.start_at.month}",
        "message":     f"Створено подію «{ev.title}» на {ev.start_at.strftime('%d.%m.%Y %H:%M')}",
    }


def _get_weekly_summary(inp, user):
    from calendar_app.models import CalendarEvent
    from datetime import timedelta

    now        = timezone.now()
    week_end   = now + timedelta(days=7)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    upcoming = list(
        CalendarEvent.objects.filter(
            user=user, is_done=False, start_at__gte=now, start_at__lte=week_end
        ).order_by('start_at')[:20].values('id', 'title', 'start_at', 'event_type')
    )
    overdue   = CalendarEvent.objects.filter(user=user, is_done=False, start_at__lt=now).count()
    done_today = CalendarEvent.objects.filter(
        user=user, is_done=True, start_at__gte=today_start
    ).count()

    return {
        "upcoming_7d":  [{"id": e['id'], "title": e['title'],
                          "start": e['start_at'].strftime('%d.%m %H:%M'),
                          "type": e['event_type']} for e in upcoming],
        "overdue_total": overdue,
        "done_today":    done_today,
        "summary":       f"Найближчі 7 днів: {len(upcoming)} подій. Прострочених: {overdue}. Виконано сьогодні: {done_today}.",
    }


# ── Context builder ───────────────────────────────────────────────────────────

def build_calendar_context(user) -> str:
    from calendar_app.models import CalendarEvent, CalendarCategory
    from datetime import timedelta

    now      = timezone.now()
    overdue  = CalendarEvent.objects.filter(user=user, is_done=False, start_at__lt=now).count()
    week     = CalendarEvent.objects.filter(
        user=user, is_done=False,
        start_at__gte=now, start_at__lte=now + timedelta(days=7),
    ).count()
    cats     = list(CalendarCategory.objects.filter(user=user).values_list('name', flat=True))

    lines = [
        f"Зараз: {now.strftime('%d.%m.%Y %H:%M %Z')}",
        f"Прострочених подій (невиконані після дати): {overdue}",
        f"Подій на наступні 7 днів: {week}",
    ]
    if cats:
        lines.append(f"Власні категорії: {', '.join(cats)}")
    return '\n'.join(lines)


# ── Chat loop ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ти — AI-асистент вбудованого календаря Minerva Business Intelligence.
Ти допомагаєш менеджеру або власнику малого бізнесу керувати своїм календарем, дедлайнами та завданнями.

Правила роботи:
- Відповідай коротко і конкретно, по-українськи
- Перш ніж видалити або позначити виконаними — перелічи які саме події зміниш (ID + назви)
- Якщо питання не стосується календаря — відповідай стисло і поверни до теми
- Для застарілих дедлайнів (відправлені замовлення, минулі зустрічі) — пропонуй mark_events_done, не delete
- Після успішної дії — коротко підтверди що зроблено і запитай що ще потрібно
"""


def calendar_chat(user_message: str, history: list, user) -> dict:
    """
    Run one turn of the calendar AI chat.
    history: list of {role, content} dicts (already-filtered conversation).
    Returns: {ok, reply, action} where action is optional hint for frontend (e.g. 'reload').
    """
    from strategy.models import AISettings
    from ai_assistant.budget_guard import check_budget, calc_cost
    from ai_assistant.models import AIBudgetLog

    # Budget check — use profile if available
    try:
        profile = user.userprofile
    except Exception:
        profile = None

    allowed, reason = check_budget(profile)
    if not allowed:
        msg = 'Бюджет AI вичерпано на цей місяць.' if 'monthly' in reason else 'Денний ліміт AI вичерпано.'
        return {'ok': False, 'error': msg}

    ai_settings = AISettings.get()
    api_key = ai_settings.anthropic_api_key
    if not api_key:
        return {'ok': False, 'error': 'Anthropic API ключ не налаштований у Системних налаштуваннях AI.'}

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    cal_ctx = build_calendar_context(user)
    system  = SYSTEM_PROMPT + f"\n\nПоточний стан календаря:\n{cal_ctx}"

    # Build messages list (last 20 entries max)
    messages = []
    for m in history[-20:]:
        if m.get('role') in ('user', 'assistant'):
            messages.append({'role': m['role'], 'content': m['content']})
    messages.append({'role': 'user', 'content': user_message})

    MODEL   = 'claude-haiku-4-5-20251001'
    action  = None

    for _iter in range(6):
        resp = client.messages.create(
            model=MODEL, max_tokens=1024,
            system=system,
            tools=CALENDAR_AI_TOOLS,
            messages=messages,
        )

        if resp.stop_reason == 'end_turn':
            reply = ''.join(b.text for b in resp.content if hasattr(b, 'text'))
            # Record cost to global budget log
            try:
                cost = calc_cost(MODEL, resp.usage.input_tokens, resp.usage.output_tokens)
                from django.db.models import F
                AIBudgetLog.objects.filter(pk=AIBudgetLog.current().pk).update(
                    total_cost_usd=F('total_cost_usd') + cost,
                    total_requests=F('total_requests') + 1,
                )
            except Exception:
                pass
            return {'ok': True, 'reply': reply, 'action': action}

        if resp.stop_reason == 'tool_use':
            messages.append({'role': 'assistant', 'content': resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != 'tool_use':
                    continue
                result = execute_calendar_tool(block.name, block.input, user)
                # Hint frontend to reload if state-changing tool succeeded
                if block.name in ('mark_events_done', 'delete_events', 'create_event') and result.get('success'):
                    action = 'reload'
                tool_results.append({
                    'type':        'tool_result',
                    'tool_use_id': block.id,
                    'content':     json.dumps(result, ensure_ascii=False, default=str),
                })
            messages.append({'role': 'user', 'content': tool_results})
            continue

        break  # unexpected stop_reason

    return {'ok': True, 'reply': 'Не вдалося отримати відповідь.', 'action': action}
