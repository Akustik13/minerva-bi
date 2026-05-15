import json
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as _DjangoUserAdmin
from django.contrib.auth.models import User
from django.urls import reverse, NoReverseMatch
from django.utils.html import format_html, mark_safe

from .models import AuditLog, UserProfile, ModuleBundle, ModuleRegistry, TenantAccount


# ── Invitation System helpers ─────────────────────────────────────────────────

_ROLE_CAPS = {
    'superadmin': {
        'label': 'Суперадміністратор', 'color': '#f85149',
        'desc': 'Повний доступ до всіх функцій системи.',
        'caps': ['Управління всіма модулями', 'Налаштування системи',
                 'Управління користувачами та правами',
                 'Журнал аудиту', 'Імпорт, експорт та видалення'],
    },
    'admin': {
        'label': 'Адміністратор', 'color': '#ff9800',
        'desc': 'Адміністративний доступ до всіх модулів.',
        'caps': ['Продажі та клієнти', 'Склад та закупівлі',
                 'Фінансова аналітика та звіти', 'Інтеграції', 'Імпорт та експорт'],
    },
    'manager': {
        'label': 'Менеджер', 'color': '#58a6ff',
        'desc': 'Доступ до CRM, продажів, відправлень та завдань.',
        'caps': ['CRM та клієнти (RFM, стратегії)', 'Продажі та замовлення',
                 'Відправлення та доставка', 'Email-асистент', 'Завдання та дедлайни'],
    },
    'warehouse': {
        'label': 'Складський працівник', 'color': '#3fb950',
        'desc': 'Доступ до складських операцій та відправлень.',
        'caps': ['Складські транзакції та інвентаризація',
                 'Прийом товарів (закупівлі)', 'Відправлення та трекінг', 'Друк етикеток DYMO'],
    },
    'accountant': {
        'label': 'Бухгалтер', 'color': '#c9a84c',
        'desc': 'Доступ до продажів, бухгалтерії та складу.',
        'caps': ['Рахунки-фактури та рахунки', 'Бухгалтерський облік',
                 'Склад (тільки перегляд)', 'Аналітика та звіти', 'Експорт даних'],
    },
    'ai': {
        'label': 'AI-фахівець', 'color': '#a78bfa',
        'desc': 'Доступ до CRM та AI-функцій.',
        'caps': ['CRM та клієнти', 'AI-аналіз клієнтів', 'Стратегії утримання'],
    },
    'readonly': {
        'label': 'Тільки перегляд', 'color': '#607d8b',
        'desc': 'Тільки перегляд дашборду та довідника.',
        'caps': ['Аналітика (тільки перегляд)', 'FAQ та підтримка'],
    },
}


