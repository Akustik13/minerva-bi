"""
crm/merge.py — злиття дублікатів клієнта CRM («🔀 Злити дублікатів», «🔍 Знайти дублікати → Авто-злити»).

Перед видаленням дубліката все, що на нього посилається, переходить до основного клієнта — в одній транзакції
(збій — нічого не змінено):
  - замовлення: SalesOrder.customer_key (ключ, не FK); замовлення без ключа клієнта CRM — за email дубліката;
  - КОЖЕН зв'язок з Customer (ForeignKey / OneToOne з будь-якого застосунку, зокрема приховані related_name="+")
    — шукаються через Customer._meta, тож нові зв'язки переходять самі: нотатки, хронологія, стратегії, задачі,
    рахунки, проєкти й учасники, гілки пошти, Angebote, документи й джерела бази знань, події календаря,
    запрошення, askDANTE, акаунти магазину, Kundennummer…;
  - зв'язки ManyToMany (через проміжну таблицю, без дублів);
  - «🧭 Огляд / ❓ Запитати» картки клієнта (rag_assistant.EntityHelp, kind=customer — не FK);
  - порожні поля основного заповнюються з дубліката, «Примітки» дописуються;
  - файли картки media/customers/<pk>/ — до основного (після commit; збіг назв — «назва (2).pdf»).
Унікальний зв'язок, який основний клієнт уже має (напр. Kundennummer — одна на клієнта), не переноситься:
запис лишається без клієнта (не видаляється) і називається в підсумку. Якщо такий зв'язок не може бути
порожнім — злиття не виконується (MergeError), щоб нічого не загубити.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import models, transaction

logger = logging.getLogger(__name__)

FILL_FIELDS = ("email", "phone", "company", "country",
               "addr_street", "addr_city", "addr_zip", "addr_state", "shipping_address", "source")


class MergeError(Exception):
    """Злиття неможливе без втрати даних — нічого не змінено."""


def relations():
    """Усі зв'язки інших моделей з Customer: [(model, field)] — FK / OneToOne (і приховані)."""
    from .models import Customer
    out = []
    for rel in Customer._meta.get_fields(include_hidden=True):
        if rel.auto_created and not rel.concrete and rel.is_relation and (rel.one_to_many or rel.one_to_one) \
                and rel.related_model._meta.managed:          # managed=False (заглушки, представлення) — без таблиці
            out.append((rel.related_model, rel.field))
    return out


def m2m_relations():
    """Зв'язки ManyToMany з Customer: [(through, поле на Customer, поле на іншу модель)]."""
    from .models import Customer
    out = []
    for f in Customer._meta.get_fields(include_hidden=True):
        if not f.many_to_many:
            continue
        if not f.auto_created:      # поле на самому Customer
            through, mine, other = f.remote_field.through, f.m2m_field_name(), f.m2m_reverse_field_name()
        else:               # поле на іншій моделі → Customer
            through, mine, other = f.through, f.field.m2m_reverse_field_name(), f.field.m2m_field_name()
        if (through, mine) not in [(t, m) for t, m, _ in out]:
            out.append((through, mine, other))
    return out


def _unique_groups(model, field) -> list:
    """Інші поля, разом з якими field унікальне: [()] — саме поле (OneToOne / unique), [("shop",)] …"""
    groups = []
    if field.unique:
        groups.append(())
    for fields in model._meta.unique_together:
        if field.name in fields:
            groups.append(tuple(f for f in fields if f != field.name))
    for c in model._meta.constraints:
        if isinstance(c, models.UniqueConstraint) and c.fields and field.name in c.fields and c.condition is None:
            groups.append(tuple(f for f in c.fields if f != field.name))
    return groups


def _label(model) -> str:
    return str(model._meta.verbose_name_plural)


def _move_relation(model, field, winner, loser_pks, stats):
    manager = model._base_manager
    qs = manager.filter(**{f"{field.name}__in": loser_pks})
    groups = _unique_groups(model, field)
    if not groups:
        moved = qs.update(**{field.name: winner})
        if moved:
            stats["moved"][_label(model)] = stats["moved"].get(_label(model), 0) + moved
        return
    for obj in qs:
        clash = any(
            manager.filter(**{field.name: winner}, **{f: getattr(obj, f) for f in other}).exclude(pk=obj.pk).exists()
            for other in groups
        )
        if not clash:
            manager.filter(pk=obj.pk).update(**{field.name: winner})
            stats["moved"][_label(model)] = stats["moved"].get(_label(model), 0) + 1
        elif field.null:
            manager.filter(pk=obj.pk).update(**{field.name: None})
            stats["kept"].append(f"{model._meta.verbose_name}: {obj}")
        else:
            raise MergeError(f"«{model._meta.verbose_name}» ({obj}) вже є в основного клієнта, "
                             "а без клієнта цей запис існувати не може. Злиття не виконано.")


def _move_m2m(through, mine, other, winner, loser_pks, stats):
    rows = through._base_manager.filter(**{f"{mine}_id__in": loser_pks})
    have = set(through._base_manager.filter(**{f"{mine}_id": winner.pk}).values_list(f"{other}_id", flat=True))
    for row in rows:
        target = getattr(row, f"{other}_id")
        if target in have:
            row.delete()            # той самий зв'язок уже є в основного
        else:
            through._base_manager.filter(pk=row.pk).update(**{f"{mine}_id": winner.pk})
            have.add(target)
            label = _label(through._meta.get_field(other).related_model)
            stats["moved"][label] = stats["moved"].get(label, 0) + 1


