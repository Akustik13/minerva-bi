"""🤖 Доручення агента й 📜 журнал — лише власні скриньки (пошта — лише її власника, і для адміністратора);
🤖 Завдання агенту — лише свої (сторінка перевірки: ✓ / ✕ пропозицій, лист, зупинити / продовжити)."""
import json
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from agent import mail as agent_mail
from agent import products as product_fields
from agent.models import AgentProposal, AgentRule, AgentRun, AgentSchedule, AgentTask

# ✓ на сторінці завдання — кому є що застосувати (кожна пропозиція ще перевіряє своє право, agent/decide.py)
APPLY_PERMS = ('inventory.change_product', 'inventory.add_product', 'inventory.add_inventorytransaction',
               'calendar_app.add_calendarevent', 'shop.change_shoplisting', 'bots.change_digikeylisting',
               'shipping.add_shipment', 'sales.change_salesorder', 'inventory.add_purchaseorder')
STATUS_ICONS = {AgentRun.QUEUED: '⏳', AgentRun.RUNNING: '⚙️', AgentRun.DRAFTED: '📝', AgentRun.SCHEDULED: '✉️',
                AgentRun.TASKED: '🧩', AgentRun.SKIPPED: '↷', AgentRun.FAILED: '⚠️'}


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
    if run.task_id:
        return format_html('<a href="/admin/agent/agenttask/{}/review/">🧩 завдання #{}</a>', run.task_id, run.task_id)
    return ''


class AgentRuleForm(forms.ModelForm):
    class Meta:
        model = AgentRule
        fields = ['name', 'enabled', 'account', 'kind', 'senders', 'subject_words', 'body_words', 'category',
                  'instruction', 'mode', 'send_delay_minutes', 'daily_limit', 'forward_to', 'cc', 'auto_apply',
                  'task_model', 'task_budget_usd', 'importance', 'notify', 'opt_important', 'opt_receipt',
                  'remind_days', 'remind_until']
        widgets = {
            'senders': forms.Textarea(attrs={'rows': 3}),
            'subject_words': forms.Textarea(attrs={'rows': 3}),
            'body_words': forms.Textarea(attrs={'rows': 3}),
            'instruction': forms.Textarea(attrs={'rows': 8}),
            'cc': forms.Textarea(attrs={'rows': 2}),
            'forward_to': forms.Textarea(attrs={'rows': 2}),
        }

    def clean(self):
        data = super().clean()
        rule = AgentRule(senders=data.get('senders') or '', subject_words=data.get('subject_words') or '',
                         body_words=data.get('body_words') or '', category=data.get('category'))
        if not rule.has_conditions:
            raise forms.ValidationError('Потрібна хоча б одна умова: від кого, тема, текст або своя категорія — '
                                        'інакше агент відповідав би на всі листи.')
        import re as _re
        for field in ('cc', 'forward_to'):
            bad = [x for x in _re.split(r'[\s,;]+', data.get(field) or '')
                   if x and not agent_mail.EMAIL_RE.fullmatch(x)]
            if bad:
                self.add_error(field, 'Не схоже на адресу: ' + ', '.join(bad[:3]))
        if data.get('kind') not in AgentRule.NO_INSTRUCTION and not (data.get('instruction') or '').strip():
            self.add_error('instruction', 'Напишіть, що робити — інструкцію агенту.')
        if data.get('kind') == AgentRule.MAIL_FORWARD and not (data.get('forward_to') or '').strip():
            self.add_error('forward_to', 'Вкажіть, кому пересилати.')
        long_lines = [ln for ln in (data.get('body_words') or '').splitlines() if len(ln.split()) > 6]
        if long_lines:
            self.add_error('body_words', 'Це слова чи фраза, яка має буквально бути в тексті листа (по одній у рядку). '
                                         'Пояснення, що робити, — у «Що робити — інструкція агенту».')
        return data


# Написи результату «🧪 Що зробив би агент» — тут, а не в JS (переклади інтерфейсу беруть їх з Python)
_TEST_LABELS = {'not_match': '⚠️ Лист не підходить під умови:', 'to': 'Кому:', 'cc': 'Копія:', 'order': 'Замовлення:',
                'tracking': 'трек-номер', 'subject': 'Тема:', 'att': 'Вкладення:', 'none': 'немає',
                'opts': 'Лист піде', 'model': 'Модель AI:'}
