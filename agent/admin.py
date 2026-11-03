"""🤖 Доручення агента й 📜 журнал — лише власні скриньки (пошта — лише її власника, і для адміністратора)."""
from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from agent import mail as agent_mail
from agent.models import AgentRule, AgentRun

STATUS_ICONS = {AgentRun.QUEUED: '⏳', AgentRun.RUNNING: '⚙️', AgentRun.DRAFTED: '📝', AgentRun.SCHEDULED: '✉️',
                AgentRun.SKIPPED: '↷', AgentRun.FAILED: '⚠️'}


def _mail_link(msg):
    subject = (msg.subject or '(без теми)')[:80]
    return format_html('<a href="/email/message/{}/" target="_blank" translate="no">{}</a> '
                       '<span style="opacity:.7" translate="no">{}</span>', msg.pk, subject, msg.from_email)


def _result_link(run):
    if run.draft_id:
        return format_html('<a href="/email/compose/?draft={}">✏️ відкрити чернетку</a>', run.draft_id)
    if run.scheduled_id:
        return format_html('<a href="/admin/email_assistant/scheduledemail/{}/change/">⏳ запланований лист</a>',
                           run.scheduled_id)
    return ''


class AgentRuleForm(forms.ModelForm):
    class Meta:
        model = AgentRule
        fields = ['name', 'enabled', 'account', 'senders', 'subject_words', 'body_words', 'category',
                  'instruction', 'mode', 'send_delay_minutes', 'daily_limit']
        widgets = {
            'senders': forms.Textarea(attrs={'rows': 3}),
            'subject_words': forms.Textarea(attrs={'rows': 3}),
            'body_words': forms.Textarea(attrs={'rows': 3}),
            'instruction': forms.Textarea(attrs={'rows': 8}),
        }

    def clean(self):
        data = super().clean()
        rule = AgentRule(senders=data.get('senders') or '', subject_words=data.get('subject_words') or '',
                         body_words=data.get('body_words') or '', category=data.get('category'))
        if not rule.has_conditions:
            raise forms.ValidationError('Потрібна хоча б одна умова: від кого, тема, текст або своя категорія — '
                                        'інакше агент відповідав би на всі листи.')
        return data


