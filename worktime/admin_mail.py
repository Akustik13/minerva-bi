"""✉️ Час на пошту: блок «⏱ askDANTE» у своїй категорії листів і сторінка оцінки по днях (логіка — mail_time.py)."""
import datetime

from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import reverse

from email_assistant.models import MailCategory

from . import mail_time, services
from .client import DanteError
from .models import Booking, DanteActivity, DanteProject, DanteSettings, MailCategoryTime


class MailCategoryTimeInline(admin.StackedInline):
    """У картці «🏷 Своя категорія листів»: куди в askDANTE йде час на ці листи і скільки хвилин на що."""
    model = MailCategoryTime
    can_delete = False
    max_num = 1
    extra = 1
    verbose_name = "⏱ Час на ці листи в askDANTE"
    verbose_name_plural = "⏱ Час на ці листи в askDANTE"
    fields = (("dante_project", "activity"), ("reply_minutes", "read_minutes", "order_minutes"), "auto")
    autocomplete_fields = ()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "dante_project":
            kwargs["queryset"] = DanteProject.objects.filter(archived=False, gone=False).order_by("name")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


def _day(value, default):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _hm(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _loose_action(request, du, st, back):
    """Листи без askDANTE: 🏷 до категорії (+ проєкт askDANTE) або 🙈 не рахувати / ↺ знову рахувати."""
    from email_assistant.models import MailCategory
    from .models import MailTimeIgnore
    p = request.POST
    mails = [int(x) for x in p.getlist("mail") if str(x).isdigit()]
    if not mails:
        messages.warning(request, "Виберіть листи галочками.")
        return redirect(back)
    action = p.get("action")
    if action == "ignore":
        scope = p.get("ignore_scope") if p.get("ignore_scope") in dict(MailTimeIgnore.KINDS) else MailTimeIgnore.MESSAGE
        n = mail_time.ignore(du.user, mails, scope)   # рішення — для скриньок цього працівника
        messages.success(request, f"🙈 Не рахувати: {n} ({dict(MailTimeIgnore.KINDS)[scope].lower()}). Повернути — "
                                  "фільтр «проігноровані» → «↺ Знову рахувати».")
        return redirect(back)
    if action == "unignore":
        n = mail_time.unignore(du.user, mails)
        messages.success(request, f"↺ Знову рахуються (знято рішень: {n}).")
        return redirect(back)
    # assign
    if not (services.is_manager(request.user) or request.user.has_perm("email_assistant.add_mailcategory")):
        messages.error(request, "Немає права змінювати категорії листів.")
        return redirect(back)
    value = p.get("wcat") or ""
    if value == "new":
        name = (p.get("wcat_name") or "").strip()[:80]
        if not name:
            messages.error(request, "Вкажіть назву нової категорії.")
            return redirect(back)
        category = MailCategory.objects.filter(name__iexact=name).first() or MailCategory.objects.create(
            name=name, order=MailCategory.objects.count())
    else:
        category = MailCategory.objects.filter(pk=value or 0).first()
        if category is None:
            messages.error(request, "Виберіть категорію.")
            return redirect(back)
    dp = None
    try:
        dp = services.create_target(p.get("new_target"), p.get("sub_name") or category.name, st)
        if dp is None and p.get("dp"):   # ＋ новий проєкт клієнта / підпроєкт — або вибраний наявний
            dp = DanteProject.objects.filter(pk=p.get("dp")).first()
    except DanteError as e:
        messages.error(request, f"askDANTE: {e.message}")
        return redirect(back)
    scope = p.get("scope") if p.get("scope") in ("sender", "domain", "thread") else "sender"
    r = mail_time.assign(du.user, mails, category, scope, dp)
    messages.success(request, f"🏷 {r['category']}: оновлено листів {r['changed']}"
                              + (f" · askDANTE: {dp}" if dp else " · проєкт askDANTE ще не вибрано (🗂 Категорії → askDANTE)"))
    return redirect(back)


def transfer_url(category, days: int = 30) -> str:
    """«⏱ Перенести в askDANTE»: сторінка «✉️ Час на пошту» лише з цією категорією за останні `days` днів."""
    today = services.local_today()
    return (f"{reverse('admin:worktime_workday_mailtime')}?cat={category.pk}"
            f"&from={today - datetime.timedelta(days=days - 1)}&to={today}")


def mail_time_page(model_admin, request):
    """Оцінка часу на пошту по днях і проєктах askDANTE; ✓ — записати різницю в askDANTE."""
    from .admin import WEEKDAYS, WorkDayAdmin
    st = DanteSettings.get()
    people = list(services.users_for(request.user))
    du = WorkDayAdmin._pick(request, people)
    today = services.local_today()
    g = request.POST if request.method == "POST" else request.GET
    first = today.replace(day=1)
    if g.get("preset") == "last_month":
        date_to = first - datetime.timedelta(days=1)
        date_from = date_to.replace(day=1)
    elif g.get("preset") == "this_month":
        date_from, date_to = first, today
    else:
        date_to = min(_day(g.get("to"), today), today)
        date_from = _day(g.get("from"), date_to - datetime.timedelta(days=6))
    if date_from > date_to:
        date_from = date_to
    if (date_to - date_from).days >= mail_time.MAX_DAYS:
        date_from = date_to - datetime.timedelta(days=mail_time.MAX_DAYS - 1)
    show = g.get("show") if g.get("show") in ("all", "ignored") else "dante"
    # лише одна своя категорія — рядки її проєкту askDANTE (запис іде на день × проєкт)
    focus = (MailCategoryTime.objects.select_related("category", "dante_project")
             .filter(category_id=g.get("cat")).first() if str(g.get("cat") or "").isdigit() else None)
    focus_cat = focus.category if focus else MailCategory.objects.filter(pk=g.get("cat") or 0).first() \
        if str(g.get("cat") or "").isdigit() else None
    if focus_cat is not None:
        show = "dante"
    back = (f"{reverse('admin:worktime_workday_mailtime')}?u={du.pk if du else ''}&from={date_from}&to={date_to}"
            f"&show={show}" + (f"&cat={focus_cat.pk}" if focus_cat else ""))

    if request.method == "POST" and du is not None and request.POST.get("action") in ("assign", "ignore", "unignore"):
        return _loose_action(request, du, st, back)

    if request.method == "POST" and du is not None:
        if not (st.enabled and st.configured):
            messages.error(request, "askDANTE вимкнено або не налаштовано.")
            return redirect(back)
        done = minutes = 0
        activity = DanteActivity.objects.filter(pk=request.POST.get("activity") or 0).first()
        if request.POST.get("action") == "describe":   # 📝 короткі описи AI для рядків періоду (далі — правити вручну)
            n = 0
            for day, info in mail_time.estimate(du, date_from, date_to, st).items():
                for r in info["rows"]:
                    if n < mail_time.MAX_DESCRIBE and not mail_time.cached_summary(du, r, st.comment_lang):
                        n += bool(mail_time.describe(du, r, st.comment_lang, request.user))
            if n:
                messages.success(request, f"Описів AI: {n}. Перевірте й за потреби виправте текст перед записом.")
            else:
                messages.warning(request, "AI не дав описів (немає ключа Anthropic або листи без тексту) — "
                                          "лишається опис за темами листів.")
            return redirect(back)
        rewrite = request.POST.get("rewrite", "")
        picks = [rewrite] if rewrite else request.POST.getlist("pick")
        for pick in picks:
            day_s, _, dp = pick.partition("|")
            day = _day(day_s, None)
            if day is None or not dp.isdigit():
                continue
            text = request.POST.get(f"c_{pick}", "")
            try:
                if rewrite:
                    b = mail_time.rewrite(du, day, int(dp), comment=text, user=request.user, st=st, activity=activity)
                else:
                    b = mail_time.book(du, day, int(dp), user=request.user, source=Booking.MAIL, st=st,
                                       activity=activity, comment=text)
            except DanteError as e:
                messages.error(request, f"{day:%d.%m}: {e.message}")
                continue
            if b.status == Booking.SENT:
                done += 1
                minutes += b.duration or 0
            else:
                messages.error(request, f"{day:%d.%m}: askDANTE — {b.error}")
        if done:
            messages.success(request, f"Записано в askDANTE: {done} ({_hm(minutes)}).")
        return redirect(back)

    days = []
    total = todo = 0
    if du is not None:
        sent = {}
        for b in Booking.objects.filter(dante_user=du, date__range=(date_from, date_to), kind=Booking.PROJECT,
                                        source=Booking.MAIL, status=Booking.SENT).order_by("created_at"):
            sent.setdefault((b.date, b.dante_project_id), []).append(b)
        for day, info in mail_time.estimate(du, date_from, date_to, st).items():
            if focus_cat is not None:
                info["rows"] = [r for r in info["rows"] if focus and r["dp"].pk == focus.dante_project_id]
                if not info["rows"]:
                    continue
                info["unmapped"], info["loose"], info["ignored"] = 0, [], []
            for r in info["rows"]:
                r["dur"], r["booked_dur"], r["todo_dur"] = _hm(r["minutes"]), _hm(r["booked"]), _hm(r["to_book"])
                r["key"] = f"{day:%Y-%m-%d}|{r['dp'].pk}"
                summary = mail_time.cached_summary(du, r, st.comment_lang)
                r["comment"], r["ai"] = mail_time.default_comment(r, st.comment_lang, summary), bool(summary)
                r["sent"] = sent.get((day, r["dp"].pk), [])
                total += r["minutes"]
                todo += r["to_book"]
            days.append({"day": day, "wd": WEEKDAYS[day.weekday()], "rows": info["rows"], "unmapped": info["unmapped"],
                         "loose": info["loose"], "ignored": info["ignored"],
                         "ongoing": day == today})
    days.reverse()   # свіжі дні зверху
    cats = list(MailCategoryTime.objects.select_related("category", "dante_project").order_by("category__order"))
    shared = [c.category.label for c in cats if focus and focus.dante_project_id and c.pk != focus.pk
              and c.dante_project_id == focus.dante_project_id]
    context = {
        **model_admin.admin_site.each_context(request), "title": "✉️ Час на пошту", "st": st, "du": du,
        "people": people, "manager": services.is_manager(request.user), "date_from": date_from, "date_to": date_to,
        "days": days, "total": _hm(total), "todo": _hm(todo), "today": today, "cats": cats,
        "cats_url": reverse("admin:email_assistant_mailcategory_changelist"),
        "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
        "calendar_url": reverse("admin:worktime_workday_changelist"),
        "mapping_url": reverse("admin:worktime_danteproject_changelist") + "?view=minerva",
        "activities": list(DanteActivity.objects.filter(archived=False)),
        "needs_activity": st.activity_required and not st.default_activity_id,
        "show": show, "n_loose": sum(len(d["loose"]) for d in days), "n_ignored": sum(len(d["ignored"]) for d in days),
        "focus": focus, "focus_cat": focus_cat, "shared": shared,
        "focus_url": (reverse("admin:email_assistant_mailcategory_change", args=[focus_cat.pk]) if focus_cat else ""),
        "categories": list(MailCategory.objects.filter(is_active=True)),
        "can_categorize": services.is_manager(request.user) or request.user.has_perm("email_assistant.add_mailcategory"),
        "projects": list(DanteProject.objects.filter(archived=False, gone=False).select_related("parent")
                         .order_by("name")),
        "customers": services.dante_customers(),
        "lang": dict(DanteSettings.COMMENT_LANGS).get(st.comment_lang, st.comment_lang),
        "dante_url": st.base_url,
    }
    return render(request, "admin/worktime/mail_time.html", context)
