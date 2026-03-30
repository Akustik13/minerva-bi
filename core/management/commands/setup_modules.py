"""
python manage.py setup_modules

Creates/updates ModuleRegistry entries for all Minerva modules.
Safe to re-run: does not change is_active for existing entries.
"""
from django.core.management.base import BaseCommand

MODULES = [
    {'app_label': 'core',       'name': 'Ядро системи',         'tier': 'core',     'order': 0},
    {'app_label': 'config',     'name': 'Налаштування',         'tier': 'core',     'order': 1},
    {'app_label': 'auth',       'name': 'Авторизація',          'tier': 'core',     'order': 2},
    {'app_label': 'crm',        'name': 'CRM',                  'tier': 'standard', 'order': 10},
    {'app_label': 'strategy',   'name': 'Стратегії CRM',        'tier': 'standard', 'order': 11},
    {'app_label': 'sales',      'name': 'Продажі',              'tier': 'standard', 'order': 20},
    {'app_label': 'accounting', 'name': 'Бухгалтерія',          'tier': 'standard', 'order': 21},
    {'app_label': 'shipping',   'name': 'Доставка',             'tier': 'standard', 'order': 30},
    {'app_label': 'inventory',  'name': 'Склад',                'tier': 'standard', 'order': 40},
    {'app_label': 'tasks',      'name': 'Задачі',               'tier': 'standard', 'order': 50},
    {'app_label': 'bots',       'name': 'AI та Боти',           'tier': 'premium',  'order': 60},
    {'app_label': 'api',        'name': 'REST API',             'tier': 'premium',  'order': 61},
    {'app_label': 'autoimport', 'name': 'Авто-імпорт',         'tier': 'premium',  'order': 62},
    {'app_label': 'backup',     'name': 'Резервне копіювання',  'tier': 'standard', 'order': 70},
]


class Command(BaseCommand):
    help = 'Create/update ModuleRegistry entries for all Minerva modules'

    def handle(self, *args, **options):
        from core.models import ModuleRegistry
        created = updated = 0

        for m in MODULES:
            obj, was_created = ModuleRegistry.objects.get_or_create(
                app_label=m['app_label'],
                defaults={
                    'name': m['name'],
                    'tier': m['tier'],
                    'order': m['order'],
                    'is_active': True,
                },
            )
            if was_created:
                created += 1
                self.stdout.write(f"  ✅ Створено: {m['name']} ({m['app_label']})")
            else:
                changed = False
                for field in ('name', 'tier', 'order'):
                    if getattr(obj, field) != m[field]:
                        setattr(obj, field, m[field])
                        changed = True
                if changed:
                    obj.save(update_fields=['name', 'tier', 'order'])
                    updated += 1
                    self.stdout.write(f"  🔄 Оновлено: {m['name']}")

        self.stdout.write(self.style.SUCCESS(
            f'\n✅ Готово: {created} створено, {updated} оновлено\n'
        ))