_TEST_JS = """(function () {
  var URL = '/admin/agent/agentrule/%(pk)s/test/';
  var L = JSON.parse(document.getElementById('art-labels').dataset.l);
  document.querySelectorAll('[data-art]').forEach(function (b) {
    b.addEventListener('click', async function () {
      var row = document.getElementById('art-res-' + b.dataset.art), pre = row.querySelector('pre');
      row.hidden = false; pre.textContent = '⏳ …'; b.disabled = true;
      var csrf = (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
      try {
        var r = await fetch(URL + b.dataset.art + '/', {method: 'POST', credentials: 'same-origin',
                                                        headers: {'X-CSRFToken': csrf}});
        var d = await r.json();
        if (!d.ok) { pre.textContent = '❌ ' + (d.error || ''); return; }
        var lines = [d.outcome || ''];
        if (!d.matches) lines.push(L.not_match + ' ' + d.why);
        if (d.to) lines.push(L.to + ' ' + (d.to_name ? d.to_name + ' <' + d.to + '>' : d.to));
        if (d.cc && d.cc.length) lines.push(L.cc + ' ' + d.cc.join(', '));
        if (d.order) lines.push(L.order + ' ' + d.order + (d.tracking ? ' · ' + L.tracking + ' ' + d.tracking : ''));
        if (d.subject) lines.push(L.subject + ' ' + d.subject);
        if (d.attachments) lines.push(L.att + ' ' + (d.attachments.join(', ') || L.none));
        if (d.options) lines.push(L.opts + ' ' + d.options);
        if (d.model) lines.push(L.model + ' ' + d.model);
        if (d.reply && d.kind !== 'mail_mark') lines.push('', d.reply);
        pre.textContent = lines.join('\\n');
      } catch (e) { pre.textContent = '❌ ' + e; }
      finally { b.disabled = false; }
    });
  });
})();"""


