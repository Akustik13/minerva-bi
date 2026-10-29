"""
📚 Гід по Minerva — довідник для людей: docs/guide/<розділ>/<сторінка>.md (Markdown із шапкою front-matter).

Розділ — тека з _section.md (title, icon, order + вступ). Сторінка — шапка між рядками «---»:
    title, order, summary, where (шлях у меню), urls (адреси Minerva через кому), roles, keywords, related (slug-и).
Slug сторінки — «<розділ>/<файл без .md>», адреса — /guide/<slug>/. Правила оформлення — docs/guide/README.md,
перевірка посилань і шапок — check.py (manage.py guide_check, тест guide/tests.py).
Сторінки читаються з диска й кешуються в пам'яті процесу, доки не зміниться жоден файл (час зміни).
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

LIST_FIELDS = {"urls", "roles", "keywords", "related"}
_cache = {"stamp": None, "sections": []}


@dataclass
class Page:
    slug: str
    section: str
    title: str
    body: str
    path: str
    order: int = 100
    summary: str = ""
    where: str = ""
    urls: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    related: list = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/guide/{self.slug}/"


@dataclass
class Section:
    key: str
    title: str
    icon: str = "📄"
    order: int = 100
    intro: str = ""
    pages: list = field(default_factory=list)


def guide_dir() -> Path:
    return Path(getattr(settings, "GUIDE_DIR", None) or Path(settings.BASE_DIR) / "docs" / "guide")


def parse(text: str):
    """Шапка front-matter → (dict, тіло). Списки — через кому."""
    text = text.lstrip("﻿")
    meta, body = {}, text
    if text.startswith("---"):
        end = re.search(r"^---\s*$", text[3:], re.M)
        if end:
            head, body = text[3:3 + end.start()], text[3 + end.end():]
            for line in head.splitlines():
                if ":" not in line or not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, value = line.split(":", 1)
                key, value = key.strip(), value.strip()
                meta[key] = [v.strip() for v in value.split(",") if v.strip()] if key in LIST_FIELDS else value
    return meta, body.lstrip("\n")


def _int(value, default=100):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stamp(root: Path):
    stamp = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".md"):
                full = os.path.join(dirpath, name)
                stamp.append((full, os.stat(full).st_mtime_ns))
    return tuple(sorted(stamp))


def load() -> list:
    """Розділи зі сторінками в порядку order (кеш до зміни файлів)."""
    root = guide_dir()
    if not root.is_dir():
        return []
    stamp = (str(root), _stamp(root))
    if _cache["stamp"] == stamp:
        return _cache["sections"]
    sections = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        meta, intro = ({}, "")
        head = folder / "_section.md"
        if head.is_file():
            meta, intro = parse(head.read_text(encoding="utf-8"))
        section = Section(key=folder.name, title=meta.get("title") or folder.name, icon=meta.get("icon") or "📄",
                          order=_int(meta.get("order")), intro=intro.strip())
        for file in sorted(folder.glob("*.md")):
            if file.name.startswith("_"):
                continue
            pmeta, body = parse(file.read_text(encoding="utf-8"))
            section.pages.append(Page(
                slug=f"{folder.name}/{file.stem}", section=folder.name, title=pmeta.get("title") or file.stem,
                body=body, path=str(file.relative_to(root.parent.parent)).replace(os.sep, "/"),
                order=_int(pmeta.get("order")), summary=pmeta.get("summary", ""), where=pmeta.get("where", ""),
                urls=pmeta.get("urls", []), roles=pmeta.get("roles", []), keywords=pmeta.get("keywords", []),
                related=pmeta.get("related", [])))
        section.pages.sort(key=lambda p: (p.order, p.title))
        sections.append(section)
    sections.sort(key=lambda s: (s.order, s.title))
    _cache.update(stamp=stamp, sections=sections)
    return sections


def pages() -> list:
    return [p for s in load() for p in s.pages]


def page(slug: str):
    return next((p for p in pages() if p.slug == slug), None)


def section(key: str):
    return next((s for s in load() if s.key == key), None)


_WORD_RE = re.compile(r"[\w'’-]+", re.U)
STOPWORDS = set("""що як де це та і й в у на з із зі до для про чи а але або не є які який яка яке яку мені мене
мій моя моє мої ти ви ваш ваша вас нам нас від по за так вже ще все усі всі цей ця ці той та ті там тут коли чому
можна треба потрібно буде було був була бути таке такий така такі the a an is are to of in on for and or how what
where why do does
der die das und ist wie wo was ich""".split())


def _words(text: str) -> list:
    return [w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 1]


def _query_words(text: str) -> list:
    return [w for w in _words(text) if w not in STOPWORDS]


def search(query: str, limit: int = 10) -> list:
    """Сторінки за словами запиту: назва ×5, ключові слова ×4, опис і «де» ×2, текст ×1 (з частиною слова —
    «налаштуван» знаходить «налаштування»); службові слова («що», «як», «де»…) не рахуються. → [(сторінка, бал)]."""
    words = _query_words(query)
    if not words:
        return []
    out = []
    for p in pages():
        fields = ((p.title, 5), (" ".join(p.keywords), 4), (p.summary + " " + p.where, 2), (p.body, 1))
        score = 0
        for w in words:
            stem = w[:max(4, len(w) - 2)] if len(w) > 5 else w
            for text, weight in fields:
                low = text.lower()
                if stem in low:
                    score += weight * (2 if w in _words(text) else 1)
        if score:
            out.append((p, score))
    out.sort(key=lambda x: -x[1])
    return out[:limit]
