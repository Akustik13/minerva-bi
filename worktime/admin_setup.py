"""🗂 Категорії листів → askDANTE: знайти групи в пошті за період, створити категорії, дати кожній проєкт або
підпроєкт askDANTE і норми — одна сторінка поверх наявного (work_categories.suggest / MailCategory /
MailCategoryTime / services.create_dante). Далі — «✉️ Час на пошту»."""
import datetime
import json

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from . import services
from .client import DanteError
from .models import DanteActivity, DanteProject, DanteSettings, MailCategoryTime


def _int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def categories_page(model_admin, request):
    from email_assistant import work_categories
    from email_assistant.models import EmailMessage, MailCategory
    if not services.is_manager(request.user):
        raise PermissionDenied
    st = DanteSettings.get()
    url = reverse("admin:worktime_danteproject_categories")
    days = _int(request.POST.get("days") or request.GET.get("days"), 60) or 60

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":   # ✓ вибрані пропозиції → свої категорії
                made = []
                for key in request.POST.getlist("pick"):
                    data = json.loads(request.POST.get(f"s_{key}") or "{}")
                    name = (request.POST.get(f"n_{key}") or data.get("name") or key).strip()
                    made.append(work_categories.create_from_suggestion(name, data.get("senders", []),
                                                                       data.get("sources", [])).name)
                if made:
                    work_categories.start_recompute(days)
                    messages.success(request, f"Створено категорій: {len(made)} ({', '.join(made)}). Листи за "
                                              f"{days} днів розкладаються у фоні — далі виберіть їм проєкт askDANTE.")
                return redirect(f"{url}?days={days}#map")
            if action == "top":      # ＋ проєкт askDANTE верхнього рівня (напр. «Intern»)
                dp = services.create_dante(request.POST.get("top_name"), None, st)
                messages.success(request, f"Проєкт askDANTE «{dp.name}» готовий — тепер його можна вибрати як батьківський.")
                return redirect(f"{url}#map")
            if action == "save":     # проєкти askDANTE і норми категорій
                n = 0
                for cat in MailCategory.objects.filter(is_active=True):
                    p = request.POST
                    if f"dp_{cat.pk}" not in p:
                        continue
                    ct = MailCategoryTime.objects.filter(category=cat).first() or MailCategoryTime(category=cat)
                    parent = DanteProject.objects.filter(pk=p.get(f"parent_{cat.pk}") or 0, gone=False).first()
                    if parent is not None:   # ＋ новий підпроєкт під вибраним
                        ct.dante_project = services.create_dante(p.get(f"sub_{cat.pk}") or cat.name, parent, st)
                    else:
                        ct.dante_project = DanteProject.objects.filter(pk=p.get(f"dp_{cat.pk}") or 0).first()
                    ct.activity = DanteActivity.objects.filter(pk=p.get(f"act_{cat.pk}") or 0).first()
                    ct.reply_minutes = _int(p.get(f"reply_{cat.pk}"), ct.reply_minutes)
                    ct.read_minutes = _int(p.get(f"read_{cat.pk}"), ct.read_minutes)
                    ct.order_minutes = _int(p.get(f"order_{cat.pk}"), ct.order_minutes)
                    ct.auto = p.get(f"auto_{cat.pk}") == "1"
                    ct.save()
                    n += 1
                messages.success(request, f"Збережено категорій: {n}. Далі — «✉️ Час на пошту»: оцінка по днях і запис.")
                return redirect(f"{url}#map")
        except DanteError as e:
            messages.error(request, f"askDANTE: {e.message}")
            return redirect(f"{url}#map")

    suggestions = []
    if request.GET.get("scan") == "1":
        suggestions = work_categories.suggest(days)
        for s in suggestions:
            s["json"] = json.dumps({"name": s["name"], "senders": s["senders"], "sources": s["sources"]})
    since = timezone.now() - datetime.timedelta(days=days)
    rows = []
    for cat in MailCategory.objects.filter(is_active=True).select_related("dante__dante_project", "dante__activity"):
        ct = getattr(cat, "dante", None)
        rows.append({"cat": cat, "ct": ct,
                     "mails": EmailMessage.objects.filter(work_category=cat, sent_at__gte=since,
                                                          is_deleted=False).count()})
    projects = list(DanteProject.objects.filter(archived=False, gone=False).select_related("parent").order_by("name"))
    context = {
        **model_admin.admin_site.each_context(request), "title": "🗂 Категорії листів → askDANTE", "st": st,
        "days": days, "scanned": request.GET.get("scan") == "1", "suggestions": suggestions, "rows": rows,
        "projects": projects, "parents": [p for p in projects if p.time_trackable or not p.parent_id],
        "activities": list(DanteActivity.objects.filter(archived=False)),
        "cats_url": reverse("admin:email_assistant_mailcategory_changelist"),
        "mail_url": reverse("admin:worktime_workday_mailtime"),
        "mapping_url": reverse("admin:worktime_danteproject_changelist"),
    }
    return render(request, "admin/worktime/categories.html", context)