def _move_orders(winner, losers, stats):
    from .models import Customer
    from sales.models import SalesOrder
    moved = SalesOrder.objects.filter(customer_key__in=[c.external_key for c in losers]).update(
        customer_key=winner.external_key)
    # Замовлення без ключа клієнта CRM вели на дубліката за email (як посилання в Продажах) — тепер на основного
    known = Customer.objects.exclude(external_key="").values("external_key")
    for email in {(c.email or "").strip() for c in losers if (c.email or "").strip()}:
        moved += (SalesOrder.objects.filter(email__iexact=email).exclude(customer_key__in=known)
                  .update(customer_key=winner.external_key))
    stats["orders"] = moved


def _move_entity_help(winner, loser_pks, stats):
    try:
        model = apps.get_model("rag_assistant", "EntityHelp")
    except LookupError:
        return
    moved = model.objects.filter(kind="customer", obj_id__in=loser_pks).update(obj_id=winner.pk)
    if moved:
        stats["moved"]["Огляди й запитання картки"] = moved


def _fill_winner(winner, losers):
    changed = False
    extra_notes = []
    for loser in losers:
        for field in FILL_FIELDS:
            if not getattr(winner, field) and getattr(loser, field):
                setattr(winner, field, getattr(loser, field))
                changed = True
        if loser.notes:
            extra_notes.append(loser.notes)
    if extra_notes:
        sep = "\n\n---\n"
        winner.notes = (winner.notes + sep if winner.notes else "") + sep.join(extra_notes)
        changed = True
    if changed:
        winner.save()


def _docs_dir(pk) -> Path:
    return Path(settings.MEDIA_ROOT) / "customers" / str(pk)


def _free_name(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    stem, ext = os.path.splitext(name)
    n = 2
    while target.exists():
        target = dest_dir / f"{stem} ({n}){ext}"
        n += 1
    return target


def move_documents(winner_pk, loser_pks) -> int:
    """Файли «📎 Документи клієнта» дублікатів → папка основного. Файл не перезаписується."""
    moved = 0
    dest = _docs_dir(winner_pk)
    for pk in loser_pks:
        src = _docs_dir(pk)
        if not src.is_dir():
            continue
        for f in sorted(src.iterdir()):
            if not f.is_file():
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(_free_name(dest, f.name)))
                moved += 1
            except OSError as e:
                logger.warning("merge: file %s not moved: %s", f, e)
        try:
            src.rmdir()
        except OSError:
            pass            # лишились файли, які не вдалося перенести — папка не видаляється
    return moved


def _after_commit(winner_pk, loser_pks, thread_ids, item_ids):
    """Файли картки й база знань: масові .update() без сигналів → повідомити RAG (як knowledge_hook)."""
    try:
        move_documents(winner_pk, loser_pks)
    except Exception as e:  # noqa: BLE001 — файли лишаються в старій папці, нічого не втрачено
        logger.warning("merge: documents of %s not moved: %s", loser_pks, e)
    if thread_ids:
        try:
            from email_assistant.knowledge_hook import touch
            from email_assistant.models import EmailMessage
            touch(pks=list(EmailMessage.objects.filter(thread_id__in=thread_ids).values_list("pk", flat=True)))
        except Exception as e:  # noqa: BLE001 — нічна звірка knowledge_refresh добере
            logger.warning("merge: knowledge touch of threads failed: %s", e)
    if item_ids:
        try:
            from rag_assistant.knowledge_items import touch
            touch(item_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("merge: knowledge touch of documents failed: %s", e)


def _ids_of(app_label, model_name, field, loser_pks) -> list:
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return []
    return list(model._base_manager.filter(**{f"{field}__in": loser_pks}).values_list("pk", flat=True))


def merge_customers(winner, losers) -> dict:
    """Злити дублікатів у winner. Повертає {orders, moved: {що: скільки}, kept: [..], deleted, winner}."""
    from .models import Customer
    losers = [c for c in losers if c.pk != winner.pk]
    loser_pks = [c.pk for c in losers]
    stats = {"orders": 0, "moved": {}, "kept": [], "deleted": 0, "winner": winner}
    if not losers:
        return stats

    with transaction.atomic():
        # гілки пошти й документи бази знань, які переходять, — щоб після commit повідомити RAG
        thread_ids = _ids_of("email_assistant", "EmailThread", "crm_customer", loser_pks)
        item_ids = _ids_of("rag_assistant", "KnowledgeItem", "customer", loser_pks)

        _move_orders(winner, losers, stats)
        for model, field in relations():
            _move_relation(model, field, winner, loser_pks, stats)
        for through, mine, other in m2m_relations():
            _move_m2m(through, mine, other, winner, loser_pks, stats)
        _move_entity_help(winner, loser_pks, stats)
        _fill_winner(winner, losers)

        stats["deleted"] = Customer.objects.filter(pk__in=loser_pks).delete()[1].get(Customer._meta.label, 0)
        transaction.on_commit(lambda: _after_commit(winner.pk, loser_pks, thread_ids, item_ids))
    return stats


def summary(stats) -> str:
    """«Замовлень: 3 · Проєкти: 1 · Задачі: 2» — для повідомлення після злиття."""
    parts = [f"Замовлень перенесено: {stats['orders']}"]
    parts += [f"{label}: {n}" for label, n in sorted(stats["moved"].items())]
    text = " · ".join(parts)
    if stats["kept"]:
        text += (" · Лишилось без клієнта (в основного вже є своє): " + "; ".join(stats["kept"][:5])
                 + ("…" if len(stats["kept"]) > 5 else ""))
    return text
