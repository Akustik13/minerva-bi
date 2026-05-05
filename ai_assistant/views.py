import json
import logging
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.models import UserProfile

logger = logging.getLogger(__name__)


def _get_profile(user):
    try:
        return UserProfile.objects.select_related('user').get(user=user)
    except UserProfile.DoesNotExist:
        return None


@login_required
def webchat_view(request):
    profile = _get_profile(request.user)
    return render(request, 'ai_assistant/webchat.html', {'profile': profile})


@login_required
@require_POST
def chat_api(request):
    try:
        data = json.loads(request.body)
        user_text = (data.get('message') or '').strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    if not user_text:
        return JsonResponse({'error': 'Empty message'}, status=400)

    profile = _get_profile(request.user)

    if profile and not profile.ai_enabled:
        return JsonResponse({'reply': '🔒 AI-асистент для вас вимкнений.'})

    try:
        from .service import chat
        reply = chat(
            user_text=user_text,
            profile=profile,
            channel='webchat',
        )
    except Exception as e:
        logger.exception("AI chat error for user %s: %s", request.user, e)
        return JsonResponse({'reply': f'⚠️ Помилка: {type(e).__name__}: {e}'})

    return JsonResponse({'reply': reply})


@login_required
@require_POST
def reset_chat(request):
    profile = _get_profile(request.user)
    from .service import reset_conversation
    reset_conversation(profile, channel='webchat')
    return JsonResponse({'ok': True})


@login_required
def history_api(request):
    """Return active webchat conversation messages (user + assistant only)."""
    profile = _get_profile(request.user)
    if not profile:
        return JsonResponse({'messages': []})

    from .models import AIConversation
    conv = AIConversation.objects.filter(
        user_profile=profile, channel='webchat', is_active=True,
    ).first()
    if not conv:
        return JsonResponse({'messages': []})

    msgs = []
    for m in conv.messages.filter(role__in=('user', 'assistant')).order_by('created_at'):
        msgs.append({'role': m.role, 'content': m.content})
    return JsonResponse({'messages': msgs})


_DIAGNOSTIC_COMMANDS = [
    {
        'key':         'morning_briefing',
        'label':       'Morning Briefing',
        'description': 'Генерує і надсилає ранковий брифінг в Telegram',
    },
    {
        'key':         'auto_advance',
        'label':       'Auto Advance Strategies',
        'description': 'Просуває кроки стратегій (pause/email timeout)',
    },
    {
        'key':         'fetch_emails',
        'label':       'Fetch Emails (IMAP)',
        'description': 'Читає пошту і додає листи в хронологію CRM',
    },
    {
        'key':         'send_reminders',
        'label':       'Send Reminders',
        'description': 'Надсилає нагадування з CustomerTimeline',
    },
]

_CMD_MAP = {
    'morning_briefing': 'morning_briefing',
    'auto_advance':     'auto_advance_strategies',
    'fetch_emails':     'fetch_emails',
    'send_reminders':   'send_reminders',
}


@login_required
def tools_diagnostic_view(request):
    """Diagnostic page — staff only."""
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()
    from .tools import ALL_TOOLS
    return render(request, 'ai_assistant/diagnostic.html', {
        'tools':               ALL_TOOLS,
        'diagnostic_commands': _DIAGNOSTIC_COMMANDS,
    })


@login_required
@require_POST
def run_tool_diagnostic(request):
    """Run a single tool (or management command) and return JSON result with timing."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    import time
    try:
        data = json.loads(request.body)
        tool_name  = data.get('tool', '') or data.get('tool_name', '')
        tool_input = data.get('input', {}) or data.get('tool_input', {})
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    # ── Management command runner ────────────────────────────────
    if tool_name.startswith('__cmd_'):
        cmd_key = tool_name[len('__cmd_'):]
        cmd = _CMD_MAP.get(cmd_key)
        if not cmd:
            return JsonResponse({'error': f'Unknown command: {cmd_key}'}, status=400)
        from io import StringIO
        from django.core.management import call_command
        t0  = time.monotonic()
        out = StringIO()
        try:
            call_command(cmd, stdout=out, stderr=out)
            output = out.getvalue().strip() or 'OK (no output)'
            has_error = False
        except Exception as e:
            output    = f'ERROR: {e}'
            has_error = True
        ms = int((time.monotonic() - t0) * 1000)
        return JsonResponse({
            'result':    {'output': output},
            'ms':        ms,
            'tool':      tool_name,
            'has_error': has_error,
        })

    # ── Regular tool ─────────────────────────────────────────────
    from .tools import execute_tool
    profile = _get_profile(request.user)
    t0 = time.monotonic()
    result = execute_tool(tool_name, tool_input, profile)
    ms = int((time.monotonic() - t0) * 1000)

    return JsonResponse({'result': result, 'ms': ms, 'tool': tool_name})
