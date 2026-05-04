from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

User = get_user_model()


class AuditLog(models.Model):
    """Immutable audit trail for all significant actions."""

    class Action(models.TextChoices):
        LOGIN    = 'login',    'Вхід'
        LOGOUT   = 'logout',   'Вихід'
        CREATE   = 'create',   'Створення'
        UPDATE   = 'update',   'Зміна'
        DELETE   = 'delete',   'Видалення'
        VIEW     = 'view',     'Перегляд'
        EXPORT   = 'export',   'Експорт'
        IMPORT   = 'import',   'Імпорт'
        SETTINGS = 'settings', 'Налаштування'

    user         = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name='Користувач',
    )
    action       = models.CharField(max_length=10, choices=Action.choices, verbose_name='Дія')
    content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="Тип об'єкта",
    )
    object_id    = models.CharField(max_length=255, blank=True, verbose_name="ID об'єкта")
    object_repr  = models.CharField(max_length=500, blank=True, verbose_name="Об'єкт")
    ip_address   = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP-адреса')
    extra        = models.JSONField(default=dict, blank=True, verbose_name='Деталі')
    timestamp    = models.DateTimeField(default=timezone.now, db_index=True, verbose_name='Час')

    class Meta:
        verbose_name        = 'Запис аудиту'
        verbose_name_plural = 'Журнал аудиту'
        ordering            = ['-timestamp']
        indexes = [
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['action', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.get_action_display()} — {self.user or 'система'} — {self.timestamp:%d.%m.%Y %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk:
            return  # immutable — do not allow updates
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # immutable — do not allow deletes from ORM

    @classmethod
    def log(cls, action, module=None, model_name=None, object_id=None,
            object_repr='', extra=None, user=None, request=None):
        """Convenience factory for creating audit log entries."""
        ip = None
        if request:
            ip = request.META.get('REMOTE_ADDR')
            if not user and hasattr(request, 'user') and request.user.is_authenticated:
                user = request.user
        entry_extra = dict(extra or {})
        if module:
            entry_extra.setdefault('module', module)
        if model_name:
            entry_extra.setdefault('model_name', model_name)
        cls.objects.create(
            user=user,
            action=action,
            object_id=str(object_id) if object_id is not None else '',
            object_repr=str(object_repr),
            ip_address=ip,
            extra=entry_extra,
        )


class UserProfile(models.Model):
    """Extended profile: role + AI settings + optional module overrides."""

    class Role(models.TextChoices):
        SUPERADMIN = 'superadmin', '👑 Суперадмін'
        ADMIN      = 'admin',      '🔑 Адміністратор'
        MANAGER    = 'manager',    '💼 Менеджер'
        WAREHOUSE  = 'warehouse',  '📦 Складник'
        ACCOUNTANT = 'accountant', '💰 Бухгалтер'
        AI         = 'ai',         '🤖 AI-асистент'
        READONLY   = 'readonly',   '👁 Тільки перегляд'

    user             = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='profile', verbose_name='Користувач',
    )
    role             = models.CharField(
        max_length=20, choices=Role.choices, default=Role.MANAGER, verbose_name='Роль',
    )
    notes            = models.TextField(blank=True, verbose_name='Нотатки')

    # AI assistant settings
    ai_enabled       = models.BooleanField(default=False, verbose_name='AI увімкнено')
    ai_model         = models.CharField(
        max_length=60, default='claude-sonnet-4-6', verbose_name='AI модель',
    )
    ai_system_prompt = models.TextField(blank=True, verbose_name='Системний промпт')
    ai_temperature   = models.FloatField(default=0.7, verbose_name='Температура AI')

    # Пакет модулів (пріоритет над роллю, але нижче ніж allowed_modules)
    bundle           = models.ForeignKey(
        'ModuleBundle', null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name='Пакет модулів',
        help_text='Якщо вказано — використовується замість дефолтів ролі',
    )
    # Повне ручне перевизначення (найвищий пріоритет; None = авто)
    allowed_modules  = models.JSONField(
        null=True, blank=True, default=None,
        verbose_name='Ручний список модулів',
        help_text='None = авто (роль/пакет). [] = заблокувати все крім ядра. [...] = явний список.',
    )

    # ── AI Асистент — Telegram ───────────────────────────────
    telegram_id       = models.BigIntegerField(
        null=True, blank=True, unique=True,
        verbose_name='Telegram ID',
        help_text='Дізнатись через /myid в боті')
    telegram_username = models.CharField(
        max_length=100, blank=True,
        verbose_name='Telegram username')

    # ── AI Асистент — роль ────────────────────────────────────
    AI_ROLE_CHOICES = [
        ('viewer',  'Viewer — тільки загальні дані'),
        ('staff',   'Staff — продажі, склад, клієнти'),
        ('manager', 'Manager — + аналітика, звіти'),
        ('admin',   'Admin — повний доступ'),
    ]
    ai_assistant_role = models.CharField(
        max_length=20, choices=AI_ROLE_CHOICES, default='viewer',
        verbose_name='Роль в AI асистенті',
        help_text='Визначає які дані доступні через AI чат')

    # ── AI Асистент — статистика ──────────────────────────────
    ai_total_requests  = models.PositiveIntegerField(
        default=0, verbose_name='AI запитів всього')
    ai_total_spent_usd = models.DecimalField(
        max_digits=10, decimal_places=6, default=0,
        verbose_name='AI витрачено ($)')
    ai_last_active     = models.DateTimeField(
        null=True, blank=True, verbose_name='Остання AI активність')

    # Per-user permission overrides (None = use role default)
    can_delete     = models.BooleanField(
        null=True, blank=True, default=None,
        verbose_name='Може видаляти',
        help_text='Порожньо = за роллю',
    )
    can_export     = models.BooleanField(
        null=True, blank=True, default=None,
        verbose_name='Може експортувати',
        help_text='Порожньо = за роллю',
    )
    can_import     = models.BooleanField(
        null=True, blank=True, default=None,
        verbose_name='Може імпортувати',
        help_text='Порожньо = за роллю',
    )
    can_view_audit = models.BooleanField(
        null=True, blank=True, default=None,
        verbose_name='Бачить журнал аудиту',
        help_text='Порожньо = за роллю',
    )
    can_manage_users = models.BooleanField(
        null=True, blank=True, default=None,
        verbose_name='Може керувати юзерами',
        help_text='Порожньо = за роллю',
    )

    denied_models  = models.JSONField(
        default=list, blank=True,
        verbose_name='Заблоковані моделі',
        help_text='Список "app_label:ModelName" — моделі заблоковані всередині дозволеного модуля.',
    )
    module_operations = models.JSONField(
        null=True, blank=True, default=None,
        verbose_name='Операції по модулях',
        help_text=(
            'None = авто (роль). '
            '{"crm": ["view","add","change"]} = явні операції для модуля. '
            '{} = повна заборона всіх операцій.'
        ),
    )

    # ── Персональні налаштування ──────────────────────────────
    notify_email       = models.BooleanField(default=True,  verbose_name='Email сповіщення')
    notify_telegram    = models.BooleanField(default=False, verbose_name='Telegram сповіщення')
    interface_language = models.CharField(
        max_length=5,
        choices=[('uk', 'Українська'), ('de', 'Deutsch'), ('en', 'English')],
        default='uk', verbose_name='Мова інтерфейсу',
    )
    items_per_page     = models.PositiveIntegerField(default=25, verbose_name='Записів на сторінку')
    theme              = models.CharField(
        max_length=20,
        choices=[('dark', 'Темна'), ('light', 'Світла'), ('minerva', 'Minerva'), ('auto', 'Авто')],
        default='dark', verbose_name='Тема',
    )
    created_at         = models.DateTimeField(auto_now_add=True, verbose_name='Дата створення')

    # ── Особиста пошта (IMAP) ─────────────────────────────────
    imap_enabled = models.BooleanField(
        default=False, verbose_name='IMAP увімкнено',
        help_text='Читати листи з особистого ящика і додавати в хронологію клієнтів.',
    )
    imap_host = models.CharField(
        max_length=255, blank=True, verbose_name='IMAP Host',
        help_text='ionos: imap.ionos.de | Gmail: imap.gmail.com',
    )
    imap_port = models.PositiveSmallIntegerField(
        default=993, verbose_name='IMAP Port',
    )
    imap_use_ssl = models.BooleanField(
        default=True, verbose_name='SSL (порт 993)',
    )
    imap_user = models.CharField(
        max_length=255, blank=True, verbose_name='IMAP Login',
        help_text='Зазвичай повна email-адреса.',
    )
    imap_password = models.CharField(
        max_length=255, blank=True, verbose_name='IMAP Password',
    )
    imap_sent_folder = models.CharField(
        max_length=100, blank=True, default='INBOX.Sent',
        verbose_name='Sent папка',
        help_text='ionos: INBOX.Sent | Gmail: [Gmail]/Sent Mail',
    )

    class Meta:
        verbose_name        = 'Профіль користувача'
        verbose_name_plural = 'Профілі користувачів'

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    def get_allowed_modules(self):
        """
        Priority (highest → lowest):
          1. allowed_modules is not None — explicit override
             - []    → only ALWAYS modules (full restriction)
             - [...] → exactly these modules
          2. bundle — assigned package
          3. role defaults (ROLE_PERMISSIONS)
        Returns '__all__' for superadmin/admin, or a list.
        """
        if self.role in (self.Role.SUPERADMIN, self.Role.ADMIN):
            return '__all__'
        # 1. Manual override (None = not set; [] = explicit empty)
        if self.allowed_modules is not None:
            return self.allowed_modules
        # 2. Bundle
        if self.bundle_id:
            try:
                return self.bundle.get_module_labels()
            except Exception:
                pass
        # 3. Role defaults
        from core.utils import ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS.get(self.role, {})
        return perms.get('modules', '__all__')

    def has_module_access(self, app_label: str) -> bool:
        ALWAYS = frozenset({'core', 'auth', 'faq', 'admin', 'authtoken', 'dashboard'})
        if app_label in ALWAYS:
            return True
        allowed = self.get_allowed_modules()
        if allowed == '__all__':
            return True
        return app_label in allowed

    def get_allowed_apps(self) -> list:
        modules = self.get_allowed_modules()
        if modules == '__all__':
            from core.models import ModuleRegistry
            return ModuleRegistry.get_active_apps()
        return list(modules)

    def get_allowed_operations(self, app_label: str):
        """
        Returns list of allowed operations for app_label, or '__all__'.
        Operations: view, add, change, delete, export, import

        Priority:
          1. superadmin/admin → '__all__'
          2. module_operations is not None → explicit per-app list ([] = deny all)
          3. ROLE_OPERATIONS defaults for this role
        """
        if self.role in (self.Role.SUPERADMIN, self.Role.ADMIN):
            return '__all__'
        if self.module_operations is not None:
            # Explicit override — app not in map means deny
            ops = self.module_operations.get(app_label)
            if ops is None:
                return ['view']  # default to view-only for unlisted apps
            return list(ops)
        # Role defaults
        from core.utils import ROLE_OPERATIONS
        role_ops = ROLE_OPERATIONS.get(self.role, {})
        if role_ops == '__all__':
            return '__all__'
        return list(role_ops.get(app_label, ['view']))


class ModuleBundle(models.Model):
    """
    Named bundle of modules — custom package that can be assigned to users.
    Core modules are ALWAYS included regardless of bundle contents.
    """

    name        = models.CharField(max_length=100, unique=True, verbose_name='Назва пакету')
    description = models.TextField(blank=True, verbose_name='Опис')
    color       = models.CharField(
        max_length=7, default='#58a6ff', blank=True,
        verbose_name='Колір', help_text='HEX, напр. #58a6ff',
    )
    is_system   = models.BooleanField(
        default=False, verbose_name='Системний',
        help_text='Системні пакети не можна видалити',
    )
    modules     = models.ManyToManyField(
        'ModuleRegistry', blank=True,
        verbose_name='Модулі',
        help_text='Core-модулі додаються автоматично незалежно від вибору',
    )

    class Meta:
        verbose_name        = 'Пакет модулів'
        verbose_name_plural = 'Пакети модулів'
        ordering            = ['name']

    def __str__(self):
        return self.name

    def get_module_labels(self) -> list:
        """Return list of app_labels in this bundle + all core modules."""
        bundle_labels = list(self.modules.values_list('app_label', flat=True))
        core_labels   = list(
            ModuleRegistry.objects.filter(tier=ModuleRegistry.Tier.CORE)
            .values_list('app_label', flat=True)
        )
        return list(set(bundle_labels + core_labels))


class ModuleRegistry(models.Model):
    """Registry of app modules — controls enable/disable per-installation."""

    class Tier(models.TextChoices):
        CORE     = 'core',     '🔒 Базовий (завжди увімкнено)'
        STANDARD = 'standard', '📦 Стандартний'
        PREMIUM  = 'premium',  '⭐ Преміум'

    app_label   = models.CharField(max_length=50, unique=True, verbose_name='App label')
    name        = models.CharField(max_length=100, verbose_name='Назва')
    description = models.TextField(blank=True, verbose_name='Опис')
    tier        = models.CharField(
        max_length=10, choices=Tier.choices, default=Tier.STANDARD, verbose_name='Тир',
    )
    is_active   = models.BooleanField(default=True, verbose_name='Активний')
    order       = models.PositiveSmallIntegerField(default=0, verbose_name='Порядок')

    class Meta:
        verbose_name        = 'Модуль'
        verbose_name_plural = 'Реєстр модулів'
        ordering            = ['order', 'app_label']

    def __str__(self):
        status = '✅' if self.is_active else '⛔'
        return f"{status} {self.name} ({self.app_label})"

    def save(self, *args, **kwargs):
        if self.tier == self.Tier.CORE:
            self.is_active = True
        super().save(*args, **kwargs)

    @classmethod
    def check_active(cls, app_label: str) -> bool:
        """Returns True if app is active (or not in registry — safe open default)."""
        try:
            obj = cls.objects.get(app_label=app_label)
            return obj.is_active
        except cls.DoesNotExist:
            return True
        except Exception:
            return True

    @classmethod
    def get_active_apps(cls) -> list:
        """Return list of active app_labels."""
        try:
            return list(cls.objects.filter(is_active=True).values_list('app_label', flat=True))
        except Exception:
            return []

    @classmethod
    def is_app_active(cls, app_label: str) -> bool:
        """Alias for check_active."""
        return cls.check_active(app_label)


class TenantAccount(models.Model):
    """
    Client account — each represents one Minerva installation / tenant.
    Vendor (superadmin) manages statuses and packages here.
    """

    STATUS_CHOICES = [
        ('pending',   'Очікує підтвердження email'),
        ('trial',     'Пробний період'),
        ('active',    'Активний'),
        ('suspended', 'Призупинений'),
        ('expired',   'Термін вийшов'),
        ('cancelled', 'Скасований'),
    ]

    PACKAGE_CHOICES = [
        ('free',     'Free — базові модулі'),
        ('starter',  'Starter — €30/міс'),
        ('business', 'Business — €60/міс'),
        ('custom',   'Custom — від €300'),
    ]

    # ── Статус і пакет ──────────────────────────────────────
    status  = models.CharField(
        max_length=20, choices=STATUS_CHOICES,
        default='pending', verbose_name='Статус', db_index=True)
    package = models.CharField(
        max_length=20, choices=PACKAGE_CHOICES,
        default='starter', verbose_name='Пакет')

    # ── Дати ────────────────────────────────────────────────
    trial_ends_at   = models.DateField(null=True, blank=True, verbose_name='Trial до')
    paid_until      = models.DateField(null=True, blank=True, verbose_name='Оплачено до')
    registered_at   = models.DateTimeField(auto_now_add=True, verbose_name='Дата реєстрації')
    activated_at    = models.DateTimeField(null=True, blank=True, verbose_name='Дата активації')
    last_login_at   = models.DateTimeField(null=True, blank=True, verbose_name='Остання активність')

    # ── Компанія ────────────────────────────────────────────
    company_name    = models.CharField(max_length=200, default='', verbose_name='Назва компанії')
    company_country = models.CharField(max_length=100, blank=True, default='DE', verbose_name='Країна')
    contact_phone   = models.CharField(max_length=50, blank=True, verbose_name='Телефон')

    # ── Власник ─────────────────────────────────────────────
    owner_email = models.EmailField(default='', verbose_name='Email власника')
    owner_name  = models.CharField(max_length=200, blank=True, verbose_name="Ім'я власника")
    owner_user  = models.OneToOneField(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='tenant_owner',
        verbose_name='Акаунт власника')

    # ── Активні модулі ───────────────────────────────────────
    active_modules = models.JSONField(default=list, blank=True, verbose_name='Активні модулі')

    # ── Email підтвердження ──────────────────────────────────
    email_verified       = models.BooleanField(default=False, verbose_name='Email підтверджено')
    verification_token   = models.CharField(max_length=64, blank=True, verbose_name='Токен підтвердження')
    verification_sent_at = models.DateTimeField(null=True, blank=True)

    # ── Нотатки вендора ──────────────────────────────────────
    notes = models.TextField(blank=True, verbose_name='Нотатки (тільки вендор бачить)')

    class Meta:
        verbose_name        = 'Акаунт клієнта'
        verbose_name_plural = 'Акаунти клієнтів'
        ordering            = ['-registered_at']

    def __str__(self):
        return f'{self.company_name} [{self.get_status_display()}]'

    # ── Business logic ───────────────────────────────────────

    def get_package_modules(self):
        """Modules included in the package."""
        PACKAGES = {
            'free':     ['crm', 'sales', 'inventory', 'dashboard', 'faq'],
            'starter':  ['crm', 'sales', 'inventory', 'shipping',
                         'labels_app', 'tasks', 'backup', 'dashboard', 'faq'],
            'business': ['crm', 'sales', 'inventory', 'shipping',
                         'labels_app', 'tasks', 'backup', 'dashboard', 'faq',
                         'strategy', 'accounting', 'autoimport'],
            'custom':   '__all__',
            'trial':    ['crm', 'sales', 'inventory', 'shipping', 'dashboard', 'faq'],
        }
        return PACKAGES.get(self.package, PACKAGES['starter'])

    def activate_modules(self):
        """Set active_modules from package definition."""
        modules = self.get_package_modules()
        if modules == '__all__':
            self.active_modules = ModuleRegistry.get_active_apps()
        else:
            active = set(ModuleRegistry.get_active_apps())
            self.active_modules = [m for m in modules if m in active]
        self.save(update_fields=['active_modules'])

    def is_trial_expired(self):
        if not self.trial_ends_at:
            return False
        return timezone.now().date() > self.trial_ends_at

    def is_paid_expired(self):
        if not self.paid_until:
            return True
        return timezone.now().date() > self.paid_until

    @property
    def is_access_allowed(self):
        if self.status == 'active':
            return not self.is_paid_expired()
        if self.status == 'trial':
            return not self.is_trial_expired()
        return False

    @property
    def days_until_expiry(self):
        end = self.paid_until or self.trial_ends_at
        if not end:
            return None
        return (end - timezone.now().date()).days
