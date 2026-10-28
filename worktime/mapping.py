"""Проєкти Minerva ↔ askDANTE. askDANTE лишається своїм довідником (там уже є проєкти й дані), Minerva лише
пам'ятає, куди писати час кожного свого проєкту (ProjectMap). Кілька проєктів Minerva можуть писати в один
проєкт askDANTE (напр. «Підтримка клієнта X»). Підказки нічого не зберігають — зв'язок ставить людина.
"""
from __future__ import annotations

import difflib
import re

from .models import DanteProject, ProjectMap

SIMILAR = 0.75


def _norm(text: str) -> str:
    text = re.sub(r"\b(gmbh|ag|kg|ug|ltd|llc|inc|co|ohg|e\.?k\.?|тов|фоп)\b\.?", " ", (text or "").lower())
    return re.sub(r"[^\w]+", " ", text).strip()


def _customer_names(project) -> list[str]:
    c = project.customer
    if c is None:
        return []
    return [n for n in (_norm(getattr(c, "company", "")), _norm(getattr(c, "name", ""))) if n]


def suggest(project, dante_projects) -> tuple[DanteProject, str] | None:
    """Найкращий проєкт askDANTE для проєкту Minerva: код P-…, та сама назва, клієнт, схожа назва."""
    code = (project.code or "").lower()
    name = _norm(project.name)
    if code:
        for d in dante_projects:
            if d.reference_number.lower() == code or code in d.name.lower():
                return d, f"код {project.code}"
    for d in dante_projects:
        if name and _norm(d.name) == name:
            return d, "та сама назва"
    customers = _customer_names(project)
    if customers:
        same = [d for d in dante_projects if _norm(d.customer_name) in customers or _norm(d.name) in customers]
        if len(same) == 1:
            return same[0], f"клієнт «{same[0].customer_name or same[0].name}»"
    best, score = None, 0.0
    for d in dante_projects:
        s = difflib.SequenceMatcher(None, name, _norm(d.name)).ratio() if name else 0
        if s > score:
            best, score = d, s
    if best and score >= SIMILAR:
        return best, f"схожа назва ({int(score * 100)}%)"
    return None


def suggest_customer(dp: DanteProject, customers=None):
    """Клієнт CRM для проєкту askDANTE за назвою клієнта askDANTE (компанія або контактна особа, без GmbH/AG…).
    Лише один збіг — підказка; кілька або жодного — None. customers — заздалегідь [(pk, norm company, norm name)]."""
    want = _norm(dp.customer_name)
    if not want:
        return None
    if customers is None:
        customers = customer_index()
    hits = {pk for pk, company, name in customers if want in (company, name)}
    if len(hits) != 1:
        return None
    from crm.models import Customer
    return Customer.objects.filter(pk=hits.pop()).first()


def customer_index() -> list[tuple[int, str, str]]:
    from crm.models import Customer
    return [(pk, _norm(company), _norm(name)) for pk, company, name in
            Customer.objects.values_list("pk", "company", "name")]


def find_customer(text: str):
    """Клієнт CRM з введеного тексту: «#123», точна компанія / ім'я / email або єдиний частковий збіг."""
    from crm.models import Customer
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("#") and text[1:].isdigit():
        return Customer.objects.filter(pk=int(text[1:])).first()
    for field in ("company__iexact", "name__iexact", "email__iexact"):
        qs = Customer.objects.filter(**{field: text})
        if qs.count() == 1:
            return qs.first()
    qs = Customer.objects.filter(company__icontains=text) | Customer.objects.filter(name__icontains=text)
    qs = qs.distinct()
    return qs.first() if qs.count() == 1 else None


def create_in_minerva(dp: DanteProject, user=None):
    """Проєкт Minerva з проєкту askDANTE (назва, клієнт CRM, якщо прив'язано), одразу зв'язаний з ним."""
    from projects import services as project_services
    project = project_services.create_project(name=dp.name, user=user, customer=dp.crm_customer,
                                              description=f"Створено з проєкту askDANTE «{dp}».")
    link(project, dp, user)
    return project


def link(project, dante_project: DanteProject, user=None) -> ProjectMap:
    m, _ = ProjectMap.objects.update_or_create(project=project, defaults={
        "dante_project": dante_project, "created_by": user if getattr(user, "pk", None) else None})
    return m


def default_dante_name(project) -> str:
    """Назва нового проєкту askDANTE для проєкту Minerva: «P-… Назва», до 50 символів."""
    from . import services
    return services.fit_name(f"{project.code} {project.name}")


def create_in_dante(project, user=None, name: str = "") -> DanteProject:
    """Новий проєкт в askDANTE з номером = код Minerva, одразу прив'язаний. Назва — `name` (людина підтвердила /
    змінила), інакше «P-… Назва», скорочена до 50 символів (більше askDANTE не приймає)."""
    from . import services
    data = services.client().create_project(services.check_name(name or default_dante_name(project)), project.code)
    if not data["id"]:
        from .client import DanteError
        raise DanteError("unavailable", "askDANTE створив проєкт, але не повернув його ID — "
                                        "оновіть довідники в налаштуваннях і прив'яжіть вручну.")
    dp, _ = DanteProject.objects.update_or_create(dante_id=data["id"], defaults={
        "name": data["name"][:255], "reference_number": data["reference_number"][:100], "gone": False,
        "archived": False, "time_trackable": True})
    link(project, dp, user)
    return dp
