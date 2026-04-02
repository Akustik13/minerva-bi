"""
Migration 0004: Replace TenantAccount stub with full client account model.
Old fields: name, slug, plan, is_active, owner, created_at
New fields: status, package, dates, company_*, owner_*, active_modules, email verification, notes
"""
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_userprofile_can_delete_userprofile_can_export_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Remove old fields
        migrations.RemoveField(model_name='tenantaccount', name='name'),
        migrations.RemoveField(model_name='tenantaccount', name='slug'),
        migrations.RemoveField(model_name='tenantaccount', name='plan'),
        migrations.RemoveField(model_name='tenantaccount', name='is_active'),
        migrations.RemoveField(model_name='tenantaccount', name='owner'),
        migrations.RemoveField(model_name='tenantaccount', name='created_at'),

        # Add new fields
        migrations.AddField(
            model_name='tenantaccount',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending',   'Очікує підтвердження email'),
                    ('trial',     'Пробний період'),
                    ('active',    'Активний'),
                    ('suspended', 'Призупинений'),
                    ('expired',   'Термін вийшов'),
                    ('cancelled', 'Скасований'),
                ],
                default='pending', db_index=True, max_length=20, verbose_name='Статус'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='package',
            field=models.CharField(
                choices=[
                    ('free',     'Free — базові модулі'),
                    ('starter',  'Starter — €30/міс'),
                    ('business', 'Business — €60/міс'),
                    ('custom',   'Custom — від €300'),
                ],
                default='starter', max_length=20, verbose_name='Пакет'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='trial_ends_at',
            field=models.DateField(blank=True, null=True, verbose_name='Trial до'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='paid_until',
            field=models.DateField(blank=True, null=True, verbose_name='Оплачено до'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='registered_at',
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                verbose_name='Дата реєстрації'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='activated_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Дата активації'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='last_login_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Остання активність'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='company_name',
            field=models.CharField(default='', max_length=200, verbose_name='Назва компанії'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='company_country',
            field=models.CharField(blank=True, default='DE', max_length=100, verbose_name='Країна'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='contact_phone',
            field=models.CharField(blank=True, max_length=50, verbose_name='Телефон'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='owner_email',
            field=models.EmailField(default='', max_length=254, verbose_name='Email власника'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='owner_name',
            field=models.CharField(blank=True, max_length=200, verbose_name="Ім'я власника"),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='owner_user',
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='tenant_owner',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Акаунт власника'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='active_modules',
            field=models.JSONField(blank=True, default=list, verbose_name='Активні модулі'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='email_verified',
            field=models.BooleanField(default=False, verbose_name='Email підтверджено'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='verification_token',
            field=models.CharField(blank=True, max_length=64, verbose_name='Токен підтвердження'),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='verification_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenantaccount',
            name='notes',
            field=models.TextField(blank=True, verbose_name='Нотатки (тільки вендор бачить)'),
        ),

        # Update meta
        migrations.AlterModelOptions(
            name='tenantaccount',
            options={
                'verbose_name': 'Акаунт клієнта',
                'verbose_name_plural': 'Акаунти клієнтів',
                'ordering': ['-registered_at'],
            },
        ),

        # Add Action.SETTINGS to AuditLog
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('login', 'Вхід'), ('logout', 'Вихід'),
                    ('create', 'Створення'), ('update', 'Зміна'),
                    ('delete', 'Видалення'), ('view', 'Перегляд'),
                    ('export', 'Експорт'), ('import', 'Імпорт'),
                    ('settings', 'Налаштування'),
                ],
                max_length=10, verbose_name='Дія'),
        ),
    ]