def _build_invitation_html(user, password, company, system_url):
    """Minerva-branded invitation email: owl + rotating rings, Cinzel typography, gold palette."""
    import html as _h
    import os
    from django.conf import settings as _dj_settings

    try:
        role = user.profile.role
    except Exception:
        role = 'readonly'
    ri    = _ROLE_CAPS.get(role, _ROLE_CAPS['readonly'])
    first = _h.escape(user.first_name or user.username)
    co    = _h.escape(company)
    c     = ri['color']
    uname = _h.escape(user.username)
    pwd   = _h.escape(password)
    surl  = _h.escape(system_url)
    label = _h.escape(ri['label'])
    desc  = _h.escape(ri['desc'])
    caps  = ''.join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid rgba(201,168,76,0.1)">'
        f'<span style="color:#c9a84c;margin-right:10px">&#10003;</span>'
        f'<span style="font-family:Crimson Pro,Georgia,serif;color:rgba(245,237,209,0.65);font-size:16px">{_h.escape(cap)}</span>'
        f'</td></tr>'
        for cap in ri['caps']
    )

    # Load real owl SVG from includes/owl.html
    owl = ''
    try:
        owl_path = os.path.join(_dj_settings.BASE_DIR, 'templates', 'includes', 'owl.html')
        with open(owl_path, 'r', encoding='utf-8') as _f:
            owl = _f.read().strip()
        # Replace the CSS class with inline size — animation handled by .mv-float wrapper
        owl = owl.replace(
            'class="owl-svg"',
            'width="120" height="120" style="display:block"',
        )
    except Exception:
        # Fallback minimal owl if file not found
        owl = (
            '<svg viewBox="0 0 82 113" xmlns="http://www.w3.org/2000/svg"'
            ' width="120" height="120" style="display:block">'
            '<circle cx="41" cy="40" r="30" fill="none" stroke="#c9a84c" stroke-width="2"/>'
            '</svg>'
        )

    return f"""<!DOCTYPE html>
<html lang="uk"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Minerva BI &mdash; Запрошення</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cinzel+Decorative:wght@400;700;900&family=Cinzel:wght@400;600;700;900&family=Crimson+Pro:ital,wght@0,300;0,400;0,600;1,300;1,400&display=swap" rel="stylesheet">
<style>
@keyframes rotateSlow{{to{{transform:rotate(360deg);}}}}
@keyframes rotateSlowR{{to{{transform:rotate(-360deg);}}}}
@keyframes float{{0%,100%{{transform:translateY(0) rotate(-.5deg);}}50%{{transform:translateY(-10px) rotate(.5deg);}}}}
@keyframes shimmer{{0%,100%{{opacity:.4;}}50%{{opacity:1;}}}}
.mv-float{{animation:float 6s ease-in-out infinite;}}
.mv-r1{{animation:rotateSlow 20s linear infinite;}}
.mv-r2{{animation:rotateSlowR 35s linear infinite;}}
.mv-shim{{animation:shimmer 4s ease-in-out infinite;}}
</style>
</head>
<body style="margin:0;padding:0;background:#080b0f;font-family:'Crimson Pro',Georgia,serif;color:#f5edd1">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">

<!-- HEADER -->
<div style="position:relative;background:#080b0f;border-radius:16px 16px 0 0;overflow:hidden;padding:52px 40px 38px;text-align:center;border-bottom:1px solid rgba(201,168,76,0.15)">
  <div style="position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse 80% 60% at 50% 0%,rgba(201,168,76,0.12) 0%,transparent 70%);pointer-events:none"></div>
  <div class="mv-shim" style="position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,#c9a84c,transparent)"></div>

  <div style="position:relative;display:inline-block;margin-bottom:26px">
    <div class="mv-r2" style="position:absolute;top:-22px;left:-22px;right:-22px;bottom:-22px;border:1px solid rgba(201,168,76,0.15);border-radius:50%"></div>
    <div class="mv-r1" style="position:absolute;top:-11px;left:-11px;right:-11px;bottom:-11px;border:1px solid rgba(201,168,76,0.25);border-radius:50%">
      <span style="position:absolute;top:-8px;left:50%;transform:translateX(-50%);font-size:12px;color:#c9a84c">&#10022;</span>
    </div>
    <div class="mv-float" style="filter:drop-shadow(0 0 40px rgba(201,168,76,0.5)) drop-shadow(0 0 80px rgba(201,168,76,0.2)) drop-shadow(0 4px 20px rgba(0,0,0,0.6))">
      {owl}
    </div>
  </div>

  <div style="font-family:'Cinzel',Georgia,serif;font-size:38px;font-weight:900;letter-spacing:4px;background:linear-gradient(135deg,#f5edd1,#e8d5a3,#c9a84c);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;filter:drop-shadow(0 0 30px rgba(201,168,76,0.4));margin-bottom:7px">MINERVA</div>
  <div style="font-family:'Cinzel',Georgia,serif;font-size:12px;letter-spacing:5px;text-transform:uppercase;color:rgba(201,168,76,0.75);margin-bottom:22px">SAPIENTIA &middot; VISIO &middot; IMPERIUM</div>

  <div style="display:flex;align-items:center;gap:14px;max-width:220px;margin:0 auto">
    <div style="flex:1;height:1px;background:linear-gradient(to right,transparent,rgba(201,168,76,0.22))"></div>
    <span style="color:rgba(201,168,76,0.5);font-size:14px">&#10022;</span>
    <div style="flex:1;height:1px;background:linear-gradient(to left,transparent,rgba(201,168,76,0.22))"></div>
  </div>
</div>

<!-- BODY -->
<div style="background:#0d1117;padding:44px 40px;border:1px solid rgba(201,168,76,0.15);border-top:none">

  <div style="text-align:center;margin-bottom:36px">
    <div style="font-family:'Cinzel',Georgia,serif;font-size:12px;font-weight:600;letter-spacing:5px;text-transform:uppercase;color:#e8d5a3;margin-bottom:14px;text-shadow:0 0 15px rgba(201,168,76,0.6),0 2px 4px rgba(0,0,0,0.8)">&#10022; ДОСТУП НАДАНО &#10022;</div>
    <h1 style="margin:0 0 14px;font-size:24px;font-weight:700;line-height:1.3;font-family:'Cinzel',Georgia,serif;background:linear-gradient(135deg,#f5edd1,#e8d5a3,#c9a84c);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">Ласкаво просимо, {first}</h1>
    <p style="margin:0;font-family:'Crimson Pro',Georgia,serif;font-size:18px;font-style:italic;color:rgba(245,237,209,0.65);line-height:1.7;letter-spacing:0.2px">
      <em style="color:#e8d5a3;font-style:normal;font-weight:600">{co}</em> надає вам доступ до Minerva BI &mdash;<br>
      системи управління бізнесом нового покоління.
    </p>
  </div>

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:28px">
    <div style="flex:1;height:1px;background:rgba(201,168,76,0.15)"></div>
    <span style="color:rgba(201,168,76,0.5);font-size:14px">&#10022;</span>
    <div style="flex:1;height:1px;background:rgba(201,168,76,0.15)"></div>
  </div>

  <div style="background:#080b0f;border:1px solid rgba(201,168,76,0.22);border-radius:4px;padding:24px;margin-bottom:28px">
    <div style="font-family:'Cinzel',Georgia,serif;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:rgba(201,168,76,0.5);margin-bottom:18px">&#128273; Ваші облікові дані</div>
    <table style="width:100%;border-collapse:collapse">
      <tr>
        <td style="padding:11px 0;border-bottom:1px solid rgba(201,168,76,0.1);width:90px;font-family:'Cinzel',Georgia,serif;font-size:10px;letter-spacing:1px;color:rgba(201,168,76,0.5);vertical-align:middle">ЛОГІН</td>
        <td style="padding:11px 0;border-bottom:1px solid rgba(201,168,76,0.1)">
          <code style="font-size:17px;font-weight:700;color:#58a6ff;background:rgba(88,166,255,0.08);padding:5px 14px;border-radius:4px;border:1px solid rgba(88,166,255,0.18)">{uname}</code>
        </td>
      </tr>
      <tr>
        <td style="padding:11px 0;font-family:'Cinzel',Georgia,serif;font-size:10px;letter-spacing:1px;color:rgba(201,168,76,0.5);vertical-align:middle">ПАРОЛЬ</td>
        <td style="padding:11px 0">
          <code style="font-size:17px;font-weight:700;color:#c9a84c;background:rgba(201,168,76,0.08);padding:5px 14px;border-radius:4px;border:1px solid rgba(201,168,76,0.22);letter-spacing:2px">{pwd}</code>
        </td>
      </tr>
    </table>
  </div>

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:28px">
    <div style="flex:1;height:1px;background:rgba(201,168,76,0.15)"></div>
    <span style="color:rgba(201,168,76,0.5);font-size:14px">&#10022;</span>
    <div style="flex:1;height:1px;background:rgba(201,168,76,0.15)"></div>
  </div>

  <div style="text-align:center;margin-bottom:24px">
    <div style="display:inline-block;background:{c}22;color:{c};border:1px solid {c}44;padding:8px 22px;border-radius:4px;font-family:'Cinzel',Georgia,serif;font-size:12px;font-weight:700;letter-spacing:2px;margin-bottom:12px">{label}</div>
    <div style="font-family:'Crimson Pro',Georgia,serif;font-size:16px;font-style:italic;color:rgba(245,237,209,0.65);line-height:1.6">{desc}</div>
  </div>

  <div style="background:rgba(201,168,76,0.04);border:1px solid rgba(201,168,76,0.15);border-radius:4px;padding:20px;margin-bottom:36px">
    <div style="font-family:'Cinzel',Georgia,serif;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:rgba(201,168,76,0.5);margin-bottom:14px">Ваші можливості в системі</div>
    <table style="width:100%;border-collapse:collapse">{caps}</table>
  </div>

  <div style="text-align:center;margin-bottom:24px">
    <a href="{surl}" style="display:inline-block;padding:16px 56px;border-radius:4px;background:linear-gradient(90deg,#7a5810 0%,#a8792a 25%,#c9a84c 55%,#d4a83a 80%,#e8c85a 100%);color:#080b0f;text-decoration:none;font-family:'Cinzel Decorative','Cinzel',Georgia,serif;font-size:13px;font-weight:700;letter-spacing:4px;text-transform:uppercase;box-shadow:0 3px 14px rgba(0,0,0,0.5),0 0 8px rgba(201,168,76,0.1)">Увійти до системи</a>
  </div>

</div>

<!-- FOOTER -->
<div style="background:#080b0f;border:1px solid rgba(201,168,76,0.15);border-top:none;border-radius:0 0 16px 16px;padding:20px 40px;text-align:center">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
    <div style="flex:1;height:1px;background:rgba(201,168,76,0.1)"></div>
    <span style="color:rgba(201,168,76,0.5);font-size:14px">&#10022;</span>
    <div style="flex:1;height:1px;background:rgba(201,168,76,0.1)"></div>
  </div>
  <div style="font-family:'Crimson Pro',Georgia,serif;font-size:14px;font-style:italic;color:rgba(245,237,209,0.5);margin-bottom:10px">Рекомендуємо змінити пароль після першого входу в систему</div>
  <div style="margin-bottom:10px">
    <a href="https://minerva-bi.com" style="font-family:'Cinzel',Georgia,serif;font-size:11px;letter-spacing:3px;color:#e8d5a3;text-decoration:none;border-bottom:1px solid rgba(201,168,76,0.35)">minerva-bi.com</a>
  </div>
  <div style="font-family:'Cinzel',Georgia,serif;font-size:9px;letter-spacing:3px;color:rgba(201,168,76,0.4);text-transform:uppercase">Minerva BI &middot; {co}</div>
</div>

</div>
</body></html>"""


