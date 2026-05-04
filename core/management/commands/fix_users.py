"""core/management/commands/fix_users.py — Repair user profiles."""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

User = get_user_model()


class Command(BaseCommand):
    help = 'Fix user profiles: create missing, set is_staff, fill allowed_modules.'

    def handle(self, *args, **options):
        from core.models import UserProfile
        from core.utils import apply_role_defaults

        rows = []

        for user in User.objects.all().order_by('username'):
            action_parts = []
            profile = None

            # 1. Create missing profiles
            try:
                profile = user.profile
            except UserProfile.DoesNotExist:
                role = UserProfile.Role.SUPERADMIN if user.is_superuser else UserProfile.Role.MANAGER
                profile = UserProfile.objects.create(user=user, role=role)
                action_parts.append(f'created profile (role={role})')

            # 2. Ensure is_staff=True
            if not user.is_staff:
                User.objects.filter(pk=user.pk).update(is_staff=True)
                action_parts.append('set is_staff=True')

            # 3. Fill empty allowed_modules
            if profile.allowed_modules == [] or profile.allowed_modules is None:
                apply_role_defaults(profile)
                profile.save(update_fields=['allowed_modules'])
                action_parts.append(f'filled modules ({len(profile.allowed_modules)})')

            mods_count = len(profile.allowed_modules) if isinstance(profile.allowed_modules, list) else '?'
            action = ', '.join(action_parts) if action_parts else 'ok'
            rows.append((user.username, profile.role, user.is_staff, mods_count, action))

        # Print table
        self.stdout.write('')
        self.stdout.write(
            f'{"Username":<22} {"Role":<12} {"Staff":<6} {"Mods":<5} {"Action"}'
        )
        self.stdout.write('-' * 70)
        for username, role, is_staff, mods, action in rows:
            staff_str = 'yes' if is_staff else 'no'
            self.stdout.write(f'{username:<22} {role:<12} {staff_str:<6} {str(mods):<5} {action}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Done. Processed {len(rows)} users.'))
