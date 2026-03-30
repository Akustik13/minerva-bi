"""
python manage.py setup_roles

Creates UserProfile for every User that doesn't have one.
Assigns superadmin role to is_superuser=True users, admin otherwise.
Safe to re-run.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create UserProfile for users who do not have one'

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        from core.models import UserProfile
        User = get_user_model()
        created = 0

        for user in User.objects.all():
            role = UserProfile.Role.SUPERADMIN if user.is_superuser else UserProfile.Role.ADMIN
            _, was_created = UserProfile.objects.get_or_create(
                user=user,
                defaults={'role': role},
            )
            if was_created:
                created += 1
                self.stdout.write(f'  ✅ {user.username} → {role}')

        self.stdout.write(self.style.SUCCESS(
            f'\n✅ Готово: {created} профіль(ів) створено\n'
        ))
