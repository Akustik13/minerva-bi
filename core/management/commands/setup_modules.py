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


DEFAULT_BUNDLES = [
    # ── Tier-based (автоматично підхоплюють нові модулі при setup_modules) ──
    {
        'name': '⭐ Повний доступ',
        'color': '#c9a84c',
        'description': 'Всі модулі — для адміністраторів системи',
        'tiers': ('core', 'standard', 'premium'),
        'is_system': True,
    },
    {
        'name': '📦 Стандарт',
        'color': '#58a6ff',
        'description': 'CRM, Продажі, Склад, Доставка, Задачі — базовий пакет',
        'tiers': ('core', 'standard'),
        'is_system': True,
    },
    # ── Рольові пакети ──────────────────────────────────────────────────────
    {
        'name': '💼 Менеджер',
        'color': '#2196f3',
        'description': 'CRM, стратегії, продажі, доставка — для менеджерів з продажів',
        'app_labels': ['crm', 'strategy', 'sales', 'shipping'],
        'is_system': True,
    },
    {
        'name': '📦 Складник',
        'color': '#4caf50',
        'description': 'Склад та доставка — для складських працівників',
        'app_labels': ['inventory', 'shipping'],
        'is_system': True,
    },
    {
        'name': '💰 Бухгалтер',
        'color': '#ff9800',
        'description': 'Продажі, бухгалтерія, склад — для фінансового відділу',
        'app_labels': ['sales', 'accounting', 'inventory'],
        'is_system': True,
    },
    {
        'name': '🎯 CRM-фокус',
        'color': '#9c27b0',
        'description': 'Тільки CRM та стратегії — для аналітиків клієнтської бази',
        'app_labels': ['crm', 'strategy'],
        'is_system': True,
    },
    {
        'name': '🛒 Інтернет-магазин',
        'color': '#e91e63',
        'description': 'Продажі + CRM + Склад + Доставка — для e-commerce',
        'app_labels': ['crm', 'sales', 'inventory', 'shipping'],
        'is_system': True,
    },
    {
        'name': '🔒 Тільки ядро',
        'color': '#607d8b',
        'description': 'Мінімальний доступ — тільки базові системні модулі',
        'app_labels': [],
        'is_system': True,
    },
]


class Command(BaseCommand):
    help = 'Create/update ModuleRegistry entries and default bundles'

    def handle(self, *args, **options):
        from core.models import ModuleRegistry, ModuleBundle
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
                self.stdout.write(f"  [+] {m['name']} ({m['app_label']})")
            else:
                changed = False
                for field in ('name', 'tier', 'order'):
                    if getattr(obj, field) != m[field]:
                        setattr(obj, field, m[field])
                        changed = True
                if changed:
                    obj.save(update_fields=['name', 'tier', 'order'])
                    updated += 1
                    self.stdout.write(f"  [~] {m['name']}")

        self.stdout.write(self.style.SUCCESS(
            f'\nModules: {created} created, {updated} updated'
        ))

        # ── Default bundles ────────────────────────────────────────────────────
        self.stdout.write('\nBundles:')
        for b in DEFAULT_BUNDLES:
            bundle, b_created = ModuleBundle.objects.get_or_create(
                name=b['name'],
                defaults={
                    'color': b['color'],
                    'description': b['description'],
                    'is_system': b.get('is_system', False),
                },
            )
            # Resolve modules: by tier OR by explicit app_labels list
            if 'tiers' in b:
                mods = ModuleRegistry.objects.filter(tier__in=b['tiers'])
            else:
                labels = b.get('app_labels', [])
                mods = ModuleRegistry.objects.filter(app_label__in=labels) if labels else ModuleRegistry.objects.none()
            bundle.modules.set(mods)
            status = '[+]' if b_created else '[~]'
            safe_name = b['name'].encode('ascii', 'replace').decode()
            self.stdout.write(f"  {status} {safe_name} ({mods.count()} modules)")

        self.stdout.write(self.style.SUCCESS('\nDone.\n'))
