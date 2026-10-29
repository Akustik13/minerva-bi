"""
Перевірка гіда (manage.py guide_check, тест guide/tests.py): шапки сторінок, усі посилання й адреси.

- шапка: title, summary, where; розділ має _section.md з title;
- [текст](/адреса) і urls: — адреса Minerva, яку знає Django (resolve; без «<id>» — лише списки й сторінки);
- [текст](guide:розділ/сторінка) і related: — така сторінка є;
- {{api_endpoints:префікс}} — у цього префікса є адреси API.
"""
from urllib.parse import urlsplit

from django.urls import Resolver404, resolve

from . import api_docs, loader, render

REQUIRED = ("title", "summary", "where")


def _path_ok(target: str) -> bool:
    """Адреса є в Minerva. Адмінка Django ловить будь-яку /admin/… (catch_all_view і «/<номер>/» → редирект),
    тому там потрібен справжній іменований маршрут."""
    path = urlsplit(target).path
    try:
        match = resolve(path)
    except Resolver404:
        return False
    return not (path.startswith("/admin/") and match.url_name is None)


def check(section: str = "") -> list:
    """→ [«файл: помилка»]; порожньо — усе гаразд."""
    errors = []
    sections = loader.load()
    slugs = {p.slug for p in loader.pages()}
    for s in sections:
        if section and s.key != section:
            continue
        if s.title == s.key:
            errors.append(f"docs/guide/{s.key}/_section.md: немає розділу або title")
        for p in s.pages:
            where = p.path
            for name in REQUIRED:
                if not getattr(p, name):
                    errors.append(f"{where}: у шапці немає «{name}»")
            if not p.body.strip():
                errors.append(f"{where}: порожня сторінка")
            for target in render.links(p.body):
                href, _external = render.link_href(target)
                if href is None:
                    errors.append(f"{where}: незрозуміле посилання «{target}» (guide:…, /адреса або https://…)")
                elif target.startswith("guide:"):
                    slug = target[6:].partition("#")[0].strip("/")
                    if slug not in slugs:
                        errors.append(f"{where}: немає сторінки гіда «{slug}»")
                elif target.startswith("/") and not _path_ok(target):
                    errors.append(f"{where}: адреси Minerva немає «{target}»")
            for url in p.urls:
                if not url.startswith("/") or not _path_ok(url):
                    errors.append(f"{where}: urls — адреси Minerva немає «{url}»")
            for slug in p.related:
                if slug not in slugs:
                    errors.append(f"{where}: related — немає сторінки «{slug}»")
            for prefix in render.directives_used(p.body):
                if not api_docs.endpoints(prefix):
                    errors.append(f"{where}: {{{{api_endpoints:{prefix}}}}} — таких адрес API немає")
    return errors
