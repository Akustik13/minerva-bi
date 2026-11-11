"""Перші категорії постачальників (людина потім перейменовує / додає свої). Наявні з тією самою назвою не чіпаються."""
from django.db import migrations

CATEGORIES = [
    ("🔌", "Виробники кабелів", "#e8a33d", 10, "cable"),
    ("🟩", "Виробники плат", "#3daa5b", 20, "pcb"),
    ("🔧", "Складання плат (PCBA)", "#2f8fbf", 30, "pcba"),
    ("🔩", "Закупка компонентів", "#8a6bd1", 40, "components"),
    ("⚙️", "Механіка й корпуси", "#7b8794", 50, "mechanical"),
]


def seed(apps, schema_editor):
    Category = apps.get_model("suppliers", "SupplierCategory")
    for icon, name, color, order, template in CATEGORIES:
        Category.objects.get_or_create(name=name, defaults={"icon": icon, "color": color, "order": order,
                                                            "request_template": template})


class Migration(migrations.Migration):
    dependencies = [("suppliers", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
