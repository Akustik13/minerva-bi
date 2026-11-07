"""📨 Щотижневий звіт відповідальним (manage.py project_weekly --if-due у cron_runner.sh; кнопки в «⚙️ Налаштування
проєктів»). ProjectSettings: увімкнено (за замовчуванням ні), день, година, лист / Telegram, знімок кольору в історію.

due(now) — чи настав день і година (раз на день, last_run_at); claim(now) — атомарно (два процеси не надішлють двічі);
run(now, only=None) — для кожного відкритого проєкту без статусу людини за 6 днів — знімок кольору в історію
(ProjectUpdate auto, якщо auto_snapshot), далі кожному відповідальному (або лише `only`) — його відкриті проєкти:
колір і причина, фаза, прогноз проти строку, ризики (нові й закриті за тиждень), коли оновлювали статус, кроки
наступного тижня. Доставка — agent.notify.deliver (лист — SMTP сповіщень або скринька людини, Telegram —
UserProfile.telegram_id). → {"sent": n, "people": n, "snapshots": n, "result": текст}.
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Project, ProjectRisk, ProjectSettings, ProjectStep, ProjectUpdate

logger = logging.getLogger(__name__)

SNAPSHOT_GAP = 6            # днів без статусу людини — тижневий запуск записує знімок кольору сам
LINES_MAX = 25
NEXT_DAYS = 7
CLOSED = (ProjectStep.DONE, ProjectStep.SKIPPED)


def _start_of(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def due(now=None, s=None) -> bool:
    now = timezone.localtime(now or timezone.now())
    s = s or ProjectSettings.get()
    if not s.weekly_enabled or now.weekday() != s.weekday or now.hour < s.hour:
        return False
    return s.last_run_at is None or timezone.localtime(s.last_run_at).date() < now.date()


def claim(now=None) -> bool:
    """Атомарно позначити сьогоднішній запуск: True — цей процес надсилає."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    ProjectSettings.get()
    return bool(ProjectSettings.objects.filter(pk=1).filter(
        Q(last_run_at__isnull=True) | Q(last_run_at__lt=_start_of(today))).update(last_run_at=now))


def site_link(path) -> str:
    try:
        from config.models import SystemSettings
        s = SystemSettings.objects.filter(pk=1).first()
        if s is not None and s.site_domain:
            return s.site_url.rstrip("/") + path
    except Exception:  # noqa: BLE001
        pass
    return path


def snapshots(projects, rows, today) -> int:
    """Знімок кольору в історію для проєктів без статусу за SNAPSHOT_GAP днів (людини чи попереднього знімка)."""
    from . import health, risk_rules
    since = timezone.now() - timedelta(days=SNAPSHOT_GAP)
    recent = set(ProjectUpdate.objects.filter(project__in=projects, created_at__gte=since)
                 .values_list("project_id", flat=True))
    counts = risk_rules.counts([p.pk for p in projects])
    n = 0
    for p in projects:
        r = rows.get(p.pk)
        if p.pk in recent or r is None:
            continue
        ProjectUpdate.objects.create(project=p, health=health.suggest(r.health), source=ProjectUpdate.AUTO,
                                     text=r.reason or "", snapshot=health.snapshot(p, r, counts.get(p.pk, {})))
        n += 1
    return n


def _risk_news(project_ids, since) -> dict:
    """{project_id: (відкрито, закрито)} за період."""
    out = defaultdict(lambda: [0, 0])
    for pid in ProjectRisk.objects.filter(project_id__in=project_ids, opened_at__gte=since).values_list(
            "project_id", flat=True):
        out[pid][0] += 1
    for pid in ProjectRisk.objects.filter(project_id__in=project_ids, closed_at__gte=since).values_list(
            "project_id", flat=True):
        out[pid][1] += 1
    return out


