"""core/signals.py — Auto-create UserProfile + audit login/logout."""
from django.db.models.signals import post_save
from django.contrib.auth import get_user_model
from django.dispatch import receiver

User = get_user_model()


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Auto-create UserProfile for new users."""
    if not created:
        return
    try:
        from core.models import UserProfile
        role = UserProfile.Role.SUPERADMIN if instance.is_superuser else UserProfile.Role.ADMIN
        UserProfile.objects.get_or_create(user=instance, defaults={'role': role})
    except Exception:
        pass


def _on_login(sender, request, user, **kwargs):
    try:
        from core.models import AuditLog
        AuditLog.objects.create(
            user=user,
            action=AuditLog.Action.LOGIN,
            ip_address=_get_ip(request),
            extra={'path': request.path},
        )
    except Exception:
        pass


def _on_logout(sender, request, user, **kwargs):
    try:
        from core.models import AuditLog
        AuditLog.objects.create(
            user=user,
            action=AuditLog.Action.LOGOUT,
            ip_address=_get_ip(request),
        )
    except Exception:
        pass


def _get_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')