# ── Custom User Admin (replaces Django default) ───────────────────────────────

admin.site.unregister(User)

@admin.register(User)
class MinervaUserAdmin(_DjangoUserAdmin):
    """
    Simplified User admin for Minerva.
    Hides Django's unused groups/permissions — real access control
    is in UserProfile (role, can_delete, can_export, etc.).
    """

    list_display  = ('username', 'email', 'full_name_col', 'is_active',
                     'is_staff', 'is_superuser', 'profile_link_col')
    list_display_links = ('username',)
    list_filter   = ('is_active', 'is_staff', 'is_superuser')
    search_fields = ('username', 'email', 'first_name', 'last_name')
    actions       = ['send_invitation_action']

    fieldsets = (
        ('👤 Обліковий запис', {
            'fields': ('username', 'password'),
        }),
        ('📋 Особисті дані', {
            'fields': ('first_name', 'last_name', 'email'),
        }),
        ('🔑 Доступ до системи', {
            'fields': ('is_active', 'is_staff', 'is_superuser'),
            'description': (
                '<b>is_staff</b> — дозволяє вхід в адмін-панель.&nbsp;&nbsp;'
                '<b>is_superuser</b> — повний доступ до всього (обережно!).'
            ),
        }),
        ('⚙️ Роль та права Minerva', {
            'fields': ('profile_panel',),
            'description': (
                '<div style="background:rgba(255,152,0,.08);border:1px solid rgba(255,152,0,.3);'
                'border-left:4px solid #ff9800;border-radius:6px;padding:10px 14px;font-size:12px;margin-bottom:6px">'
                '👆 Роль, модулі, can_delete/export/import — все керується в <strong>Профілі користувача</strong>, '
                'не тут. Натисніть кнопку нижче щоб перейти.'
                '</div>'
            ),
        }),
        ('🕒 Активність', {
            'fields': ('last_login', 'date_joined'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('last_login', 'date_joined', 'profile_panel')

    # Hide add_fieldsets groups/permissions too
    add_fieldsets = (
        ('👤 Новий користувач', {
            'classes': ('wide',),
            'fields': ('username', 'email', 'first_name', 'last_name',
                       'password1', 'password2', 'is_active', 'is_staff'),
        }),
    )

    @admin.display(description='Профіль Minerva')
    def profile_panel(self, obj):
        role_colors = {
            'superadmin': '#f85149', 'admin': '#ff9800', 'manager': '#58a6ff',
            'warehouse': '#3fb950', 'accountant': '#c9a84c', 'ai': '#a78bfa',
            'readonly': '#607d8b',
        }
        try:
            profile = obj.profile
            color = role_colors.get(profile.role, '#9aafbe')
            url = reverse('admin:core_userprofile_change', args=[profile.pk])
            allowed = profile.get_allowed_modules()
            if allowed == '__all__':
                modules_html = '<span style="color:var(--ok,#3fb950);font-weight:700">✅ Всі модулі</span>'
            else:
                modules_html = ', '.join(f'<code>{m}</code>' for m in allowed) or '<em>жодного</em>'
            return format_html(
                '<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">'
                '<a href="{}" style="display:inline-flex;align-items:center;gap:8px;padding:8px 18px;'
                'border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;'
                'background:{}22;color:{};border:1px solid {}55">'
                '⚙️ Редагувати профіль — {}</a>'
                '<span style="font-size:11px;color:var(--text-muted)">Модулі: {}</span>'
                '</div>',
                url, color, color, color, profile.get_role_display(),
                mark_safe(modules_html),
            )
        except UserProfile.DoesNotExist:
            try:
                url = reverse('admin:core_userprofile_add') + f'?user={obj.pk}'
            except NoReverseMatch:
                url = '#'
            return format_html(
                '<a href="{}" style="display:inline-flex;align-items:center;gap:8px;padding:8px 18px;'
                'border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;'
                'background:rgba(248,81,73,.1);color:var(--err,#f85149);border:1px solid rgba(248,81,73,.3)">'
                '⚠️ Профілю немає — створити зараз</a>',
                url,
            )

    @admin.display(description='Ім\'я')
    def full_name_col(self, obj):
        name = f'{obj.first_name} {obj.last_name}'.strip()
        return name or '—'

    @admin.display(description='Профіль / Права')
    def profile_link_col(self, obj):
        try:
            profile = obj.profile
            role_colors = {
                'superadmin': '#f85149', 'admin': '#ff9800', 'manager': '#58a6ff',
                'warehouse': '#3fb950', 'accountant': '#c9a84c', 'ai': '#a78bfa',
                'readonly': '#607d8b',
            }
            color = role_colors.get(profile.role, '#9aafbe')
            url = reverse('admin:core_userprofile_change', args=[profile.pk])
            return format_html(
                '<a href="{}" style="display:inline-flex;align-items:center;gap:6px;'
                'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                'text-decoration:none;background:{}22;color:{};border:1px solid {}55">'
                '⚙️ {}</a>',
                url, color, color, color, profile.get_role_display(),
            )
        except UserProfile.DoesNotExist:
            try:
                url = reverse('admin:core_userprofile_add') + f'?user={obj.pk}'
            except NoReverseMatch:
                url = '#'
            return format_html(
                '<a href="{}" style="color:var(--err,#f85149);font-size:11px;font-weight:600">'
                '⚠️ Немає профілю</a>', url
            )

    # ── Access control ────────────────────────────────────────────────────────

    @staticmethod
    def _user_is_admin(request):
        if request.user.is_superuser:
            return True
        role = getattr(getattr(request.user, 'profile', None), 'role', '')
        return role in ('superadmin', 'admin')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self._user_is_admin(request):
            return qs
        # Non-admin: own account + accounts they created
        from django.db.models import Q
        return qs.filter(
            Q(pk=request.user.pk) |
            Q(profile__created_by=request.user)
        )

    def has_change_permission(self, request, obj=None):
        if obj is not None and not self._user_is_admin(request):
            return obj.pk == request.user.pk
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if not self._user_is_admin(request):
            return False  # Non-admins cannot delete any user
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change and not self._user_is_admin(request):
            # Record who created this user (profile auto-created via signal)
            try:
                obj.profile.created_by = request.user
                obj.profile.save(update_fields=['created_by'])
            except Exception:
                pass

    # ── Invitation ────────────────────────────────────────────────────────────

    def get_urls(self):
        from django.urls import path
        return [
            path(
                '<int:pk>/send-invitation/',
                self.admin_site.admin_view(self.send_invitation_view),
                name='auth_user_send_invitation',
            ),
        ] + super().get_urls()

    @admin.action(description='📨 Надіслати запрошення')
    def send_invitation_action(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, '⛔ Тільки суперадміністратор може надсилати запрошення.', level='warning')
            return
        users = list(queryset.select_related('profile'))
        if len(users) == 1:
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(
                reverse('admin:auth_user_send_invitation', args=[users[0].pk])
            )
        import secrets
        try:
            from config.models import SystemSettings
            company = (SystemSettings.objects.filter(pk=1)
                       .values_list('company_name', flat=True).first() or 'Minerva BI')
        except Exception:
            company = 'Minerva BI'
        system_url = 'https://sevskiy.minerva-bi.com/admin/'
        sent, errors = [], []
        for user in users:
            if not user.email:
                errors.append(f'{user.username} (немає email)')
                continue
            result = self._do_send_invitation(user, secrets.token_urlsafe(10), company, system_url)
            (sent if result['ok'] else errors).append(
                user.username if result['ok'] else f"{user.username}: {result['error']}"
            )
        if sent:
            self.message_user(request, f'📨 Запрошення надіслано: {", ".join(sent)}.')
        if errors:
            self.message_user(request, f'⚠️ Помилки: {"; ".join(errors)}.', level='warning')

    def send_invitation_view(self, request, pk):
        import secrets
        from django.shortcuts import render, redirect
        from django.contrib import messages

        if not request.user.is_superuser:
            messages.error(request, '⛔ Доступ заборонено.')
            return redirect('admin:auth_user_changelist')

        try:
            user = User.objects.select_related('profile').get(pk=pk)
        except User.DoesNotExist:
            messages.error(request, 'Користувача не знайдено.')
            return redirect('admin:auth_user_changelist')

        try:
            from config.models import SystemSettings
            company = (SystemSettings.objects.filter(pk=1)
                       .values_list('company_name', flat=True).first() or 'Minerva BI')
        except Exception:
            company = 'Minerva BI'

        system_url       = request.build_absolute_uri('/admin/')
        default_password = secrets.token_urlsafe(10)

        if request.method == 'POST':
            password = request.POST.get('password', '').strip() or default_password
            sys_url  = request.POST.get('system_url', '').strip() or system_url
            result   = self._do_send_invitation(user, password, company, sys_url)
            if result['ok']:
                messages.success(request, f'📨 Запрошення надіслано на {user.email}.')
            else:
                messages.error(request, f'❌ Помилка надсилання: {result["error"]}')
            return redirect('admin:auth_user_change', pk)

        preview_html = _build_invitation_html(user, default_password, company, system_url)
        context = {
            **self.admin_site.each_context(request),
            'title':            f'Запрошення — {user.username}',
            'user_obj':         user,
            'default_password': default_password,
            'system_url':       system_url,
            'preview_html':     preview_html,
            'opts':             User._meta,
        }
        return render(request, 'admin/auth/user/send_invitation.html', context)

    def _do_send_invitation(self, user, password, company, system_url):
        """Set user password and send branded invitation email. Returns {'ok': bool, 'error': str}."""
        if not user.email:
            return {'ok': False, 'error': 'У користувача немає email-адреси'}

        user.set_password(password)
        user.save(update_fields=['password'])

        html_body = _build_invitation_html(user, password, company, system_url)
        subject   = f'🎉 Запрошення до {company} — Minerva BI'
        plain     = f'Запрошення до Minerva BI. Логін: {user.username}, Пароль: {password}. URL: {system_url}'

        try:
            from config.models import NotificationSettings
            ns = NotificationSettings.objects.filter(pk=1).first()
            if not ns or not ns.email_host:
                return {'ok': False, 'error': 'SMTP не налаштований (Config → Notifications → Email)'}
            from django.core.mail import EmailMultiAlternatives, get_connection
            conn = get_connection(
                backend='django.core.mail.backends.smtp.EmailBackend',
                host=ns.email_host,
                port=ns.email_port,
                username=ns.email_host_user,
                password=ns.email_host_password,
                use_tls=ns.email_use_tls,
                use_ssl=getattr(ns, 'email_use_ssl', False),
                fail_silently=False,
            )
            from_email = (getattr(ns, 'email_from', '') or ns.email_host_user or 'noreply@minerva.local').strip()
            msg = EmailMultiAlternatives(
                subject=subject, body=plain, from_email=from_email,
                to=[user.email], connection=conn,
            )
            msg.attach_alternative(html_body, 'text/html')
            msg.send()
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}


# ── UserProfile custom form ───────────────────────────────────────────────────

class UserProfileForm(forms.ModelForm):
    """Replace raw JSON allowed_modules with friendly checkboxes."""

    modules_override = forms.ModelMultipleChoiceField(
        queryset=ModuleRegistry.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label='Ручне перевизначення модулів',
        help_text=(
            'Заповнюйте лише якщо пакет не підходить. '
            'Порожньо = використовується пакет або роль.'
        ),
    )

    class Meta:
        model = UserProfile
        fields = '__all__'
        exclude = ['allowed_modules']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set querysets at request time (not at class definition / import time)
        self.fields['bundle'].queryset = ModuleBundle.objects.all().order_by('name')
        self.fields['bundle'].empty_label = '— без пакету (використовувати роль) —'
        self.fields['modules_override'].queryset = ModuleRegistry.objects.order_by('order', 'name')
        if self.instance.pk and self.instance.allowed_modules:
            self.fields['modules_override'].initial = (
                ModuleRegistry.objects.filter(app_label__in=self.instance.allowed_modules)
            )
        # Render denied_models as hidden input — UI managed by JS
        self.fields['denied_models'].widget  = forms.HiddenInput()
        self.fields['denied_models'].required = False
        # Render module_operations as hidden input — UI managed by JS
        self.fields['module_operations'].widget   = forms.HiddenInput()
        self.fields['module_operations'].required = False

    def save(self, commit=True):
        instance = super().save(commit=False)
        selected = self.cleaned_data.get('modules_override', [])
        if selected:
            # Explicit list selected → use it
            instance.allowed_modules = [m.app_label for m in selected]
        else:
            # Nothing selected → None = use role/bundle defaults
            instance.allowed_modules = None
        if commit:
            instance.save()
            self.save_m2m()
        return instance


# ── AuditLog ──────────────────────────────────────────────────────────────────

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display    = ('timestamp', 'user', 'action_badge', 'object_repr', 'ip_address')
    list_filter     = ('action', 'user')
    search_fields   = ('user__username', 'object_repr', 'ip_address')
    readonly_fields = (
        'user', 'action', 'content_type', 'object_id', 'object_repr',
        'ip_address', 'extra', 'timestamp',
    )
    date_hierarchy  = 'timestamp'

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def has_view_permission(self, request, obj=None):
        if not (request.user.is_active and request.user.is_staff):
            return False
        from core.utils import user_can
        return user_can(request.user, 'view_audit')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description='Дія')
    def action_badge(self, obj):
        colors = {
            'login':  '#3fb950', 'logout': '#9aafbe',
            'create': '#58a6ff', 'update': '#e3b341',
            'delete': '#f85149', 'view':   '#607d8b',
            'export': '#c9a84c', 'import': '#2196f3',
        }
        color = colors.get(obj.action, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px;font-weight:600">{}</span>',
            color, obj.get_action_display(),
        )


# ── ModuleBundle ──────────────────────────────────────────────────────────────

@admin.register(ModuleBundle)
class ModuleBundleAdmin(admin.ModelAdmin):
    list_display      = ('name_badge', 'modules_summary', 'is_system', 'description')
    list_filter       = ('is_system',)
    search_fields     = ('name', 'description')
    filter_horizontal = ('modules',)
    fieldsets = (
        (None, {
            'fields': ('name', 'color', 'description', 'is_system'),
        }),
        ('Модулі пакету', {
            'fields': ('modules',),
            'description': '🔒 Core-модулі (core, config, auth) завжди додаються автоматично',
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        from config.admin import ColorPickerWidget
        form = super().get_form(request, obj, **kwargs)
        if 'color' in form.base_fields:
            form.base_fields['color'].widget = ColorPickerWidget()
        return form

    def has_delete_permission(self, request, obj=None):
        if obj and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description='Назва')
    def name_badge(self, obj):
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 12px;'
            'border-radius:4px;font-weight:600">{}</span>',
            obj.color or '#58a6ff', obj.name,
        )

    @admin.display(description='Склад пакету')
    def modules_summary(self, obj):
        mods = obj.modules.order_by('order').values_list('name', 'tier')
        tier_colors = {'core': '#f85149', 'standard': '#58a6ff', 'premium': '#c9a84c'}
        badges = mark_safe('')
        for name, tier in mods:
            badges += format_html(
                '<span style="background:{};color:#fff;padding:1px 6px;'
                'border-radius:3px;font-size:11px;margin:1px 2px;display:inline-block">{}</span>',
                tier_colors.get(tier, '#9aafbe'), name,
            )
        return badges or '—'


# ── RichSignatureWidget ───────────────────────────────────────────────────────

class RichSignatureWidget(forms.Widget):
    """Mini WYSIWYG editor for email signature stored as HTML.
    Supports per-line bold / italic / underline / font-size / color / font-face.
    """

    def render(self, name, value, attrs=None, renderer=None):
        uid     = (attrs or {}).get('id', f'id_{name}')
        initial = json.dumps(value or '')   # safe JSON string for JS innerHTML assignment
        return mark_safe(f'''
<div style="border:1px solid var(--border-strong,#3d4f61);border-radius:8px;
            overflow:hidden;max-width:640px">
  <div style="display:flex;gap:4px;padding:6px 8px;flex-wrap:wrap;align-items:center;
              background:var(--bg-hover,#1e2d40);
              border-bottom:1px solid var(--border-strong,#3d4f61)">
    <button type="button" title="Жирний" onclick="mvSigCmd('{uid}','bold')"
            style="padding:2px 9px;border-radius:4px;font-weight:700;font-size:13px;
                   cursor:pointer;border:1px solid var(--border-strong,#3d4f61);
                   background:transparent;color:var(--text,#c9d8e4)">B</button>
    <button type="button" title="Курсив" onclick="mvSigCmd('{uid}','italic')"
            style="padding:2px 9px;border-radius:4px;font-style:italic;font-size:13px;
                   cursor:pointer;border:1px solid var(--border-strong,#3d4f61);
                   background:transparent;color:var(--text,#c9d8e4)">I</button>
    <button type="button" title="Підкреслення" onclick="mvSigCmd('{uid}','underline')"
            style="padding:2px 9px;border-radius:4px;text-decoration:underline;font-size:13px;
                   cursor:pointer;border:1px solid var(--border-strong,#3d4f61);
                   background:transparent;color:var(--text,#c9d8e4)">U</button>
    <div style="width:1px;height:20px;background:var(--border-strong,#3d4f61);margin:0 2px"></div>
    <select title="Розмір" onchange="mvSigCmd('{uid}','fontSize',this.value);this.selectedIndex=0"
            style="padding:2px 6px;border-radius:4px;font-size:12px;cursor:pointer;
                   border:1px solid var(--border-strong,#3d4f61);
                   background:var(--bg-input,#141f2b);color:var(--text,#c9d8e4)">
      <option value="">Розмір</option>
      <option value="1">Дрібний (8px)</option>
      <option value="2">Малий (10px)</option>
      <option value="3">Звичайний (12px)</option>
      <option value="4">Середній (14px)</option>
      <option value="5">Великий (18px)</option>
      <option value="6">Дуже великий (24px)</option>
      <option value="7">Максимальний (36px)</option>
    </select>
    <select title="Шрифт" onchange="mvSigCmd('{uid}','fontName',this.value);this.selectedIndex=0"
            style="padding:2px 6px;border-radius:4px;font-size:12px;cursor:pointer;
                   border:1px solid var(--border-strong,#3d4f61);
                   background:var(--bg-input,#141f2b);color:var(--text,#c9d8e4)">
      <option value="">Шрифт</option>
      <option value="Arial">Arial</option>
      <option value="Georgia">Georgia</option>
      <option value="Verdana">Verdana</option>
      <option value="Courier New">Courier New</option>
      <option value="Times New Roman">Times New Roman</option>
    </select>
    <div style="width:1px;height:20px;background:var(--border-strong,#3d4f61);margin:0 2px"></div>
    <label title="Колір тексту"
           style="display:flex;align-items:center;gap:3px;cursor:pointer;
                  font-size:12px;color:var(--text-muted,#9aafbe)">
      <span style="font-weight:700;font-size:14px">A</span>
      <input type="color" value="#c9d8e4"
             oninput="mvSigCmd('{uid}','foreColor',this.value)"
             style="width:22px;height:22px;padding:0;cursor:pointer;
                    border:1px solid var(--border-strong,#3d4f61);
                    border-radius:3px;background:transparent">
    </label>
    <button type="button" title="Очистити" onclick="mvSigClear('{uid}')"
            style="margin-left:auto;padding:2px 8px;border-radius:4px;font-size:11px;
                   cursor:pointer;border:1px solid var(--border-strong,#3d4f61);
                   background:transparent;color:var(--err,#f85149)">✕ Очистити</button>
  </div>
  <div id="{uid}_ed" contenteditable="true"
       oninput="mvSigSync('{uid}')" onblur="mvSigSync('{uid}')"
       style="min-height:90px;max-height:280px;overflow-y:auto;padding:10px 14px;
              outline:none;font-size:13px;line-height:1.7;
              background:var(--bg-card,#1a2535);color:var(--text,#c9d8e4);
              font-family:Arial,sans-serif"></div>
  <div style="padding:3px 10px;font-size:10px;color:var(--text-dim,#607d8b);
              background:var(--bg-input,#141f2b);border-top:1px solid var(--border-strong,#3d4f61)">
    Підказка: виділіть текст і застосуйте форматування. {{name}} → ім'я користувача.
  </div>
</div>
<textarea id="{uid}" name="{name}" style="display:none"></textarea>
<script>
function mvSigSync(uid){{
  document.getElementById(uid).value=document.getElementById(uid+'_ed').innerHTML;
}}
function mvSigCmd(uid,cmd,val){{
  document.getElementById(uid+'_ed').focus();
  document.execCommand(cmd,false,val||null);
  mvSigSync(uid);
}}
function mvSigClear(uid){{
  document.getElementById(uid+'_ed').innerHTML='';
  document.getElementById(uid).value='';
}}
(function(){{
  var ed=document.getElementById('{uid}_ed');
  var ta=document.getElementById('{uid}');
  ed.innerHTML={initial};
  ta.value=ed.innerHTML;
  var f=ed.closest('form');
  if(f) f.addEventListener('submit',function(){{ta.value=ed.innerHTML;}},true);
}})();
</script>
''')

    def value_from_datadict(self, data, files, name):
        return data.get(name, '')


# ── UserProfile ───────────────────────────────────────────────────────────────

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    form          = UserProfileForm
    list_display  = ('user', 'role_badge', 'effective_access', 'ai_enabled')
    list_filter   = ('role', 'bundle', 'ai_enabled')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    readonly_fields = (
        'effective_access_detail', 'denied_models_panel', 'module_operations_panel',
        'effective_permissions_display', 'permissions_debug_link',
    )

    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        for field_name in ('imap_password', 'smtp_password'):
            if field_name in form.base_fields:
                form.base_fields[field_name].widget = PasswordInput(render_value=True)
        if 'smtp_signature' in form.base_fields:
            form.base_fields['smtp_signature'].widget = RichSignatureWidget()
        return form

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'bundle':
            kwargs['queryset']    = ModuleBundle.objects.all().order_by('name')
            kwargs['empty_label'] = '— без пакету (використовувати роль) —'
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        from core.utils import ROLE_PERMISSIONS, ROLE_OPERATIONS, ALL_OPS, OP_LABELS
        from django.contrib import admin as _admin
        extra = extra_context or {}

        # app_label → pk (string) — needed because checkbox values are PKs
        all_modules = list(ModuleRegistry.objects.all())
        module_pk_map = {m.app_label: str(m.pk) for m in all_modules}
        module_names  = {m.app_label: m.name     for m in all_modules}

        # role → [app_labels] or '__all__'
        role_modules = {
            role: (perms['modules'] if isinstance(perms.get('modules'), list) else '__all__')
            for role, perms in ROLE_PERMISSIONS.items()
        }

        # bundle pk → [app_labels]
        bundle_modules = {
            str(b.pk): b.get_module_labels()
            for b in ModuleBundle.objects.prefetch_related('modules')
        }

        # app_label → [{object_name, verbose_name}] from admin registry
        app_models: dict = {}
        for model_cls in _admin.site._registry:
            al = model_cls._meta.app_label
            app_models.setdefault(al, [])
            app_models[al].append({
                'object_name':  model_cls.__name__,
                'verbose_name': str(model_cls._meta.verbose_name_plural or model_cls.__name__),
            })
        for al in app_models:
            app_models[al].sort(key=lambda m: m['verbose_name'])

        # role_operations: for each role, serialize (convert '__all__' to list for JS)
        role_operations_js = {}
        for role, ops in ROLE_OPERATIONS.items():
            role_operations_js[role] = ops  # keep '__all__' string, JS handles it

        # role → can_* defaults for conflict detection in JS
        role_can = {
            role: {
                'delete':       perms.get('can_delete',       False),
                'export':       perms.get('can_export',        False),
                'import':       perms.get('can_import',        False),
                'view_audit':   perms.get('can_view_audit',    False),
                'manage_users': perms.get('can_manage_users',  False),
            }
            for role, perms in ROLE_PERMISSIONS.items()
        }

        extra['mv_module_pk_map']    = json.dumps(module_pk_map,       ensure_ascii=False)
        extra['mv_module_names']     = json.dumps(module_names,        ensure_ascii=False)
        extra['mv_role_modules']     = json.dumps(role_modules,        ensure_ascii=False)
        extra['mv_bundle_modules']   = json.dumps(bundle_modules,      ensure_ascii=False)
        extra['mv_app_models_json']  = json.dumps(app_models,          ensure_ascii=False)
        extra['mv_role_operations']  = json.dumps(role_operations_js,  ensure_ascii=False)
        extra['mv_all_ops']          = json.dumps(ALL_OPS,             ensure_ascii=False)
        extra['mv_op_labels']        = json.dumps(OP_LABELS,           ensure_ascii=False)
        extra['mv_role_can']         = json.dumps(role_can,            ensure_ascii=False)
        extra['mv_changelist_url']   = '../'
        return super().changeform_view(request, object_id, form_url, extra)

    fieldsets = (
        ('👤 Користувач', {
            'fields': ('user', 'role', 'notes'),
        }),
        ('🧩 Доступ до модулів', {
            'fields': (
                'effective_access_detail',
                'effective_permissions_display',
                'permissions_debug_link',
                'bundle', 'modules_override',
            ),
            'description': (
                '<strong>Як визначається доступ (за пріоритетом):</strong><br>'
                '1️⃣ <strong>Ручне перевизначення</strong> — якщо відмічено нижче<br>'
                '2️⃣ <strong>Пакет</strong> — якщо вибрано пакет<br>'
                '3️⃣ <strong>Роль</strong> — автоматично за роллю користувача'
            ),
        }),
        ('🚫 Заборонені підмодулі', {
            'fields': ('denied_models_panel', 'denied_models'),
            'description': (
                'Тут можна заборонити окремі моделі (підрозділи) всередині дозволеного модуля. '
                'Заборонений підмодуль не з\'являтиметься у навігаційному меню. '
                'Натисніть на назву модуля щоб розкрити список підмодулів.'
            ),
        }),
        ('⚡ Гранулярні операції', {
            'fields': ('module_operations_panel', 'module_operations'),
            'classes': ('collapse',),
            'description': (
                'Дозволити/заборонити конкретні операції (перегляд, створення, редагування, видалення, '
                'експорт, імпорт) окремо для кожного модуля. '
                '<strong>None = авто за роллю.</strong> '
                'Увімкніть "Ручне перевизначення" щоб задати явні операції.'
            ),
        }),
        ('🔒 Дозволи (перевизначення)', {
            'fields': ('can_delete', 'can_export', 'can_import', 'can_view_audit', 'can_manage_users'),
            'classes': ('collapse',),
            'description': (
                'Порожньо = використовуються дефолти ролі. '
                'Явне Так/Ні — перевизначає роль.'
            ),
        }),
        ('⚙️ Персональні налаштування', {
            'fields': ('notify_email', 'notify_telegram', 'interface_language', 'items_per_page', 'theme'),
            'classes': ('collapse',),
            'description': 'Ці налаштування юзер може змінити самостійно на сторінці /core/my-settings/',
        }),
        ('📧 Особиста пошта (IMAP)', {
            'fields': (
                'imap_enabled',
                ('imap_host', 'imap_port'),
                'imap_use_ssl',
                'imap_user', 'imap_password',
                'imap_sent_folder',
            ),
            'classes': ('collapse',),
            'description': (
                'Листи цього юзера будуть читатися з його особистого ящика. '
                'ionos: host=imap.ionos.de, port=993, SSL=✓, Sent=INBOX.Sent. '
                'Команда: <code>python manage.py fetch_emails</code>'
            ),
        }),
        ('📤 Особистий SMTP (відповіді клієнтам з CRM)', {
            'fields': (
                ('smtp_host', 'smtp_port'),
                ('smtp_use_tls', 'smtp_use_ssl'),
                'smtp_user', 'smtp_password',
                'smtp_from',
                'smtp_signature',
            ),
            'classes': ('collapse',),
            'description': (
                'Якщо заповнено — листи з CRM (кнопка 📤 Надіслати) відправляються '
                'з особистого ящика цього користувача. '
                'Якщо порожньо — використовується глобальний SMTP (Config → Notifications). '
                'ionos: host=smtp.ionos.de, port=587, TLS=✓. '
                'Gmail: host=smtp.gmail.com, port=587, TLS=✓, потрібен App Password.'
            ),
        }),
        ('🤖 AI-асистент', {
            'fields': (
                'ai_enabled', 'ai_assistant_role',
                'telegram_id', 'telegram_username',
                'ai_model', 'ai_temperature', 'ai_system_prompt',
            ),
            'classes': ('collapse',),
        }),
    )

    def get_urls(self):
        from django.urls import path
        return [
            path(
                '<int:pk>/debug/',
                self.admin_site.admin_view(self._debug_view),
                name='userprofile_debug',
            ),
        ] + super().get_urls()

    def _debug_view(self, request, pk):
        from django.http import JsonResponse
        from django.contrib.auth.models import User as _User
        from core.utils import user_can
        from core.models import ModuleRegistry
        try:
            u = _User.objects.select_related('profile').get(pk=pk)
            p = u.profile
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=404)

        allowed = p.get_allowed_modules()
        modules = {}
        for app in ModuleRegistry.get_active_apps():
            has = (allowed == '__all__' or app in (allowed or []))
            ops = p.get_allowed_operations(app) if has else []
            modules[app] = {
                'access': has,
                'ops': ops if ops != '__all__' else 'all',
            }

        return JsonResponse({
            'user': {
                'username':    u.username,
                'is_superuser': u.is_superuser,
                'is_staff':     u.is_staff,
            },
            'profile': {
                'role':              p.role,
                'allowed_modules':   allowed if allowed != '__all__' else 'all',
                'module_operations': p.module_operations,
                'denied_models':     p.denied_models,
            },
            'can': {a: user_can(u, a) for a in
                    ['delete', 'export', 'import', 'manage_users', 'view_audit']},
            'modules': modules,
        }, json_dumps_params={'ensure_ascii': False, 'indent': 2})

    def save_model(self, request, obj, form, change):
        if change:
            try:
                old = UserProfile.objects.get(pk=obj.pk)
                if old.role != obj.role:
                    # Role changed → reset to None so new role defaults apply
                    obj.allowed_modules = None
                    self.message_user(
                        request,
                        f'Роль змінено → {obj.get_role_display()}. '
                        f'Модулі скинуто до дефолтів нової ролі.',
                    )
            except UserProfile.DoesNotExist:
                pass
        super().save_model(request, obj, form, change)
        if not obj.user.is_staff:
            User.objects.filter(pk=obj.user_id).update(is_staff=True)

    @admin.display(description='Роль')
    def role_badge(self, obj):
        colors = {
            'superadmin': '#f85149', 'admin': '#c9a84c',
            'manager': '#58a6ff',    'warehouse': '#3fb950',
            'accountant': '#2196f3', 'ai': '#9c27b0',
            'readonly': '#607d8b',
        }
        color = colors.get(obj.role, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px;font-weight:600">{}</span>',
            color, obj.get_role_display(),
        )

    @admin.display(description='Активний доступ')
    def effective_access(self, obj):
        modules = obj.get_allowed_modules()
        if modules == '__all__':
            return format_html(
                '<span style="color:#3fb950;font-weight:600">✅ Всі модулі</span>'
            )
        source = '📋' if obj.allowed_modules else ('🧩' if obj.bundle_id else '👤')
        return format_html(
            '{} <span style="color:var(--text-muted,#9aafbe)">{} модулів</span>',
            source, len(modules),
        )

    @admin.display(description='Ефективний доступ (що бачить користувач)')
    def effective_access_detail(self, obj):
        if not obj.pk:
            return '— збережіть профіль щоб побачити —'

        modules = obj.get_allowed_modules()

        # Determine source
        if obj.allowed_modules:
            source_html = format_html(
                '<span style="background:#e3b341;color:#000;padding:2px 8px;'
                'border-radius:4px;font-size:11px">1️⃣ Ручне перевизначення</span>'
            )
        elif obj.bundle_id:
            source_html = format_html(
                '<span style="background:{};color:#fff;padding:2px 8px;'
                'border-radius:4px;font-size:11px">2️⃣ Пакет: {}</span>',
                obj.bundle.color or '#58a6ff', obj.bundle.name,
            )
        else:
            source_html = format_html(
                '<span style="background:#607d8b;color:#fff;padding:2px 8px;'
                'border-radius:4px;font-size:11px">3️⃣ Роль: {}</span>',
                obj.get_role_display(),
            )

        if modules == '__all__':
            return format_html(
                '{}&nbsp; <strong style="color:#3fb950">Доступ до всіх модулів</strong>',
                source_html,
            )

        tier_colors = {'core': '#f85149', 'standard': '#58a6ff', 'premium': '#c9a84c'}
        try:
            reg = {
                r.app_label: r
                for r in ModuleRegistry.objects.filter(app_label__in=modules)
            }
        except Exception:
            reg = {}

        badges = mark_safe('')
        for app_label in sorted(modules):
            r = reg.get(app_label)
            name  = r.name if r else app_label
            color = tier_colors.get(r.tier if r else '', '#9aafbe')
            badges += format_html(
                '<span style="background:{};color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:11px;margin:2px 3px;display:inline-block">{}</span>',
                color, name,
            )

        return format_html('{}&nbsp; {}', source_html, badges)

    @admin.display(description='Управління підмодулями')
    def denied_models_panel(self, obj):
        return mark_safe(
            '<div id="mv-denied-panel" style="min-height:40px">'
            '<em style="color:var(--text-dim);font-size:12px">Завантаження…</em>'
            '</div>'
        )

    @admin.display(description='Операції по модулях')
    def module_operations_panel(self, obj):
        return mark_safe(
            '<div id="mv-ops-panel" style="min-height:40px">'
            '<em style="color:var(--text-dim);font-size:12px">Завантаження…</em>'
            '</div>'
        )

    @admin.display(description='Реальні права')
    def effective_permissions_display(self, obj):
        """Shows effective permissions — source and per-module operations."""
        if not obj or not obj.pk:
            return '—'
        from core.utils import ROLE_OPERATIONS, ROLE_DEFAULTS

        if obj.role in ('superadmin', 'admin'):
            return format_html(
                '<span style="color:var(--ok,#3fb950);font-weight:600">'
                '✅ Повний доступ ({})</span>',
                obj.get_role_display(),
            )

        allowed = obj.get_allowed_modules()
        if isinstance(allowed, list):
            modules = allowed
        else:
            modules = ROLE_DEFAULTS.get(obj.role, {}).get('modules', []) or []

        if not modules:
            return format_html(
                '<span style="color:var(--text-dim,#607d8b)">Дефолт ролі: {}</span>',
                obj.get_role_display(),
            )

        role_ops = ROLE_OPERATIONS.get(obj.role, {})
        rows = mark_safe('')
        for app in modules[:15]:
            if obj.module_operations and app in (obj.module_operations or {}):
                ops, color, src = obj.module_operations[app], '#17a2b8', 'custom'
            elif isinstance(role_ops, dict) and app in role_ops:
                ops, color, src = role_ops[app], '#607d8b', 'role'
            else:
                ops, color, src = ['view'], '#9aafbe', 'default'
            ops_str = ', '.join(ops) if isinstance(ops, list) else str(ops)
            rows += format_html(
                '<div style="font-size:12px;margin:2px 0;display:flex;gap:8px">'
                '<span style="width:110px;font-weight:500">{}</span>'
                '<span style="color:{}">{}</span>'
                '<span style="color:var(--text-dim,#607d8b);font-size:10px">({})</span>'
                '</div>',
                app, color, ops_str, src,
            )

        return format_html(
            '<div style="font-family:monospace;line-height:1.6">{}</div>'
            '<div style="font-size:10px;color:var(--text-dim,#607d8b);margin-top:4px">'
            'сірий=дефолт ролі · синій=перевизначено · світлий=тільки view</div>',
            rows,
        )

    @admin.display(description='Діагностика')
    def permissions_debug_link(self, obj):
        if not obj or not obj.pk:
            return '—'
        return format_html(
            '<a href="/admin/core/userprofile/{}/debug/" target="_blank"'
            ' style="padding:4px 12px;background:#17a2b8;color:#fff;'
            'border-radius:6px;text-decoration:none;font-size:12px">'
            '🔍 JSON діагностика прав</a>',
            obj.pk,
        )


# ── ModuleRegistry ────────────────────────────────────────────────────────────

@admin.register(ModuleRegistry)
class ModuleRegistryAdmin(admin.ModelAdmin):
    list_display  = ('name', 'app_label', 'tier_badge', 'is_active', 'order')
    list_filter   = ('tier', 'is_active')
    list_editable = ('is_active', 'order')
    search_fields = ('app_label', 'name')
    ordering      = ('order', 'app_label')

    def has_delete_permission(self, request, obj=None):
        if obj and obj.tier == ModuleRegistry.Tier.CORE:
            return False
        return request.user.is_superuser

    @admin.display(description='Тир')
    def tier_badge(self, obj):
        colors = {
            'core':     '#f85149',
            'standard': '#58a6ff',
            'premium':  '#c9a84c',
        }
        color = colors.get(obj.tier, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            color, obj.get_tier_display(),
        )


# ── TenantAccount ──────────────────────────────────────────────────────────────

@admin.register(TenantAccount)
class TenantAccountAdmin(admin.ModelAdmin):
    """Vendor dashboard — visible to superadmin only."""

    list_display  = (
        'company_name', 'owner_email', 'package_badge',
        'status_badge', 'days_badge', 'modules_count',
        'registered_at', 'actions_col',
    )
    list_filter   = ('status', 'package', 'company_country', 'email_verified')
    search_fields = ('company_name', 'owner_email', 'owner_name')
    readonly_fields = (
        'registered_at', 'activated_at', 'last_login_at',
        'verification_token', 'verification_sent_at',
        'active_modules', 'vendor_summary',
    )
    ordering = ['-registered_at']

    fieldsets = (
        ('🏢 Клієнт', {
            'fields': (
                'company_name', 'company_country', 'contact_phone',
                'owner_name', 'owner_email', 'owner_user',
            ),
        }),
        ('📦 Пакет і статус', {
            'fields': ('package', 'status', 'email_verified', 'trial_ends_at', 'paid_until'),
            'description': (
                'Після оплати: змінити status → active, '
                'встановити paid_until на дату закінчення.'
            ),
        }),
        ('🧩 Активні модулі', {
            'fields': ('active_modules',),
            'description': 'Оновлюється автоматично при зміні пакету.',
            'classes': ('collapse',),
        }),
        ('📊 Статистика', {
            'fields': ('vendor_summary',),
        }),
        ('🗒️ Нотатки', {
            'fields': ('notes',),
        }),
        ('📋 Службові', {
            'fields': ('registered_at', 'activated_at', 'last_login_at', 'verification_sent_at'),
            'classes': ('collapse',),
        }),
    )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        if change and 'package' in (form.changed_data or []):
            super().save_model(request, obj, form, change)
            obj.activate_modules()
        else:
            super().save_model(request, obj, form, change)
        if change and 'status' in (form.changed_data or []):
            AuditLog.log(
                action='settings',
                user=request.user,
                module='core',
                model_name='TenantAccount',
                object_id=obj.pk,
                object_repr=str(obj),
                extra={'event': 'status_changed', 'company': obj.company_name, 'new_status': obj.status},
                request=request,
            )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        qs = TenantAccount.objects.all()
        extra_context.update({
            'active_count':  qs.filter(status='active').count(),
            'trial_count':   qs.filter(status='trial').count(),
            'expired_count': qs.filter(status='expired').count(),
            'pending_count': qs.filter(status='pending').count(),
            'total_count':   qs.count(),
        })
        return super().changelist_view(request, extra_context)

    # ── List display columns ───────────────────────────────

    @admin.display(description='Пакет')
    def package_badge(self, obj):
        COLORS = {
            'free':     '#6c757d', 'starter':  '#007bff',
            'business': '#fd7e14', 'custom':   '#6610f2',
        }
        c = COLORS.get(obj.package, '#333')
        label = obj.get_package_display().split(' —')[0]
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;'
            'font-size:11px;background:{};color:#fff">{}</span>',
            c, label)

    @admin.display(description='Статус')
    def status_badge(self, obj):
        COLORS = {
            'pending':   ('#ffc107', '#000'),
            'trial':     ('#17a2b8', '#fff'),
            'active':    ('#28a745', '#fff'),
            'suspended': ('#fd7e14', '#fff'),
            'expired':   ('#dc3545', '#fff'),
            'cancelled': ('#6c757d', '#fff'),
        }
        bg, fg = COLORS.get(obj.status, ('#333', '#fff'))
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;'
            'font-size:11px;background:{};color:{}">{}</span>',
            bg, fg, obj.get_status_display())

    @admin.display(description='Залишилось')
    def days_badge(self, obj):
        days = obj.days_until_expiry
        if days is None:
            return '—'
        if days < 0:
            return format_html('<span style="color:#dc3545">Прострочено {}д</span>', abs(days))
        if days <= 7:
            return format_html('<span style="color:#fd7e14">{}д</span>', days)
        return format_html('<span style="color:#28a745">{}д</span>', days)

    @admin.display(description='Модулі')
    def modules_count(self, obj):
        n = len(obj.active_modules or [])
        return format_html('<span style="color:#999;font-size:12px">{} модулів</span>', n)

    @admin.display(description='Дії')
    def actions_col(self, obj):
        if obj.status == 'pending':
            return format_html(
                '<a href="{}/change/" style="color:#ffc107;font-size:12px">Активувати →</a>',
                obj.pk)
        if obj.status in ('expired', 'trial'):
            return format_html(
                '<a href="{}/change/" style="color:#007bff;font-size:12px">Продовжити →</a>',
                obj.pk)
        return '—'

    @admin.display(description='Зведення')
    def vendor_summary(self, obj):
        if not obj.pk:
            return '—'
        lines = [
            f'Email підтверджено: {"✅ Так" if obj.email_verified else "❌ Ні"}',
            f'Модулів активних: {len(obj.active_modules or [])}',
            f'Реєстрація: {obj.registered_at.strftime("%d.%m.%Y %H:%M") if obj.registered_at else "—"}',
            f'Активація: {obj.activated_at.strftime("%d.%m.%Y %H:%M") if obj.activated_at else "—"}',
            f'Trial до: {obj.trial_ends_at.strftime("%d.%m.%Y") if obj.trial_ends_at else "—"}',
        ]
        return format_html(
            '<div style="font-size:13px;line-height:1.8">{}</div>',
            format_html('<br>'.join(lines)))
