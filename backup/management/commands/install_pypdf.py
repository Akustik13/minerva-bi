import subprocess
import sys
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Install pypdf package via pip (needed for Print-All PDF merge)"

    def handle(self, *args, **options):
        self.stdout.write("Installing pypdf>=4.0 ...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pypdf>=4.0"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            self.stdout.write(self.style.SUCCESS("✅ pypdf installed successfully"))
            self.stdout.write(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
        else:
            self.stdout.write(self.style.ERROR("❌ pip install failed"))
            self.stdout.write(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
