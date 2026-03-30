from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

User = get_user_model()


class AuditLog(models.Model):
    """Immutable audit trail for all significant actions."""

    class Action(models.TextChoices):
        LOGIN   = 'login',   'Вхід'
        LOGOUT  = 'logout',  'Вихід'
        CREATE  = 'create',  'Створення'
        UPDATE  = 'update',  'Зміна'
        DELETE  = 'delete',  'Видалення'
        VIEW    = 'view',    'Перегляд'
        EXPORT  = 'export',  'Експорт'
        IMPORT  = 'import',  'Імпорт'

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


class UserProfile(models.Model):
    """Extended profile: role + AI settings + optional module overrides."""

    class Role(models.TextChoices):
        SUPERADMIN = 'superadmin', '👑 Суперадмін'
        ADMIN      = 'admin',      '🔑 Адміністратор'
        MANAGER    = 'manager',    '💼 Менеджер'
        WAREHOUSE  = 'warehouse',  '📦 Складник'
        ACCOUNTANT = 'accountant', '💰 Бухгалтер'
        AI         = 'ai',         '🤖 AI-асистент'

    user             = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='profile', verbose_name='Користувач',
    )
    role             = models.CharField(
        max_length=20, choices=Role.choices, default=Role.ADMIN, verbose_name='Роль',
    )
    notes            = models.TextField(blank=True, verbose_name='Нотатки')

    # AI assistant settings
    ai_enabled       = models.BooleanField(default=False, verbose_name='AI увімкнено')
    ai_model         = models.CharField(
        max_length=60, default='claude-sonnet-4-6', verbose_name='AI модель',
    )
    ai_system_prompt = models.TextField(blank=True, verbose_name='Системний промпт')
    ai_temperature   = models.FloatField(default=0.7, verbose_name='Температура AI')

    # Override per-user module access (empty list = use role defaults)
    allowed_modules  = models.JSONField(
        default=list, blank=True,
        verbose_name='Дозволені модулі',
        help_text='Залиште порожнім щоб використовувати дефолти ролі',
    )

    class Meta:
        verbose_name        = 'Профіль користувача'
        verbose_name_plural = 'Профілі користувачів'

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    def get_allowed_modules(self):
        """Return list of allowed app_labels, or '__all__' for superadmin."""
        if self.allowed_modules:
            return self.allowed_modules
        from core.utils import ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS.get(self.role, {})
        modules = perms.get('modules', '__all__')
        return modules


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