@admin.register(AgentRule)
class AgentRuleAdmin(admin.ModelAdmin):
    form = AgentRuleForm
    list_display = ('name', 'account', 'mode_col', 'enabled', 'conditions', 'stats', 'last_run')
    list_filter = ('enabled', 'kind', 'mode')
    search_fields = ('name', 'senders', 'subject_words', 'instruction')
    readonly_fields = ('promote_panel', 'recent_runs', 'example_link', 'streak', 'created_by', 'created_at',
                       'test_panel')
    fieldsets = (
        (None, {'fields': ('promote_panel', 'name', 'enabled', 'account', 'kind')}),
        ('🧪 Перевірити, як працює', {'fields': ('test_panel',),
                                     'description': 'Що буде після цих налаштувань: опис, листи скриньки, що '
                                                    'підходять, і «🧪 Що зробив би агент» на будь-якому з них.'}),
        ('Які листи — усі задані умови разом', {
            'fields': ('senders', 'subject_words', 'body_words', 'category', 'example_link'),
            'description': 'У кожному полі — по одному в рядку, підходить будь-який рядок; «Текст листа містить» — '
                           'слова чи фраза, що буквально є в листі. Порожнє поле не перевіряється. Агент бере лише '
                           'нові листи у Вхідних (не старші за три дні). Відповідь — не на свої листи, noreply-адреси, '
                           'автовідповіді й листи, на які вже відповіли; 🧩 завдання — і з листів колег.',
        }),
        ('Що робити', {'fields': ('instruction', 'daily_limit')}),
        ('✉️ Лист: відповідь, клієнту замовлення або пересилання', {
            'fields': ('mode', 'send_delay_minutes', 'forward_to', 'cc', 'streak'),
            'description': 'Для «Відповідати на такі листи» і «📨 Писати клієнту замовлення». Клієнту — замовлення, '
                           'чий трек-номер (у замовленні чи його відправленні) є в листі; на адресу клієнта із '
                           'замовлення, тому самому клієнту про те саме замовлення — не частіше ніж раз на 14 днів.'}),
        ('📬 Листи агента: важливо, запит на прочитання, нагадування', {
            'fields': ('opt_important', 'opt_receipt', 'remind_days', 'remind_until'),
            'description': 'Для відповідей, листів клієнту замовлення й пересилань — як галочки в «Написати». '
                           'Чернетка відкривається з ними вже вибраними; «Надсилати самому» — запланований лист з ними. '
                           'Немає реакції — лист і Telegram вам: потрібне ручне втручання.'}),
        ('❗ Позначати такі листи', {
            'fields': ('importance', 'notify'),
            'description': 'Лише для «❗ Позначати такі листи важливими»: позначка в списку листів і в папці '
                           '«⭐ Важливі», з «🔔» — ще й лист і Telegram вам, щойно такий лист прийде. Без AI. '
                           '«Що робити» — необов’язкова примітка в повідомленні.'}),
        ('🧩 Завдання агенту', {
            'fields': ('auto_apply', 'task_model', 'task_budget_usd'),
            'description': 'Лише для «Завдання агенту з кожного такого листа»: з кожного нового такого листа — '
                           'завдання агенту (як «🧩 Завдання агенту з цього листа»). Пропозиції — на сторінці '
                           'завдання, коли закінчить — лист і Telegram.'}),
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
        if obj.kind == AgentRule.MAIL_TASK:
            return '🧩 завдання' + (' · ✓ трек-номер сам' if obj.auto_apply else '')
        mode = '✉️ сам' if obj.mode == AgentRule.SEND else '📝 чернетка'
        mode += (' · ❗' if obj.opt_important else '') + (' · 📬' if obj.opt_receipt else '') + (
            f' · ⏰ {obj.remind_days} дн.' if obj.remind_days else '')
        if obj.kind == AgentRule.MAIL_FORWARD:
            return f'📤 переслати · {mode}'
        if obj.kind == AgentRule.MAIL_MARK:
            return obj.get_importance_display() + (' · 🔔' if obj.notify else '')
        return f'📨 клієнту · {mode}' if obj.kind == AgentRule.MAIL_CUSTOMER else mode

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
            '🧩': runs.filter(status=AgentRun.TASKED).count(),
            '❗': runs.filter(status=AgentRun.MARKED).count(),
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
        if obj.kind == AgentRule.MAIL_MARK:
            return format_html('<div style="opacity:.8">{} — кожен новий такий лист{}.</div>',
                               obj.get_importance_display(), ' · 🔔 одразу лист і Telegram вам' if obj.notify else '')
        if obj.kind == AgentRule.MAIL_TASK:
            return format_html('<div style="opacity:.8">🧩 З кожного нового такого листа — завдання агенту{}.</div>',
                               ' · трек-номер у порожнє поле замовлення вносить сам' if obj.auto_apply else '')
        if agent_mail.promote_ready(obj):
            return format_html(
                '<div style="padding:8px 12px;border-radius:6px;background:rgba(46,125,50,.14);'
                'border:1px solid rgba(46,125,50,.4)">✅ {} чернеток поспіль ви надіслали без змін. Можна дозволити '
                'агенту надсилати самому: «Режим» → «Надсилати самому» нижче й «Зберегти». Лист стане в «⏳ '
                'Заплановані» на {} хв — до відправки можна скасувати.</div>', obj.streak, obj.send_delay_minutes)
        return format_html('<div style="opacity:.8">{} · без змін поспіль: {} (після {} — пропозиція надсилати '
                           'самому)</div>', obj.get_mode_display(), obj.streak, agent_mail.PROMOTE_AFTER)

    @admin.display(description='Перевірка')
    def test_panel(self, obj):
        """🧪 Що робитиме доручення, які листи під нього підходять і що агент зробив би з листом (agent/rule_check.py)."""
        if obj is None or not obj.pk:
            return 'Спершу збережіть доручення — тоді тут можна перевірити його на листах скриньки.'
        from agent import rule_check
        plan = format_html_join('', '<li>{}</li>', ((ln,) for ln in rule_check.plan(obj)))
        data = rule_check.candidates(obj)
        ai = obj.kind in rule_check.AI_KINDS
        label = '🧪 Що зробив би агент' + (' (AI)' if ai else '')
        tip = ('Агент напише відповідь на цей лист, як «🧪 Спробувати» у пошті (виклик AI); нічого не зберігається й не '
               'надсилається' if ai else 'Нічого не зберігається й не надсилається')
        rows = format_html_join('', (
            '<tr><td style="white-space:nowrap">{}</td><td>{}</td><td style="opacity:.8">{}</td><td>{}</td>'
            '<td><button type="button" class="button" data-art="{}" title="{}">{}</button></td></tr>'
            '<tr id="art-res-{}" hidden><td colspan="5"><pre class="art-pre"></pre></td></tr>'), (
            (timezone.localtime(f['msg'].sent_at).strftime('%d.%m %H:%M') if f['msg'].sent_at else '',
             _mail_link(f['msg']), f['why'],
             (f'{STATUS_ICONS.get(f["run"].status, "")} {f["run"].get_status_display()}' if f['run']
              else ('⚠️ ' + f['blocked'] if f['blocked'] else '')),
             f['msg'].pk, tip, label, f['msg'].pk) for f in data['found']))
        if data['found']:
            table = format_html('<table style="width:100%;margin-top:4px"><tbody>{}</tbody></table>', rows)
        else:
            table = format_html('<div style="margin-top:4px">Жоден лист за {} днів не підходить під умови — '
                                'перевірте «Від кого», «Тема містить», «Текст листа містить» і категорію.</div>',
                                data['days'])
        near = ''
        if data['near']:
            near = format_html(
                '<div style="margin-top:8px;opacity:.85">Від того самого відправника, але не підійшли:<ul>{}</ul></div>',
                format_html_join('', '<li>{} — {}</li>', ((_mail_link(n['msg']), n['why']) for n in data['near'])))
        return format_html(
            '<div class="art"><b>Що робитиме доручення з кожним таким листом</b><ul style="margin:4px 0 8px">{}</ul>'
            '<div style="opacity:.8">Перевіряється збережене доручення — після змін спершу «Зберегти і продовжити '
            'редагування». Кнопка нижче нічого не зберігає й не надсилає.</div>'
            '<div style="margin-top:10px"><b>Листи за {} днів, що підходять: {}</b> '
            '<span style="opacity:.7">(переглянуто {} листів Вхідних)</span></div>{}{}</div>'
            '<style>.art-pre{{white-space:pre-wrap;margin:4px 0;padding:8px 10px;border-radius:6px;'
            'background:var(--darkened-bg,rgba(127,127,127,.12));font:inherit;max-height:420px;overflow:auto}}</style>'
            '<div id="art-labels" data-l="{}" hidden></div><script>{}</script>',
            plan, data['days'], len(data['found']) if len(data['found']) < rule_check.SHOW
            else f'{rule_check.SHOW}+', data['scanned'], table, near, json.dumps(_TEST_LABELS, ensure_ascii=False),
            mark_safe(_TEST_JS % {'pk': obj.pk}))

    def get_urls(self):
        from django.urls import path
        own = [path('<int:pk>/test/<int:msg_pk>/', self.admin_site.admin_view(self.test_view),
                    name='agent_agentrule_test')]
        return own + super().get_urls()

    def test_view(self, request, pk, msg_pk):
        """POST: що агент зробив би з цим листом (agent/rule_check.one) — JSON; нічого не зберігає й не надсилає."""
        from django.http import JsonResponse
        from email_assistant.models import EmailMessage
        from agent import rule_check
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'Лише POST'}, status=405)
        rule = self.get_queryset(request).filter(pk=pk).first()
        if rule is None:
            return JsonResponse({'ok': False, 'error': 'Доручення не знайдено'}, status=404)
        if not self.has_change_permission(request, rule):
            return JsonResponse({'ok': False, 'error': 'Немає права змінювати доручення'}, status=403)
        msg = EmailMessage.objects.filter(pk=msg_pk, account_id=rule.account_id).select_related('account').first()
        if msg is None:
            return JsonResponse({'ok': False, 'error': 'Лист не знайдено в скриньці доручення'}, status=404)
        try:
            out = rule_check.one(rule, msg)
        except Exception as e:  # noqa: BLE001 — показати, а не 500
            return JsonResponse({'ok': False, 'error': f'{type(e).__name__}: {e}'[:300]})
        return JsonResponse({'ok': True, 'kind': rule.kind, **out})

    @admin.display(description='Створено з листа')
    def example_link(self, obj):
        return _mail_link(obj.example_message) if obj.example_message_id else '—'

    @admin.display(description='Листи')
    def recent_runs(self, obj):
        if obj is None or not obj.pk:
            return '—'
        runs = obj.runs.select_related('message', 'task').order_by('-created_at')[:15]
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


