"""Перевірка JSON-схем бази знань Minerva і синтетичних прикладів.

    python api/schemas/knowledge/validate.py        (потрібен jsonschema>=4.18, див. requirements-dev.txt)

examples/valid/<схема>__<випадок>.json   — мають пройти і схему, і семантичні правила;
examples/invalid/<схема>__<випадок>.json — мають НЕ пройти (схему або семантику).
<схема> — ім'я файлу без .schema.json, напр. changes.response.

Семантичні правила — те, що JSON Schema не виражає: порядок seq, унікальність документа на сторінці,
збіг ревізій/джерела між подією і документом, v1-правило text_new_mode=full тощо.
Ті самі функції використовують тести Minerva для перевірки реальних відповідей API.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
TEXT_NEW_MAX = 20000


def load_registry():
    from referencing import Registry, Resource
    schemas, resources = {}, []
    for path in sorted(HERE.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.name[: -len(".schema.json")]] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return schemas, Registry().with_resources(resources)


_CACHE = {}


def validator(name):
    from jsonschema import Draft202012Validator, FormatChecker
    if not _CACHE:
        _CACHE["schemas"], _CACHE["registry"] = load_registry()
    schema = _CACHE["schemas"][name]
    return Draft202012Validator(schema, registry=_CACHE["registry"], format_checker=FormatChecker())


def schema_errors(name, data) -> list:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in sorted(validator(name).iter_errors(data), key=lambda e: list(e.absolute_path))]


def _document_semantics(doc, where="document") -> list:
    errs = []
    kind, _, pk = doc["document_id"].partition(":")
    if kind != doc["document_type"]:
        errs.append(f"{where}: document_id не відповідає document_type")
    if doc["url"] != f"/email/message/{pk}/":
        errs.append(f"{where}: url листа не відповідає document_id")
    if doc["text_new_mode"] == "full":
        if doc["text_new"] != doc["text"][:TEXT_NEW_MAX]:
            errs.append(f"{where}: у v1 (text_new_mode=full) text_new = text[:20000]")
        if doc["text_new_truncated"] != (len(doc["text"]) > TEXT_NEW_MAX):
            errs.append(f"{where}: text_new_truncated не відповідає довжині тексту")
    elif doc["text_new_mode"] == "reply_trimmed":
        errs.append(f"{where}: reply_trimmed зарезервовано, у v1 не використовується")
    indexes = [a["index"] for a in doc["attachments"]]
    if len(set(indexes)) != len(indexes):
        errs.append(f"{where}: індекси вкладень повторюються")
    for a in doc["attachments"]:
        if a.get("url"):
            expected = f"/api/v1/knowledge/documents/{doc['document_id']}/attachments/{a['index']}/" \
                       f"?revision={doc['document_revision']}"
            if a["url"] != expected:
                errs.append(f"{where}: url вкладення {a['index']} не прив'язаний до документа/ревізії")
    if doc["mailbox"]["id"] != doc["access"]["mailbox_id"]:
        errs.append(f"{where}: access.mailbox_id ≠ mailbox.id")
    return errs


def semantic_errors(name, data) -> list:
    errs = []
    if name == "document":
        errs += _document_semantics(data)
    elif name == "changes.response":
        items = data["items"]
        seqs = [i["seq"] for i in items]
        if any(b <= a for a, b in zip(seqs, seqs[1:])):
            errs.append("seq подій не зростає строго")
        ids = [i["document_id"] for i in items]
        if len(set(ids)) != len(ids):
            errs.append("document_id повторюється на одній сторінці")
        if items and data["next_after"] != seqs[-1]:
            errs.append("next_after ≠ seq останньої події сторінки")
        if data["next_after"] > data["last_seq"]:
            errs.append("next_after > last_seq")
        if data["has_more"] and not items:
            errs.append("has_more без подій")
        for i in items:
            if i["action"] != "upsert":
                continue
            d = i["document"]
            where = f"seq {i['seq']}"
            if d["document_id"] != i["document_id"] or d["document_type"] != i["document_type"]:
                errs.append(f"{where}: документ не відповідає події")
            if d["document_revision"] != i["document_revision"]:
                errs.append(f"{where}: document_revision події ≠ ревізії документа")
            if d["source_instance_id"] != data["source_instance_id"] or d["generation"] != data["generation"]:
                errs.append(f"{where}: джерело/покоління документа ≠ стрічки")
            errs += _document_semantics(d, where)
    elif name == "snapshot.page.response":
        ids = [i["document_id"] for i in data["items"]]
        if len(set(ids)) != len(ids):
            errs.append("document_id повторюється в знімку")
    elif name == "status.request":
        keys = [(i["document_id"], i["attempt_id"]) for i in data["items"]]
        if len(set(keys)) != len(keys):
            errs.append("пара (document_id, attempt_id) повторюється в одному запиті")
    elif name == "status.response":
        if data["accepted"] != sum(r["result"] == "accepted" for r in data["results"]):
            errs.append("accepted ≠ кількості результатів accepted")
    elif name == "summary.response":
        d = data["documents"]
        if d["indexed"] + d["failed"] + d["skipped"] + d["pending"] != d["eligible"]:
            errs.append("indexed + failed + skipped + pending ≠ eligible")
    return errs


def errors(name, data) -> list:
    """Усі порушення: спершу схема; семантика — лише для схемно коректних даних."""
    found = schema_errors(name, data)
    return found or semantic_errors(name, data)


def check_all() -> list:
    from jsonschema import Draft202012Validator
    problems = []
    schemas, _ = load_registry()
    for name, schema in schemas.items():
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:  # noqa: BLE001
            problems.append(f"схема {name}: {e}")
    for kind in ("valid", "invalid"):
        for path in sorted((HERE / "examples" / kind).glob("*.json")):
            name = path.name.split("__", 1)[0]
            if name not in schemas:
                problems.append(f"{kind}/{path.name}: немає схеми {name}")
                continue
            found = errors(name, json.loads(path.read_text(encoding="utf-8")))
            if kind == "valid" and found:
                problems.append(f"valid/{path.name}: {found[0]}")
            if kind == "invalid" and not found:
                problems.append(f"invalid/{path.name}: пройшов перевірку, хоча мав упасти")
    return problems


if __name__ == "__main__":
    problems = check_all()
    total = sum(1 for _ in (HERE / "examples").rglob("*.json"))
    for p in problems:
        print("✗", p)
    print(f"{'OK' if not problems else 'FAILED'}: {total} прикладів, {len(problems)} проблем")
    sys.exit(1 if problems else 0)
