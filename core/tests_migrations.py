"""
Ланцюжок міграцій: нова база з нуля має створюватися без помилок.

Решта тестів працює без міграцій (settings_test), тому тут вони завантажуються явно, без БД:
- жодна операція з базою не додає стовпець чи модель, які вже є (SQLite такий дубль ховає перебудовою таблиці,
  а PostgreSQL падає «column already exists»);
- моделі збігаються з міграціями (те саме, що `makemigrations --check`).
    python manage.py test core --settings=tabele.settings_test
"""
from django.apps import apps
from django.db import migrations
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase, override_settings
from django.utils import translation


def _database_operations(op):
    """Операції, які справді виконуються в базі (для SeparateDatabaseAndState — лише database_operations)."""
    if isinstance(op, migrations.SeparateDatabaseAndState):
        for sub in op.database_operations:
            yield from _database_operations(sub)
    else:
        yield op


@override_settings(MIGRATION_MODULES={})
class MigrationChainTests(SimpleTestCase):
    def setUp(self):
        self.loader = MigrationLoader(None, ignore_no_migrations=True)

    def _plan(self):
        plan, seen = [], set()
        for leaf in sorted(self.loader.graph.leaf_nodes()):
            for node in self.loader.graph.forwards_plan(leaf):
                if node not in seen:
                    seen.add(node)
                    plan.append(node)
        return plan

    def test_no_duplicate_columns_for_fresh_database(self):
        db_state = ProjectState(real_apps=self.loader.unmigrated_apps)
        duplicates = []
        for app_label, name in self._plan():
            for top in self.loader.graph.nodes[(app_label, name)].operations:
                for op in _database_operations(top):
                    if isinstance(op, migrations.AddField):
                        model = db_state.models.get((app_label, op.model_name_lower))
                        if model is not None and op.name in model.fields:
                            duplicates.append(f"{app_label}.{name}: {op.model_name}.{op.name}")
                    elif isinstance(op, migrations.CreateModel) and (app_label, op.name_lower) in db_state.models:
                        duplicates.append(f"{app_label}.{name}: {op.name}")
                    op.state_forwards(app_label, db_state)
        self.assertEqual(duplicates, [], "повторне додавання в базі — нова PostgreSQL-база впаде на migrate")

    def test_models_match_migrations(self):
        with translation.override(None):   # як makemigrations: підписи порівнюються без перекладу
            changes = MigrationAutodetector(self.loader.project_state(), ProjectState.from_apps(apps)).changes(
                graph=self.loader.graph)
        missing = {app: [op.describe() for mig in migs for op in mig.operations] for app, migs in changes.items()}
        self.assertEqual(missing, {}, "моделі змінено без міграції — потрібен makemigrations")
