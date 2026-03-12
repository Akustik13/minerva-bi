from django import forms
from django.contrib import admin
from django.utils.html import format_html
from .models import APIKey, SCOPE_CHOICES


class APIKeyForm(forms.ModelForm):
    scopes_input = forms.MultipleChoiceField(
        choices=SCOPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Права доступу (scopes)',
    )

    class Meta:
        model = APIKey
        fields = ['name', 'is_active', 'expires_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['scopes_input'].initial = self.instance.scopes

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.scopes = self.cleaned_data.get('scopes_input', [])
        if commit:
            instance.save()
        return instance


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    form = APIKeyForm
    list_display  = ['name', 'key_preview', 'scopes_display', 'is_active',
                     'last_used', 'expires_at', 'created_at']
    list_filter   = ['is_active']
    search_fields = ['name']
    readonly_fields = ['key_display', 'created_at', 'last_used']

    fieldsets = [
        ('Загальне', {
            'fields': ['name', 'is_active', 'expires_at'],
        }),
        ('Права доступу', {
            'fields': ['scopes_input'],
            'description': 'Оберіть які операції дозволені для цього ключа.',
        }),
        ('Технічна інформація', {
            'fields': ['key_display', 'created_at', 'last_used'],
            'classes': ['collapse'],
        }),
    ]

    def key_preview(self, obj):
        return f"{obj.key[:8]}…{obj.key[-4:]}"
    key_preview.short_description = 'Ключ'

    def key_display(self, obj):
        if obj.pk:
            return format_html(
                '<code style="font-size:13px;user-select:all;'
                'background:#0d1117;padding:6px 10px;border-radius:4px;'
                'border:1px solid #2a3f52;display:inline-block;">{}</code>',
                obj.key
            )
        return '— буде згенеровано після збереження —'
    key_display.short_description = 'API Ключ (скопіюй)'

    def scopes_display(self, obj):
        if not obj.scopes:
            return '—'
        labels = {k: v for k, v in SCOPE_CHOICES}
        return ', '.join(labels.get(s, s) for s in obj.scopes)
    scopes_display.short_description = 'Права'
