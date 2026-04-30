"""core/context_processors.py — Inject user role + allowed modules into templates."""
import json


def user_modules(request):
    """
    Returns:
        allowed_modules_json: JSON string — list of app_labels or the string '__all__'
        user_role: role string ('admin', 'manager', etc.)
    """
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return {'allowed_modules_json': '"__all__"', 'user_role': ''}
    try:
        profile = request.user.profile
        modules = profile.get_allowed_modules()
        denied  = profile.denied_models or []
        return {
            'allowed_modules_json': json.dumps(modules),
            'denied_models_json':   json.dumps(denied),
            'user_role': profile.role,
        }
    except Exception:
        return {'allowed_modules_json': '"__all__"', 'denied_models_json': '[]', 'user_role': ''}
