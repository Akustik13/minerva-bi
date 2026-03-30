"""core/mixins.py — AuditableMixin for ModelAdmin classes."""


def _get_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class AuditableMixin:
    """
    Add to ModelAdmin to log create/update/delete actions to AuditLog.

    Usage:
        class MyAdmin(AuditableMixin, admin.ModelAdmin):
            ...
    """

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
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
