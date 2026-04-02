from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Ініціалізувати системні налаштування Minerva'

    def handle(self, *args, **options):
        import os
        from config.models import SystemSettings

        obj = SystemSettings.get()

        # Оновити з env якщо є
        changed = False
        site_domain = os.getenv('SITE_DOMAIN', '')
        if site_domain and obj.site_domain == 'localhost:8000':
            obj.site_domain = site_domain
            changed = True

        company_name = os.getenv('COMPANY_NAME', '')
        if company_name and obj.company_name == 'Моя компанія':
            obj.company_name = company_name
            changed = True

        if changed:
            obj.save()

        # Оновити Sites
        try:
            from django.contrib.sites.models import Site
            site = Site.objects.get_current()
            site.domain = obj.site_domain
            site.name   = obj.company_name or 'Minerva BI'
            site.save()
            self.stdout.write(f'  Sites: domain={site.domain}')
        except Exception as e:
            self.stdout.write(f'  Sites: {e}')

        # Вивести таблицю
        self.stdout.write('\n' + '=' * 50)
        self.stdout.write('  Minerva — Системні налаштування')
        self.stdout.write('=' * 50)
        rows = [
            ('Компанія',  obj.company_name),
            ('Домен',     obj.site_domain),
            ('Протокол',  obj.site_protocol),
            ('Валюта',    obj.default_currency),
            ('Timezone',  obj.timezone),
            ('Пакет',     obj.get_license_package_display()),
            ('Email',     obj.company_email or '— не вказано'),
        ]
        for label, value in rows:
            self.stdout.write(f'  {label:<14} {value}')
        self.stdout.write('=' * 50)
        self.stdout.write(
            self.style.SUCCESS(
                '\n✅ Готово. Відкрий /admin/config/systemsettings/ для налаштування.\n'
            )
        )