def compose(owner, rows, today) -> dict:
    """Звіт людині: рядок на проєкт (найгірші першими) і кроки наступного тижня."""
    from . import health, portfolio, risk_rules
    since = timezone.now() - timedelta(days=7)
    ids = [r.project.pk for r in rows]
    news = _risk_news(ids, since)
    counts = risk_rules.counts(ids)
    statuses = {}
    for u in ProjectUpdate.objects.filter(project_id__in=ids, source=ProjectUpdate.MANUAL).order_by("-created_at"):
        statuses.setdefault(u.project_id, u)
    rows = sorted(rows, key=lambda r: (portfolio.HEALTH_ORDER.get(r.health, 9), r.project.due_date or today.max))
    lines = []
    for r in rows[:LINES_MAX]:
        p = r.project
        c = counts.get(p.pk, {})
        opened, closed = news.get(p.pk, (0, 0))
        st = statuses.get(p.pk)
        parts = [f"{r.dot} {p.code} {p.name}", r.phase_text or "без плану", r.reason]
        risk = " ".join(x for x in (f"🔴{c['red']}" if c.get("red") else "", f"🟡{c['yellow']}" if c.get("yellow")
                                     else "") if x)
        if risk or opened or closed:
            parts.append("ризики " + " ".join(x for x in (risk, f"нові {opened}" if opened else "",
                                                          f"закрито {closed}" if closed else "") if x))
        parts.append(f"статус {health.DOTS.get(st.health, '')} {health.age(st, today)} дн. тому" if st
                     else "статусу ще немає")
        lines.append(" · ".join(x for x in parts if x))
    if len(rows) > LINES_MAX:
        lines.append(f"… і ще {len(rows) - LINES_MAX}")
    upcoming = (ProjectStep.objects.filter(project_id__in=ids, f_end__gte=today,
                                           f_end__lte=today + timedelta(days=NEXT_DAYS))
                .filter(Q(assignee=owner) | Q(assignee__isnull=True, side=ProjectStep.WE))
                .exclude(status__in=CLOSED).exclude(eff_status__in=CLOSED)
                .select_related("project").order_by("f_end", "project__code")[:12])
    summary = "Кроки наступного тижня:\n" + "\n".join(f"• {s.f_end:%d.%m} {s.project.code}: {s.title}"
                                                      for s in upcoming) if upcoming else ""
    bad = sum(1 for r in rows if r.health == "bad")
    return {"subject": f"📁 Проєкти за тиждень — відкритих {len(rows)}" + (f", 🔴 {bad}" if bad else ""),
            "head": f"Ваші проєкти на {today:%d.%m.%Y}", "lines": lines, "summary": summary,
            "link": site_link("/admin/projects/project/portfolio/?f=mine"), "tg_max": 1500}


def run(now=None, only=None) -> dict:
    """Знімки кольору й звіти відповідальним. only — лише цій людині (кнопка «📨 Лише мені»)."""
    from . import portfolio
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    s = ProjectSettings.get()
    projects = list(Project.objects.filter(status__in=Project.OPEN_STATUSES).select_related("customer", "owner"))
    rows = {r.project.pk: r for r in portfolio.load(projects, None, today)}
    made = snapshots(projects, rows, today) if s.auto_snapshot and only is None else 0
    by_owner = defaultdict(list)
    for p in projects:
        if p.owner_id and p.owner.is_active and (only is None or p.owner_id == only.pk):
            by_owner[p.owner].append(rows[p.pk])
    sent, failed = 0, []
    try:
        from agent.notify import deliver
    except ImportError:            # без застосунку agent — лише знімки
        deliver = None
    for owner, items in by_owner.items():
        if deliver is None:
            break
        note = deliver(owner, compose(owner, items, today), "📊 Відкрити портфель",
                       mail=s.send_email, telegram=s.send_telegram)
        if note.startswith("🔔"):
            sent += 1
        if "🔕" in note:
            failed.append(f"{owner.get_username()}: {note.split('—', 1)[-1].strip()}")
    result = (f"{timezone.localtime(now):%d.%m %H:%M}: звітів {sent} з {len(by_owner)}"
              + (f", знімків кольору {made}" if made else "") + (f"; не вдалося — {'; '.join(failed)}" if failed else ""))
    if only is None:
        ProjectSettings.objects.filter(pk=1).update(last_result=result[:255])
    return {"sent": sent, "people": len(by_owner), "snapshots": made, "result": result}
