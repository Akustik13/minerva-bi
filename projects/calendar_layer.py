"""Кроки й віхи планів проєктів у календарі Minerva (/calendar/, calendar_app.views.calendar_view → cell.pj).

Лише свої: крок, де людина відповідальна, а без відповідального — кроки нашої сторони й віхи проєктів, де вона
власник. Відкриті проєкти, незавершені кроки. День — прогноз завершення (кеш schedule.persist), без нього — план.
Лише показ: клік відкриває вкладку «📅 План» проєкту.
"""
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

LIMIT = 300


def month_steps(user, year, month) -> dict:
    """{день: [{title, code, icon, url, tip, late}]} — кроки, що мають закінчитися цього місяця."""
    from .models import Project, ProjectStep
    from .schedule import planned_end
    if not getattr(user, "is_authenticated", False) or not user.has_perm("projects.view_project"):
        return {}
    closed = (ProjectStep.DONE, ProjectStep.SKIPPED)
    mine = Q(assignee=user) | (Q(assignee__isnull=True, project__owner=user)
                               & (Q(side=ProjectStep.WE) | Q(kind=ProjectStep.MILESTONE)))
    qs = (ProjectStep.objects.annotate(day=Coalesce("f_end", "end"))
          .filter(mine, day__year=year, day__month=month, project__status__in=Project.OPEN_STATUSES)
          .exclude(status__in=closed).exclude(eff_status__in=closed)
          .select_related("project").order_by("day", "project__code", "order")[:LIMIT])
    today = timezone.localdate()
    out = {}
    for s in qs:
        milestone = s.kind == ProjectStep.MILESTONE
        planned = planned_end(s)
        late = bool(planned and planned < today)
        tip = (f"{'◆ Віха' if milestone else '📋 Крок'} · {s.project.code} {s.project.name} · {s.title}"
               f" · прогноз {s.day:%d.%m.%Y}" + (f" · план {planned:%d.%m}" if planned and planned != s.day else "")
               + (" · прострочено" if late else "") + (" · критичний шлях" if s.critical else ""))
        out.setdefault(s.day.day, []).append({
            "title": s.title, "code": s.project.code, "icon": "◆" if milestone else "📋", "late": late,
            "url": f"/admin/projects/project/{s.project_id}/change/#pj-plan", "tip": tip})
    return out
