"""core/mixins.py — AuditableMixin for ModelAdmin classes."""


def _get_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class AuditableMixin:
    """
    Add to ModelAdmin to log create/update/delete actions to AuditLog.
    Also bypasses Django's model-level permission checks — Minerva controls
    access via UserProfile.allowed_modules + middleware instead.

    Usage:
        class MyAdmin(AuditableMixin, admin.ModelAdmin):
            ...
    """

    def _minerva_staff_ok(self, request):
        return request.user.is_active and request.user.is_staff

    def has_module_permission(self, request):
        return self._minerva_staff_ok(request)

    def has_view_permission(self, request, obj=None):
        if not self._minerva_staff_ok(request):
            return False
        return not self._minerva_model_denied(request)

    def has_add_permission(self, request):
        return self._minerva_staff_ok(request)

    def has_change_permission(self, request, obj=None):
        return self._minerva_staff_ok(request)

    def _minerva_model_denied(self, request):
        """Return True if this model is in the user's denied_models list."""
        try:
            profile = request.user.profile
            denied = getattr(profile, 'denied_models', None) or []
            if not denied:
                return False
            key = f'{self.opts.app_label}:{self.opts.object_name}'
            return key in denied
        except Exception:
            return False

    def save_model(self, request, obj, form, change):
        # Set thread-local user BEFORE super() so signals fired by save() can read it
        try:
            from core.utils import set_current_user, clear_current_user
            set_current_user(request.user)
        except Exception:
            pass
        try:
            super().save_model(request, obj, form, change)
        finally:
            try:
                from core.utils import clear_current_user
                clear_current_user()
            except Exception:
                pass
        try:
            from core.models import AuditLog
            from django.contrib.contenttypes.models import ContentType
            action = AuditLog.Action.UPDATE if change else AuditLog.Action.CREATE
            AuditLog.objects.create(
                user=request.user,
                action=action,
                content_type=ContentType.objects.get_for_model(obj),
                object_id=str(obj.pk),
                object_repr=str(obj)[:500],
                ip_address=_get_ip(request),
                extra={'changed_fields': list(form.changed_data) if change else []},
            )
        except Exception:
            pass

    def delete_model(self, request, obj):
        try:
            from core.models import AuditLog
            from django.contrib.contenttypes.models import ContentType
            AuditLog.objects.create(
                user=request.user,
                action=AuditLog.Action.DELETE,
                content_type=ContentType.objects.get_for_model(obj),
                object_id=str(obj.pk),
                object_repr=str(obj)[:500],
                ip_address=_get_ip(request),
            )
        except Exception:
            pass
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        try:
            from core.models import AuditLog
            from django.contrib.contenttypes.models import ContentType
            items = list(queryset)
            if items:
                ct = ContentType.objects.get_for_model(queryset.model)
                AuditLog.objects.bulk_create([
                    AuditLog(
                        user=request.user,
                        action=AuditLog.Action.DELETE,
                        content_type=ct,
                        object_id=str(obj.pk),
                        object_repr=str(obj)[:500],
                        ip_address=_get_ip(request),
                    )
                    for obj in items
                ])
        except Exception:
            pass
        super().delete_queryset(request, queryset)


class MinervaAdminMixin(AuditableMixin):
    """
    Extends AuditableMixin with role-based permissions.
    Checks UserProfile.can_delete/can_export/can_import via user_can().
    Also checks module_operations (Layer 4) for add/change/delete.

    Usage:
        class MyAdmin(MinervaAdminMixin, admin.ModelAdmin):
            ...
    """

    def _get_app_label(self):
        try:
            return self.opts.app_label
        except Exception:
            return ''

    def has_add_permission(self, request):
        if not super().has_add_permission(request):
            return False
        try:
            from core.utils import user_has_operation
            return user_has_operation(request.user, self._get_app_label(), 'add')
        except Exception:
            return True

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        try:
            from core.utils import user_has_operation
            return user_has_operation(request.user, self._get_app_label(), 'change')
        except Exception:
            return True

    def has_delete_permission(self, request, obj=None):
        if not self._minerva_staff_ok(request):
            return False
        try:
            from core.utils import user_can, user_has_operation
            if not user_can(request.user, 'delete'):
                return False
            return user_has_operation(request.user, self._get_app_label(), 'delete')
        except Exception:
            return True
