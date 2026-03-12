from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from inventory.models import Location

class Command(BaseCommand):
    help = "Create default location and admin user (dev only)."

    def handle(self, *args, **options):
        Location.objects.get_or_create(code="MAIN", defaults={"name": "Main stock"})
        User = get_user_model()
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", email="", password="admin12345")
            self.stdout.write(self.style.SUCCESS("Created superuser admin / admin12345"))
        else:
            self.stdout.write("Superuser already exists")