# ── ⏰ Регулярні завдання агенту ──────────────────────────────────────────────

class AgentScheduleForm(forms.ModelForm):
    days = forms.TypedMultipleChoiceField(
        label='Дні тижня', choices=AgentSchedule.WEEKDAYS, coerce=int, required=False,
        widget=forms.CheckboxSelectMultiple, help_text='Нічого не вибрано — щодня.')

    class Meta:
        model = AgentSchedule
        fields = ['name', 'enabled', 'instruction', 'time', 'days', 'model', 'budget_usd', 'notify_email',
                  'notify_telegram']
        widgets = {'instruction': forms.Textarea(attrs={'rows': 7})}

    def clean_days(self):
        return sorted(set(self.cleaned_data.get('days') or []))


@admin.register(AgentSchedule)
class AgentScheduleAdmin(admin.ModelAdmin):
    form = AgentScheduleForm
    list_display = ('name', 'when_col', 'enabled', 'last_col', 'next_col')
    list_filter = ('enabled',)
    readonly_fields = ('state_panel',)
    DESCRIPTION = ('Агент виконує завдання за розкладом і надсилає вам звіт — у Telegram і на пошту. Пишіть своїми '
                   'словами, як завдання агенту. Агент лише читає й звітує (зміни — лише якщо попросите, і тоді — '
                   'пропозиції, які ви підтверджуєте ✓).')

    def get_fieldsets(self, request, obj=None):
        presets = format_html_join(' · ', '<a href="?preset={}">{}</a>',
                                   ((key, p['name']) for key, p in schedules_presets().items()))
        top = [(None, {'fields': ('name', 'enabled', 'instruction'),
                       'description': format_html('{}<br><br>Готові приклади: {}', self.DESCRIPTION, presets)})]
        if obj is not None and obj.pk:
            top.insert(0, ('🧪 Перевірити, як працює', {'fields': ('state_panel',)}))
        return top + [
            ('⏰ Коли', {'fields': ('time', 'days')}),
            ('Звіт і витрати', {'fields': ('notify_telegram', 'notify_email', 'model', 'budget_usd')}),
        ]

    def get_changeform_initial_data(self, request):
        data = super().get_changeform_initial_data(request)
        preset = schedules_presets().get(request.GET.get('preset', ''))
        if preset:
            data.update(name=preset['name'], instruction=preset['instruction'])
        data.setdefault('days', [0, 1, 2, 3, 4])
        return data

    def get_queryset(self, request):
        return super().get_queryset(request).filter(owner=request.user)

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.owner_id != request.user.pk:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.owner_id != request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.owner = request.user
        super().save_model(request, obj, form, change)

    def get_urls(self):
        from django.urls import path
        return [path('<int:pk>/run/', self.admin_site.admin_view(self.run_view),
                     name='agent_agentschedule_run')] + super().get_urls()

    def run_view(self, request, pk):
        """«▶ Запустити зараз» (POST): нове завдання у фоні, звіт — як за розкладом."""
        from agent import schedules
        obj = self.get_queryset(request).filter(pk=pk).first()
        if obj is None:
            raise Http404
        if request.method != 'POST' or not request.user.has_perm('agent.add_agenttask'):
            messages.error(request, 'Немає права давати завдання агенту')
            return redirect(reverse('admin:agent_agentschedule_change', args=[obj.pk]))
        quiet = request.GET.get('quiet') == '1'      # 🧪 «Спробувати без звіту»
        task = schedules.start(obj, manual=True, quiet=quiet)
        if task is None:
            messages.error(request, 'Не вдалося запустити: ' + (schedules.allowed(obj) or 'спробуйте ще раз'))
            return redirect(reverse('admin:agent_agentschedule_change', args=[obj.pk]))
        messages.info(request, '🧪 Пробний запуск — результат з’явиться на цій сторінці, звіт нікуди не надсилається'
                      if quiet else '▶ Агент працює — звіт прийде, коли закінчить; хід — на цій сторінці')
        return redirect(reverse('admin:agent_agenttask_review', args=[task.pk]))

    @admin.display(description='Коли')
    def when_col(self, obj):
        from agent import schedules
        return f'{schedules.days_text(obj)} о {obj.time:%H:%M}'

    @admin.display(description='Останній запуск')
    def last_col(self, obj):
        task = obj.tasks.order_by('-created_at').first()
        if task is None:
            return '—'
        return format_html('<a href="{}">{} · {}</a>', reverse('admin:agent_agenttask_review', args=[task.pk]),
                           timezone.localtime(task.created_at).strftime('%d.%m %H:%M'), task.get_status_display())

    @admin.display(description='Наступний')
    def next_col(self, obj):
        from agent import schedules
        at = schedules.next_run(obj)
        return timezone.localtime(at).strftime('%d.%m %H:%M') if at else '—'

    @admin.display(description='Перевірка')
    def state_panel(self, obj):
        """🧪 Як виконуватиметься (agent/schedules.preview) + «Спробувати без звіту» / «Запустити зараз» + запуски."""
        from agent import schedules
        info = schedules.preview(obj)

        def checks(rows):
            return format_html_join('', '<li>{} {}</li>', (('✓' if ok else '⚠️', text) for ok, text in rows))
        runs = format_html_join('', '<li><a href="{}">{}</a> — {}{}</li>', (
            (reverse('admin:agent_agenttask_review', args=[t.pk]),
             timezone.localtime(t.created_at).strftime('%d.%m %H:%M'), t.get_status_display(),
             ' · 🧪 пробний, без звіту' if t.quiet else '')
            for t in obj.tasks.order_by('-created_at')[:7]))
        blocked = format_html('<div style="margin-top:6px">⚠️ Не запускається: {}</div>', info['blocked']) \
            if info['blocked'] else ''
        run_url = reverse('admin:agent_agentschedule_run', args=[obj.pk])
        return format_html(
            '<div><b>⏰ Коли:</b> {}</div>{}'
            '<div style="margin-top:8px"><b>👀 Що агент бачитиме</b><ul style="margin:4px 0">{}</ul></div>'
            '<div><b>📨 Куди піде звіт</b><ul style="margin:4px 0">{}</ul></div>'
            '<div style="opacity:.8">🤖 {}. Перевіряються збережені налаштування — після змін спершу «Зберегти і '
            'продовжити редагування».</div>'
            '<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">'
            '<button type="submit" class="button" formaction="{}?quiet=1" formmethod="post" formnovalidate '
            'title="Агент виконає завдання зараз; результат — на сторінці запуску, звіт нікуди не надсилається">'
            '🧪 Спробувати без звіту</button>'
            '<button type="submit" class="button" formaction="{}" formmethod="post" formnovalidate '
            'onclick="return confirm(\'Запустити зараз? Агент виконає завдання й надішле звіт.\')">'
            '▶ Запустити зараз (зі звітом)</button></div>'
            '<div style="margin-top:8px">Останні запуски:<ul style="margin:4px 0">{}</ul></div>',
            ' · '.join(info['when']), blocked, checks(info['see']), checks(info['report']), info['cost'],
            run_url, run_url, runs or format_html('<li>ще не було</li>'))

