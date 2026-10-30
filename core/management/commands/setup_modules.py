"""
python manage.py setup_modules

Creates/updates ModuleRegistry entries for all Minerva modules (list — core/modules.py).
Safe to re-run: does not change is_active for existing entries.
New modules are also registered automatically after every `migrate` (core/apps.py → post_migrate).
"""
from django.core.management.base import BaseCommand

from core.modules import DEFAULT_BUNDLES, MODULES  # noqa: F401 — перелік модулів і пакетів (імпортують і звідси)


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
