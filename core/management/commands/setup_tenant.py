"""
python manage.py setup_tenant [--name "Company"] [--slug company] [--plan starter]

Stub: creates a default TenantAccount if none exists.
Run once after initial deployment.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create default TenantAccount (stub for future multi-tenant support)'

    def add_arguments(self, parser):
        parser.add_argument('--name', default='Default', help='Company name')
        parser.add_argument('--slug', default='default', help='URL-safe slug')
        parser.add_argument('--plan', default='trial', help='Plan: trial/starter/pro/custom')

    def handle(self, *args, **options):
        from core.models import TenantAccount
        tenant, created = TenantAccount.objects.get_or_create(
            slug=options['slug'],
            defaults={
                'name': options['name'],
                'plan': options['plan'],
                'is_active': True,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(
                f"[+] TenantAccount created: {tenant.name} ({tenant.slug}, {tenant.plan})"
            ))
        else:
            self.stdout.write(f"[=] TenantAccount already exists: {tenant.name}")
