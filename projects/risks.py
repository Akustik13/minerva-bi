"""⚠️ Ризики проєкту, які змінює людина (вікно ризику в пульті, реєстр «⚠️ Ризики»): створити, змінити, закрити,
видалити. Ризики правил (source rule) відкриває й закриває risk_rules.py: людина змінює їм власника, заходи, стан,
дату перегляду, ймовірність і вплив (edited — правило їх більше не перезаписує), назву й підставу — ні; видалити
ризик правила не можна — лише закрити (тоді правило його не відкриває знову).
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import ProjectRisk, ProjectStep


class RiskError(ValueError):
    pass


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise RiskError(f"Дата «{value}» — не у форматі РРРР-ММ-ДД")


def _level(value, current):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return current
    return v if v in (1, 2, 3) else current


def set_status(risk, status, now=None):
    """Стан, який ставить людина: закрила — правило не відкриє знову; відкрила знову — без дати закриття."""
    now = now or timezone.now()
    if status not in dict(ProjectRisk.STATUS_CHOICES) or status == risk.status:
        return
    if status == ProjectRisk.CLOSED:
        risk.closed_at, risk.closed_auto = now, False
    elif risk.status == ProjectRisk.CLOSED:
        risk.closed_at, risk.closed_auto, risk.opened_at = None, False, now
    risk.status = status


def save(project, data, user=None, message=None, source=ProjectRisk.MANUAL) -> ProjectRisk:
    """{id, title, description, category, probability, impact, owner, mitigation, status, review_on, steps: [id]}.
    source — для нового ризику: вручну або прийнята пропозиція AI-помічника (ai); правило — лише risk_rules."""
    rid = data.get("id")
    risk = ProjectRisk.objects.filter(project=project, pk=rid).first() if str(rid or "").isdigit() else None
    if rid and risk is None:
        raise RiskError("Ризик не знайдено")
    new = risk is None
    if new:
        risk = ProjectRisk(project=project, source=ProjectRisk.AI if source == ProjectRisk.AI else ProjectRisk.MANUAL,
                           created_by=user, owner=project.owner)
    if risk.source != ProjectRisk.RULE:
        title = " ".join(str(data.get("title") or "").split())[:200]
        if not title:
            raise RiskError("Напишіть, що може статися")
        risk.title = title
        if "description" in data:
            risk.description = str(data.get("description") or "")[:4000]
    if data.get("category") in dict(ProjectRisk.CATEGORY_CHOICES):
        risk.category = data["category"]
    p, i = _level(data.get("probability"), risk.probability), _level(data.get("impact"), risk.impact)
    if risk.source == ProjectRisk.RULE and (p, i) != (risk.probability, risk.impact):
        risk.edited = True
    risk.probability, risk.impact = p, i
    if "owner" in data:
        oid = data.get("owner")
        risk.owner = (get_user_model().objects.filter(pk=oid, is_active=True, is_staff=True).first()
                      if str(oid or "").isdigit() else None)
    if "mitigation" in data:
        risk.mitigation = str(data.get("mitigation") or "")[:4000]
    if "review_on" in data:
        risk.review_on = _date(data.get("review_on"))
    set_status(risk, data.get("status"))
    if message is not None and new:
        risk.message = message
        risk.evidence = (str(data.get("quote") or "").strip()[:1000]
                         or f"Лист «{message.subject or '(без теми)'}»")
    risk.save()
    if "steps" in data:
        ids = [int(x) for x in (data.get("steps") or []) if str(x).isdigit()]
        risk.steps.set(ProjectStep.objects.filter(project=project, pk__in=ids))
    return risk


def delete(project, rid) -> bool:
    risk = ProjectRisk.objects.filter(project=project, pk=rid).first()
    if risk is None:
        return False
    if risk.source == ProjectRisk.RULE:
        raise RiskError("Ризик правила не видаляється — закрийте його (правило тоді не відкриє його знову)")
    risk.delete()
    return True