def schedules_presets():
    from agent.schedules import PRESETS
    return PRESETS


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


# ── 🤖 Завдання агенту ───────────────────────────────────────────────────────

class AgentTaskForm(forms.ModelForm):
    class Meta:
        model = AgentTask
        fields = ['instruction', 'mail_to', 'mail_cc', 'message', 'products', 'each_product', 'web', 'model',
                  'budget_usd']
        widgets = {'instruction': forms.Textarea(attrs={
            'rows': 6, 'placeholder': 'Напр.: Знайди в даташитах вагу однієї штуки й країну виробника і внеси. '
                                      'Або: Знайди даташити цих антен і надішли їх на anna@kunde.de з коротким листом.'}),
            'mail_to': forms.Textarea(attrs={'rows': 1, 'placeholder': 'anna@kunde.de'}),
            'mail_cc': forms.Textarea(attrs={'rows': 1, 'placeholder': 'sergey@sevskiy.de'})}

    def clean(self):
        data = super().clean()
        if data.get('each_product') and not data.get('products'):
            raise forms.ValidationError('«Окремо для кожного товару» — виберіть товари.')
        from agent.mail_edit import bad_cc
        for field in ('mail_to', 'mail_cc'):
            bad = bad_cc(data.get(field) or '')
            if bad:
                self.add_error(field, 'Не схоже на адресу: ' + ', '.join(bad[:3]))
        return data