@admin.register(AgentRule)
class AgentRuleAdmin(admin.ModelAdmin):
    form = AgentRuleForm
    list_display = ('name', 'account', 'mode_col', 'enabled', 'conditions', 'stats', 'last_run')
    list_filter = ('enabled', 'mode')
    search_fields = ('name', 'senders', 'subject_words', 'instruction')
    readonly_fields = ('promote_panel', 'recent_runs', 'example_link', 'streak', 'created_by', 'created_at')
    fieldsets = (
        (None, {'fields': ('promote_panel', 'name', 'enabled', 'account')}),
        ('Які листи — усі задані умови разом', {
            'fields': ('senders', 'subject_words', 'body_words', 'category', 'example_link'),
            'description': 'У кожному полі — по одному в рядку, підходить будь-який рядок. Порожнє поле не '
                           'перевіряється. Агент бере лише нові листи у Вхідних (не старші за три дні), не відповідає '
                           'на свої листи, noreply-адреси, автовідповіді й листи, на які вже відповіли.',
        }),
        ('Що робити', {'fields': ('instruction', 'mode', 'send_delay_minutes', 'daily_limit', 'streak')}),
        ('📜 Останні листи за цим дорученням', {'fields': ('recent_runs',)}),
        ('Службове', {'fields': ('created_by', 'created_at'), 'classes': ('collapse',)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).filter(account__user=request.user).select_related('account', 'category')

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'account':
            from email_assistant.models import EmailAccount
            kwargs['queryset'] = EmailAccount.objects.filter(user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.account.user_id != request.user.pk:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.account.user_id != request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if obj.account.user_id != request.user.pk:   # форма дає лише свої скриньки — це запобіжник
            raise PermissionError('Не ваша скринька')
        if not change:
            obj.created_by = request.user
            obj.seen_pk = agent_mail.start_pk(obj.account)
        elif 'account' in form.changed_data:
            obj.seen_pk = agent_mail.start_pk(obj.account)
        super().save_model(request, obj, form, change)

    @admin.display(description='Режим')
    def mode_col(self, obj):
        return '✉️ сам' if obj.mode == AgentRule.SEND else '📝 чернетка'

    @admin.display(description='Умови')
    def conditions(self, obj):
        parts = []
        for field, label in (('senders', 'від'), ('subject_words', 'тема'), ('body_words', 'текст')):
            lines = [ln.strip() for ln in (getattr(obj, field) or '').splitlines() if ln.strip()]
            if lines:
                parts.append(f'{label}: {", ".join(lines[:3])}' + (' …' if len(lines) > 3 else ''))
        if obj.category_id:
            parts.append(f'категорія: {obj.category}')
        return format_html('<span translate="no">{}</span>', ' · '.join(parts) or '—')

    @admin.display(description='Листів')
    def stats(self, obj):
        runs = obj.runs.all()
        counts = {
            '📝': runs.filter(status=AgentRun.DRAFTED).count(),
            '✉️': runs.filter(status=AgentRun.SCHEDULED).count(),
            '✓': runs.filter(outcome=AgentRun.SENT_SAME).count(),
            '✏️': runs.filter(outcome=AgentRun.SENT_EDITED).count(),
            '↷': runs.filter(status=AgentRun.SKIPPED).count(),
        }
        return ' · '.join(f'{k} {v}' for k, v in counts.items() if v) or '—'

    @admin.display(description='Остання робота')
    def last_run(self, obj):
        run = obj.runs.order_by('-created_at').first()
        return timezone.localtime(run.created_at).strftime('%d.%m %H:%M') if run else '—'

    @admin.display(description='Стан')
    def promote_panel(self, obj):
        if obj is None or not obj.pk:
            return format_html('<div style="opacity:.8">Простіше створити доручення з листа: у пошті правий клік на '
                               'листі → «🤖 Доручити агенту…» — там можна й спробувати відповідь на цьому листі.</div>')
        if agent_mail.promote_ready(obj):
            return format_html(
                '<div style="padding:8px 12px;border-radius:6px;background:rgba(46,125,50,.14);'
                'border:1px solid rgba(46,125,50,.4)">✅ {} чернеток поспіль ви надіслали без змін. Можна дозволити '
                'агенту надсилати самому: «Режим» → «Надсилати самому» нижче й «Зберегти». Лист стане в «⏳ '
                'Заплановані» на {} хв — до відправки можна скасувати.</div>', obj.streak, obj.send_delay_minutes)
        return format_html('<div style="opacity:.8">{} · без змін поспіль: {} (після {} — пропозиція надсилати '
                           'самому)</div>', obj.get_mode_display(), obj.streak, agent_mail.PROMOTE_AFTER)

    @admin.display(description='Створено з листа')
    def example_link(self, obj):
        return _mail_link(obj.example_message) if obj.example_message_id else '—'

    @admin.display(description='Листи')
    def recent_runs(self, obj):
        if obj is None or not obj.pk:
            return '—'
        runs = obj.runs.select_related('message').order_by('-created_at')[:15]
        if not runs:
            return 'Ще жодного листа — агент перевіряє нові листи щохвилини.'
        return format_html(
            '<table style="width:100%"><tbody>{}</tbody></table>',
            format_html_join('', '<tr><td style="white-space:nowrap">{}</td><td>{}</td><td>{} {}</td>'
                                 '<td>{}</td><td>{}</td><td>{}</td></tr>',
                             ((timezone.localtime(r.created_at).strftime('%d.%m %H:%M'), _mail_link(r.message),
                               STATUS_ICONS.get(r.status, ''), r.get_status_display(),
                               r.get_outcome_display() if r.outcome else '', r.reason, _result_link(r))
                              for r in runs)))


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'rule', 'mail', 'status_col', 'outcome', 'reason', 'result')
    list_filter = ('status', 'outcome', ('rule', admin.RelatedOnlyFieldListFilter))
    search_fields = ('message__subject', 'message__from_email', 'reason')
    list_select_related = ('rule', 'message')
    readonly_fields = ('rule', 'mail', 'status', 'reason', 'reply', 'model', 'result', 'outcome',
                       'created_at', 'started_at', 'finished_at')
    fields = readonly_fields
    date_hierarchy = 'created_at'

    def get_queryset(self, request):
        return super().get_queryset(request).filter(rule__account__user=request.user)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.rule.account.user_id != request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description='Лист')
    def mail(self, obj):
        return _mail_link(obj.message)

    @admin.display(description='Стан', ordering='status')
    def status_col(self, obj):
        return f'{STATUS_ICONS.get(obj.status, "")} {obj.get_status_display()}'

    @admin.display(description='Результат')
    def result(self, obj):
        return _result_link(obj) or '—'
