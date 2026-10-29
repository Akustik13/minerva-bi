"""📚 Гід по Minerva: зміст і пошук (/guide/), сторінка (/guide/<розділ>/<сторінка>/), пошук JSON для помічника."""
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, JsonResponse
from django.shortcuts import render

from . import api_docs, loader
from . import render as markdown


def _base(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


@staff_member_required
def index(request):
    q = request.GET.get("q", "").strip()
    return render(request, "guide/index.html", {
        **admin.site.each_context(request), "title": "📚 Гід по Minerva", "sections": loader.load(), "q": q,
        "results": loader.search(q, 30) if q else [], "total": len(loader.pages())})


@staff_member_required
def page(request, slug):
    p = loader.page(slug.strip("/"))
    if p is None:
        raise Http404("Такої сторінки гіда немає")
    base = _base(request)
    body, toc = markdown.render(p.body, base, directives=api_docs.directive(base))
    section = loader.section(p.section)
    siblings = section.pages if section else [p]
    i = siblings.index(p)
    return render(request, "guide/page.html", {
        **admin.site.each_context(request), "title": p.title, "page": p, "section": section, "body": body, "toc": toc,
        "prev": siblings[i - 1] if i > 0 else None, "next": siblings[i + 1] if i + 1 < len(siblings) else None,
        "related": [r for r in (loader.page(s) for s in p.related) if r is not None]})


@staff_member_required
def search_api(request):
    """GET ?q= → сторінки гіда за словами (для помічника й швидкого пошуку)."""
    q = request.GET.get("q", "").strip()
    return JsonResponse({"q": q, "results": [
        {"slug": p.slug, "title": p.title, "summary": p.summary, "where": p.where, "url": p.url, "score": score}
        for p, score in loader.search(q, 10)]})