def mail_langs():
    from agent.mail_edit import LANG_CHOICES
    return LANG_CHOICES


@admin.register(AgentTask)
class AgentTaskAdmin(admin.ModelAdmin):
    form = AgentTaskForm
    list_display = ('created_at', 'title', 'status_col', 'products_col', 'proposals_col', 'cost_col')
    list_filter = ('status',)
    search_fields = ('title', 'instruction')
    autocomplete_fields = ('products',)
    DESCRIPTION = ('Напишіть завдання своїми словами. Агент читає картки товарів, їхні даташити, лист завдання з '
                   'вкладеннями (і за бажанням — сайти виробників), а потім ПРОПОНУЄ: зміни в картках, прихід на '
                   'склад, нові товари, події в календарі, лист. Ви бачите «було → стане» з цитатою з джерела й '
                   'підтверджуєте ✓. Лист агент лише готує — і лише на адреси, які ви написали тут.')

    def get_fieldsets(self, request, obj=None):
        mail = self._mail_from_get(request)
        note = format_html('<br><br>✉️ <b>Завдання з листа</b> «<span translate="no">{}</span>» від '
                           '<span translate="no">{}</span> — агент прочитає його й вкладення.',
                           mail.subject or '(без теми)', mail.from_email) if mail else ''
        return ((None, {'fields': ('instruction', 'mail_to', 'mail_cc', 'message', 'products', 'each_product', 'web',
                                   'model', 'budget_usd'),
                        'description': format_html('{}{}', self.DESCRIPTION, note)}),)

    def _mail_from_get(self, request):
        from email_assistant.models import EmailMessage
        pk = request.GET.get('msg', '')
        return (EmailMessage.objects.filter(pk=pk, account__user=request.user).first()
                if pk.isdigit() else None)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'message':                     # лише свої листи; поле приховане (з «?msg=»)
            from email_assistant.models import EmailMessage
            kwargs['queryset'] = EmailMessage.objects.filter(account__user=request.user)
            kwargs['widget'] = forms.HiddenInput
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(owner=request.user)

    def get_changeform_initial_data(self, request):
        data = super().get_changeform_initial_data(request)
        ids = [int(x) for x in (request.GET.get('ids') or '').split(',') if x.strip().isdigit()][:1000]
        if ids:
            data['products'] = ids
            data['each_product'] = len(ids) > 5
        mail = self._mail_from_get(request)
        data['message'] = mail.pk if mail else None
        if data.get('instruction'):              # ?instruction= — з «🧩 Підготувати завдання агенту» (agent/suggest.py)
            data['instruction'] = str(data['instruction'])[:3000]
        numbers = self._orders_from_get(request)
        if numbers:
            data['instruction'] = 'Замовлення: ' + ', '.join(numbers) + '\n' + str(data.get('instruction') or '')
        return data

    def _orders_from_get(self, request) -> list:
        """«🤖 Завдання агенту для вибраних…» у списку замовлень → номери в тексті завдання (якщо людина їх бачить)."""
        ids = [int(x) for x in (request.GET.get('orders') or '').split(',') if x.strip().isdigit()][:200]
        if not ids or not request.user.has_perm('sales.view_salesorder'):
            return []
        from sales.models import SalesOrder
        return list(SalesOrder.objects.filter(pk__in=ids).order_by('pk').values_list('order_number', flat=True))

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.owner_id != request.user.pk:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.owner_id != request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.owner = request.user
        first = next((ln.strip() for ln in obj.instruction.splitlines() if ln.strip()), 'Завдання')
        obj.title = first[:200]
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change:                                  # товари вже збережено (M2M) — можна починати
            from agent import tasks
            tasks.start(form.instance)

    def response_add(self, request, obj, post_url_continue=None):
        return redirect(reverse('admin:agent_agenttask_review', args=[obj.pk]))

    def change_view(self, request, object_id, form_url='', extra_context=None):
        return redirect(reverse('admin:agent_agenttask_review', args=[object_id]))

    def get_urls(self):
        from django.urls import path
        own = [
            path('<int:pk>/review/', self.admin_site.admin_view(self.review_view), name='agent_agenttask_review'),
            path('<int:pk>/status/', self.admin_site.admin_view(self.status_view), name='agent_agenttask_status'),
        ]
        return own + super().get_urls()

    def _task(self, request, pk):
        task = AgentTask.objects.filter(pk=pk, owner=request.user).first()
        if task is None:
            raise Http404
        return task

    def status_view(self, request, pk):
        task = self._task(request, pk)
        return JsonResponse({
            'status': task.status, 'label': task.get_status_display(),
            'running': task.status in (AgentTask.QUEUED, AgentTask.RUNNING),
            'done': len(task.done_products or []), 'total': task.products.count(),
            'cost': f'{task.cost_usd:.2f}', 'pending': task.proposals.filter(status=AgentProposal.PENDING).count(),
            'log': [e.get('text', '') for e in (task.log or [])[-6:]],
        })

    def review_view(self, request, pk):
        task = self._task(request, pk)
        if request.method == 'POST':
            self._decide(request, task)
            return redirect(reverse('admin:agent_agenttask_review', args=[task.pk]))
        from agent.decide import describe
        rows = []
        for p in task.proposals.exclude(kind=AgentProposal.MAIL).select_related('product'):
            p.label, p.old_display, p.new_display = describe(p)
            d = p.data or {}
            p.sku_text = p.product.sku if p.product_id else (d.get('number') or d.get('sku', ''))
            p.sku_url = '' if p.product_id else d.get('order_url', '')
            p.source_url = p.source if p.source.startswith(('http://', 'https://')) else ''
            rows.append(p)
        context = {
            **self.admin_site.each_context(request),
            'title': task.title, 'task': task, 'rows': rows,
            'mails': list(task.proposals.filter(kind=AgentProposal.MAIL)),
            'mail_langs': mail_langs(),
            'pending': sum(1 for p in rows if p.status == AgentProposal.PENDING),
            'running': task.status in (AgentTask.QUEUED, AgentTask.RUNNING),
            'stopped': task.status in (AgentTask.FAILED, AgentTask.CANCELLED),
            'total': task.products.count(), 'done': len(task.done_products or []),
            'can_apply': any(request.user.has_perm(x) for x in APPLY_PERMS),
            'log': list(reversed((task.log or [])[-80:])),
            'back_url': reverse('admin:agent_agenttask_changelist'),
        }
        return TemplateResponse(request, 'admin/agent/agenttask/review.html', context)

    def _decide(self, request, task):
        """POST сторінки перевірки: apply / reject (ids), mail_draft / mail_send (pid + правки), cancel, resume."""
        from agent import decide, tasks
        action = request.POST.get('action', '')
        ids = [int(x) for x in request.POST.getlist('ids') if x.isdigit()]
        if action == 'apply':
            out = decide.apply_selected(task, ids, request.user, request=request)
            if out['applied']:
                messages.success(request, f'✓ Застосовано: {out["applied"]}')
            for line in out['failed'][:10]:
                messages.error(request, f'⚠️ {line}')
        elif action == 'reject':
            messages.info(request, f'✕ Відхилено: {decide.reject_selected(task, ids, request.user)}')
        elif action == 'mail_rewrite':   # ✍️ «🔄 Перегенерувати лист» (agent/mail_edit.py)
            from agent import mail_edit
            p = task.proposals.filter(pk=request.POST.get('pid') or 0, kind=AgentProposal.MAIL,
                                      status=AgentProposal.PENDING).first()
            error = 'лист уже оброблено' if p is None else mail_edit.rewrite(
                p, request.user, change=request.POST.get('change', ''), lang=request.POST.get('lang', ''),
                subject=request.POST.get('subject', ''), body=request.POST.get('body', ''),
                cc=request.POST.get('cc', ''))
            if error:
                messages.error(request, f'⚠️ {error}')
            else:
                messages.success(request, '🔄 Лист переписано — перевірте й надішліть')
        elif action in ('mail_draft', 'mail_send'):
            p = task.proposals.filter(pk=request.POST.get('pid') or 0, kind=AgentProposal.MAIL,
                                      status=AgentProposal.PENDING).first()
            error = 'лист уже оброблено' if p is None else decide.mail(
                p, request.user, send=action == 'mail_send', to=request.POST.get('to', ''),
                subject=request.POST.get('subject', ''), body=request.POST.get('body', ''),
                cc=request.POST.get('cc', ''))
            if error:
                messages.error(request, f'⚠️ {error}')
            elif action == 'mail_draft':
                messages.success(request, '📝 Чернетка листа готова — відкрийте її нижче')
            else:
                messages.success(request, '✉️ Лист у «⏳ Запланованих» — піде протягом 2 хв (ще можна скасувати)')
        elif action == 'cancel':
            AgentTask.objects.filter(pk=task.pk, status__in=(AgentTask.QUEUED, AgentTask.RUNNING)).update(
                status=AgentTask.CANCELLED)
            messages.info(request, '⏹ Агента зупинено — знайдене лишається')
        elif action == 'resume':
            try:
                budget = Decimal((request.POST.get('budget') or '').replace(',', '.'))
                if budget > 0:
                    AgentTask.objects.filter(pk=task.pk).update(budget_usd=min(budget, Decimal('500')))
            except (InvalidOperation, ValueError):
                pass
            task.refresh_from_db()
            if tasks.resume(task):
                messages.info(request, '▶ Агент продовжує')

    @admin.display(description='Стан', ordering='status')
    def status_col(self, obj):
        return obj.get_status_display()

    @admin.display(description='Товарів')
    def products_col(self, obj):
        total = obj.products.count()
        return f'{len(obj.done_products or [])} / {total}' if obj.each_product else (total or 'сам шукає')

    @admin.display(description='Пропозицій')
    def proposals_col(self, obj):
        props = obj.proposals.all()
        pending = props.filter(status=AgentProposal.PENDING).count()
        applied = props.filter(status=AgentProposal.APPLIED).count()
        return f'👀 {pending} · ✓ {applied}' if pending or applied else '—'

    @admin.display(description='Витрачено')
    def cost_col(self, obj):
        return f'{obj.cost_usd:.2f} $'
