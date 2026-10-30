"""Збір українських написів інтерфейсу з коду Minerva — каталог для перекладу (core.models.UIText).

Джерела: шаблони (*.html: тексти між тегами, атрибути title / placeholder / aria-label / alt / value, рядки у
вбудованих <script>), Python (рядкові константи й f-рядки через ast; без docstring, логів, тестів, міграцій),
статичні JS. Кожен рядок ділиться на частини так само, як його побачить браузер: HTML-теги, теги шаблону й
місця вставок ({{ x }}, {}, {name}, %s, f-рядки, ${x}, ' + x + ') — межі; змінні частини всередині фрази —
{0}, {1}… Лишається лише те, де є кирилиця.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .text import edges

CYR = re.compile(r"[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ']")
LETTERS = re.compile(r"[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]")
MAX_LEN = 600                    # довші рядки — інструкції для AI, листи, а не написи інтерфейсу
SKIP_DIRS = {"venv", ".venv", "node_modules", "staticfiles", "media", ".git", ".claude", "__pycache__",
             "migrations", "locale", "docs", "integration", "knowledge_sources"}
SKIP_FILES = re.compile(r"^(tests?_.*|.*_tests?|conftest)\.py$|^tests\w*\.py$")
LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
ATTRS = ("title", "placeholder", "aria-label", "alt", "data-confirm", "data-title", "label")
VALUE_ATTR = re.compile(r"""<input\b[^>]*\btype=["'](?:submit|button|reset)["'][^>]*>""", re.I)

_DJANGO_TAG = re.compile(r"{%.*?%}|{#.*?#}", re.S)
_DJANGO_VAR = re.compile(r"{{.*?}}", re.S)
_HTML_TAG = re.compile(r"<[^>]*>")
_PLACEHOLDERS = re.compile(r"\{\d*\}|\{[A-Za-z_][\w.\[\]]*(?:![rsa])?(?::[^{}]*)?\}|%\([\w]+\)[sdifr]|%[sdifr]|\$\{[^}]*\}")
_ENTITY = re.compile(r"&(nbsp|middot|mdash|ndash|laquo|raquo|hellip|amp|#\d+);")
_ENTITIES = {"nbsp": " ", "middot": "·", "mdash": "—", "ndash": "–", "laquo": "«", "raquo": "»", "hellip": "…",
             "amp": "&"}
_SPLIT = "\x00"                  # межа частин
_PH = "\x01"                     # місце вставки всередині частини


def _entities(text: str) -> str:
    def one(m):
        name = m.group(1)
        if name.startswith("#"):
            try:
                return chr(int(name[1:]))
            except ValueError:
                return m.group(0)
        return _ENTITIES.get(name, m.group(0))
    return _ENTITY.sub(one, text)


def normalize(text: str) -> str:
    """Фраза, як її порівнює словник: пробіли зведені, місця вставок — {0}, {1}…"""
    text = " ".join((text or "").split())
    n = 0

    def ph(_):
        nonlocal n
        n += 1
        return "{%d}" % (n - 1)
    return re.sub(re.escape(_PH), ph, text).strip()


def _whole(text: str) -> str:
    """Емодзі, записані в коді як "\\ud83d\\udcc5", — пари половинок → один символ; зайві половинки — геть."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        try:
            return text.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeError:
            return text.encode("utf-8", "ignore").decode("utf-8")


def phrases(raw: str, html: bool = True) -> list[str]:
    """Рядок з коду → фрази, як їх побачить людина (без тегів, з {0} на місці змінних)."""
    if not raw or not CYR.search(raw):
        return []
    raw = _whole(raw)
    text = _DJANGO_TAG.sub(_SPLIT, raw)
    text = _DJANGO_VAR.sub(_PH, text)
    if html:
        text = _HTML_TAG.sub(_SPLIT, text)
    text = _PLACEHOLDERS.sub(_PH, text)
    text = _entities(text)
    out = []
    for segment in text.split(_SPLIT):
        # перенос рядка: у HTML це пробіл (одна фраза), в alert / confirm — окремі рядки; беремо обидва
        parts = re.split(r"[\n\r\t]+|\s{3,}", segment)
        if len(parts) > 1:
            parts.append(segment)
        for part in parts:
            if not LETTERS.search(part):
                continue
            _, core, _ = edges(normalize(part))    # емодзі, значки, «:», тире по краях — не частина фрази
            if 1 < len(core) <= MAX_LEN and LETTERS.search(re.sub(r"\{\d+\}", "", core)):
                out.append(_renumber(core))
    return out


def _renumber(phrase: str) -> str:
    n = iter(range(1000))
    return re.sub(r"\{\d+\}", lambda m: "{%d}" % next(n), phrase)


# ── шаблони ──

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_JS_STRING = re.compile(r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`""", re.S)
_ATTR_RE = re.compile(r"""\b(%s)\s*=\s*(?:"([^"]*)"|'([^']*)')""" % "|".join(re.escape(a) for a in ATTRS), re.I)


