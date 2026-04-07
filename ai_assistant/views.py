import json
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from core.models import UserProfile


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

    from .service import chat
    try:
        reply = chat(
            user_text=user_text,
            profile=profile,
            channel='webchat',
        )
    except Exception as e:
        return JsonResponse({'reply': 'Помилка сервісу. Спробуй пізніше.'})

    return JsonResponse({'reply': reply})


@login_required
@require_POST
def reset_chat(request):
    profile = _get_profile(request.user)
    from .service import reset_conversation
    reset_conversation(profile, channel='webchat')
    return JsonResponse({'ok': True})
