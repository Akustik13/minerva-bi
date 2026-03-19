from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse

from .models import (
    StrategyTemplate, TemplateStep,
    CustomerStrategy, CustomerStep, StepLog,
)


class TemplateStepInline(admin.TabularInline):
    model = TemplateStep
    extra = 1
    fields = ("order", "step_type", "title", "delay_days",
              "next_step_yes", "next_step_no", "canvas_x", "canvas_y")
    ordering = ("order",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in ("next_step_yes", "next_step_no"):
            # Limit FK queryset to steps of the same template
            obj_id = request.resolver_match.kwargs.get("object_id")
            if obj_id:
                kwargs["queryset"] = TemplateStep.objects.filter(template_id=obj_id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(StrategyTemplate)
class StrategyTemplateAdmin(admin.ModelAdmin):
    list_display  = ("name", "behavior_type", "steps_count", "is_active", "created_at")
    list_filter   = ("behavior_type", "is_active")
    search_fields = ("name", "description")
    inlines       = [TemplateStepInline]

    @admin.display(description="Кроків")
    def steps_count(self, obj):
        return obj.steps.count()


class CustomerStepInline(admin.TabularInline):
    model  = CustomerStep
    extra  = 0
    fields = ("step_type", "title", "scheduled_at", "outcome", "outcome_notes", "completed_at")
    readonly_fields = ("completed_at",)
    ordering = ("scheduled_at",)


@admin.register(CustomerStrategy)
class CustomerStrategyAdmin(admin.ModelAdmin):
    list_display  = ("customer", "template", "status", "next_action_at",
                     "current_step_display", "canvas_link")
    list_filter   = ("status", "template")
    search_fields = ("customer__name", "customer__company", "template__name")
    autocomplete_fields = ("customer",)
    readonly_fields = ("started_at", "canvas_link")
    inlines       = [CustomerStepInline]

    fieldsets = (
        (None, {"fields": ("customer", "template", "name", "status")}),
        ("Прогрес", {"fields": ("current_step", "started_at", "next_action_at", "notes")}),
        ("Дії", {"fields": ("canvas_link",)}),
    )

    @admin.display(description="Поточний крок")
    def current_step_display(self, obj):
        if obj.current_step:
            return obj.current_step.title
        return "—"

    @admin.display(description="Canvas")
    def canvas_link(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("strategy:canvas", args=[obj.pk])
        return format_html(
            '<a href="{}" target="_blank" class="button">'
            '🗺 Відкрити canvas</a>', url
        )


@admin.register(CustomerStep)
class CustomerStepAdmin(admin.ModelAdmin):
    list_display  = ("strategy", "step_type", "title", "scheduled_at", "outcome")
    list_filter   = ("step_type", "outcome")
    search_fields = ("title", "strategy__customer__name")
    readonly_fields = ("completed_at",)


@admin.register(StepLog)
class StepLogAdmin(admin.ModelAdmin):
    list_display  = ("step", "logged_by", "outcome", "logged_at")
    list_filter   = ("outcome",)
    search_fields = ("step__title", "notes")
    readonly_fields = ("logged_at",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