def _js_literals(code: str) -> list[str]:
    out = []
    for m in _JS_STRING.finditer(code):
        s = m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None else m.group(3)
        if s and CYR.search(s):
            s = s.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")
            out.extend(phrases(s))
    return out


def from_template(text: str) -> list[str]:
    out = []
    for m in _SCRIPT.finditer(text):
        if "application/json" in m.group(1).lower() or "src=" in m.group(1).lower():
            continue
        out.extend(_js_literals(_DJANGO_TAG.sub(" ", m.group(2))))
    body = _STYLE.sub(" ", _SCRIPT.sub(" ", text))
    for m in _ATTR_RE.finditer(body):
        out.extend(phrases(m.group(2) if m.group(2) is not None else m.group(3), html=False))
    for m in VALUE_ATTR.finditer(body):
        v = re.search(r"""\bvalue=["']([^"']*)["']""", m.group(0))
        if v:
            out.extend(phrases(v.group(1), html=False))
    body = _ATTR_RE.sub(" ", body)
    out.extend(phrases(body))
    return out


# ── Python ──

def _fstring(node: ast.JoinedStr) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        else:
            parts.append(_PH)
    return "".join(parts)


def from_python(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    skip = set()
    for node in ast.walk(tree):
        # docstring
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        # logger.warning("…") / print("…") — не інтерфейс
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            if name == "print" or (name in LOG_CALLS and owner in ("logger", "log", "logging", "_log", "LOG")):
                for a in node.args:
                    for sub in ast.walk(a):
                        skip.add(id(sub))
    out = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                skip.add(id(v))
            out.extend(phrases(_fstring(node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and CYR.search(node.value):
            out.extend(phrases(node.value))
    return out


# ── обхід проєкту ──

def _files(base: Path, pattern: str):
    for p in base.rglob(pattern):
        rel = p.relative_to(base).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        yield p


def collect(base_dir) -> dict:
    """{фраза: {"where": "шлях", "count": скільки разів}} з усього проєкту."""
    base = Path(base_dir)
    found: dict[str, dict] = {}

    def add(items, path):
        for ph in items:
            row = found.setdefault(ph, {"where": str(path.relative_to(base)).replace("\\", "/"), "count": 0})
            row["count"] += 1
    for p in _files(base, "*.html"):
        add(from_template(p.read_text(encoding="utf-8-sig", errors="ignore")), p)
    for p in _files(base, "*.py"):
        if SKIP_FILES.match(p.name) or p.name == "settings.py":
            continue
        add(from_python(p.read_text(encoding="utf-8-sig", errors="ignore")), p)
    for p in _files(base, "*.js"):
        if p.name.endswith(".min.js") or p.name == "i18n_minerva.js":
            continue
        add(_js_literals(p.read_text(encoding="utf-8-sig", errors="ignore")), p)
    return found
