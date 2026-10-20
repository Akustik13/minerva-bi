"""JSON-схеми бази знань (api/schemas/knowledge): схеми коректні, приклади valid проходять, invalid — падають."""
import importlib.util
import pathlib
import unittest

from django.test import SimpleTestCase

HERE = pathlib.Path(__file__).resolve().parent / "schemas" / "knowledge"


def load_validate():
    spec = importlib.util.spec_from_file_location("knowledge_schema_validate", HERE / "validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    import jsonschema  # noqa: F401
    HAS_JSONSCHEMA = True
except ImportError:   # production-образ без dev-залежностей
    HAS_JSONSCHEMA = False


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema не встановлено (requirements-dev.txt)")
class KnowledgeSchemaExamplesTests(SimpleTestCase):
    def test_schemas_and_examples(self):
        problems = load_validate().check_all()
        self.assertEqual(problems, [])

    def test_examples_exist_for_every_schema(self):
        v = load_validate()
        schemas, _ = v.load_registry()
        with_examples = {p.name.split("__", 1)[0] for p in (HERE / "examples").rglob("*.json")}
        self.assertEqual(set(schemas) - {"common"} - with_examples, set())
