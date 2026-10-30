"""🌐 Переклади інтерфейсу (core/ui_text): каталог написів, AI-переклад з оцінкою вартості, ручні правки.
Лише адміністратор Minerva (переклад коштує грошей рахунку Anthropic)."""
import re

from django import forms
from django.contrib import admin, messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import path
from django.utils.html import format_html

from .models import UIText


class UITextStateFilter(admin.SimpleListFilter):
    title = 'Стан'
    parameter_name = 'state'

    def lookups(self, request, model_admin):
        return [('missing', '⏳ Бракує перекладу'), ('ai', '🤖 Переклав AI'), ('human', '✋ Виправлено людиною'),
                ('gone', '🗑 Більше немає в коді')]

    def queryset(self, request, queryset):
        from core.ui_text.catalog import TARGETS
        v = self.value()
        if v == 'gone':
            return queryset.filter(active=False)
        if not v:
            return queryset
        queryset = queryset.filter(active=True)
        ids = []
        for pk, tr in queryset.values_list('pk', 'translations'):
            items = [(tr or {}).get(lang) or {} for lang in TARGETS]
            if (v == 'missing' and any(not i.get('text') for i in items)) or \
               (v in ('ai', 'human') and any(i.get('by') == v for i in items)):
                ids.append(pk)
        return queryset.filter(pk__in=ids)


class UITextForm(forms.ModelForm):
    text_en = forms.CharField(label='English', required=False, widget=forms.Textarea(attrs={'rows': 2, 'cols': 90}))
    text_de = forms.CharField(label='Deutsch', required=False, widget=forms.Textarea(attrs={'rows': 2, 'cols': 90}))

    class Meta:
        model = UIText
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['text_en'].initial = self.instance.text('en')
            self.fields['text_de'].initial = self.instance.text('de')

    def clean(self):
        from core.ui_text.translate import valid
        data = super().clean()
        need = ', '.join(sorted(set(re.findall(r'\{\d+\}', self.instance.source)))) or 'жодного'
        for lang in ('en', 'de'):
            text = (data.get(f'text_{lang}') or '').strip()
            if text and not valid(self.instance.source, text):
                self.add_error(f'text_{lang}', f'Мають лишитися ті самі місця вставок, що в написі: {need}')
        return data


@admin.register(UIText)
class UITextAdmin(admin.ModelAdmin):
    """Каталог написів і переклади: зібрати з коду, перекласти AI (з оцінкою вартості), виправити вручну."""
    form = UITextForm
    change_list_template = 'admin/core/uitext/change_list.html'
    list_display = ('source_col', 'en_col', 'de_col', 'where', 'uses')
    list_filter = (UITextStateFilter,)
    search_fields = ('source',)
    readonly_fields = ('source', 'where', 'uses', 'active', 'origin')
    fieldsets = ((None, {'fields': ('source', 'text_en', 'text_de', 'origin', 'where', 'uses', 'active')}),)
    list_per_page = 100
    actions = ['action_translate_selected']

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        from core.utils import is_minerva_admin
        return is_minerva_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Напис (українською)', ordering='source')
    def source_col(self, obj):
        style = '' if obj.active else 'opacity:.5;text-decoration:line-through'
        return format_html('<span translate="no" style="{}">{}</span>', style, obj.source[:160])

    def _col(self, obj, lang):
        item = (obj.translations or {}).get(lang) or {}
        if not item.get('text'):
            return format_html('<span title="{}">⏳</span>', 'ще не перекладено')
        mark = '✋' if item.get('by') == 'human' else '🤖'
        return format_html('<span translate="no">{} {}</span>', mark, item['text'][:160])

    @admin.display(description='English')
    def en_col(self, obj):
        return self._col(obj, 'en')

    @admin.display(description='Deutsch')
    def de_col(self, obj):
        return self._col(obj, 'de')

    @admin.display(description='Звідки переклад')
    def origin(self, obj):
        parts = []
        for lang, item in sorted((obj.translations or {}).items()):
            by = '✋ людина' if item.get('by') == 'human' else f"🤖 AI {item.get('model', '')}"
            parts.append(f"{lang}: {by}, {item.get('at', '')}")
        return ' · '.join(parts) or '—'

    def save_model(self, request, obj, form, change):
        from core.ui_text import catalog
        for lang in ('en', 'de'):
            text = (form.cleaned_data.get(f'text_{lang}') or '').strip()
            if text != obj.text(lang):
                catalog.set_text(obj, lang, text, by='human')

    def get_urls(self):
        return [
            path('collect/', self.admin_site.admin_view(self.collect_view), name='core_uitext_collect'),
            path('translate/', self.admin_site.admin_view(self.translate_view), name='core_uitext_translate'),
            path('status/', self.admin_site.admin_view(self.status_view), name='core_uitext_status'),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        from core.ui_text import catalog, translate
        rows, langs = translate.todo()
        estimates = [{'model': m, 'label': label, **translate.estimate(rows, langs, m)}
                     for m, label in translate.MODELS.items()]
        extra = {'ui_counts': catalog.counts(), 'ui_targets': catalog.TARGETS, 'ui_estimates': estimates,
                 'ui_state': translate.state(), 'ui_default_model': translate.DEFAULT_MODEL,
                 'title': '🌐 Переклади інтерфейсу'}
        return super().changelist_view(request, {**extra, **(extra_context or {})})

    def collect_view(self, request):
        from core.ui_text import catalog
        if request.method != 'POST' or not self.has_change_permission(request):
            return redirect('admin:core_uitext_changelist')
        res = catalog.sync()
        back = f" · повернулось: {res['back']}" if res['back'] else ''
        messages.success(request, f"Написів у коді: {res['total']} · нових: {res['new']} · "
                                  f"зникло з коду: {res['gone']}{back}.")
        return redirect('admin:core_uitext_changelist')

    def translate_view(self, request):
        from core.ui_text import translate
        if request.method != 'POST' or not self.has_change_permission(request):
            return redirect('admin:core_uitext_changelist')
        model = request.POST.get('model') or translate.DEFAULT_MODEL
        if translate.start(model=model, who=request.user.get_username()):
            messages.info(request, 'Переклад запущено у фоні — хід видно вгорі сторінки.')
        else:
            messages.warning(request, 'Переклад уже йде.')
        return redirect('admin:core_uitext_changelist')

    def status_view(self, request):
        from core.ui_text import catalog, translate
        if not self.has_view_permission(request):
            return JsonResponse({}, status=403)
        return JsonResponse({'state': translate.state(), 'counts': catalog.counts()})

    @admin.action(description='🤖 Перекласти вибрані (AI, Sonnet 5.5)')
    def action_translate_selected(self, request, queryset):
        from core.ui_text import translate
        res = translate.run(ids=list(queryset.values_list('pk', flat=True)), who=request.user.get_username())
        problem = f" · {res['problem']}" if res.get('problem') else ''
        self.message_user(request, f"Перекладено: {res.get('saved', 0)} · відхилено перевіркою: "
                                   f"{res.get('rejected', 0)}{problem}")
