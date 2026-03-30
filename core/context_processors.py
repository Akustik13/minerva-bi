"""core/context_processors.py — Inject user role + allowed modules into templates."""


def user_modules(request):
    """
    Returns:
        allowed_modules: list of app_labels or '__all__'
        user_role: role string ('admin', 'manager', etc.)
    """
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return {'allowed_modules': '__all__', 'user_role': ''}
    try:
        profile = request.user.profile
        return {
            'allowed_modules': profile.get_allowed_modules(),
            'user_role': profile.role,
        }
    except Exception:
        return {'allowed_modules': '__all__', 'user_role': ''}
