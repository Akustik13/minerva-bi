import calendar as pycal
import datetime

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from . import mapping, services
from .client import DanteError, hhmm
from .models import Booking, DanteProject, DanteSettings, DanteUser, ProjectMap, WorkDay

MONTHS = ["", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень",
          "Жовтень", "Листопад", "Грудень"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MAX_FILL_DAYS = 120


def _date(value):
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _time(value):
    try:
        return datetime.time.fromisoformat(str(value or "")[:5])
    except ValueError:
        return None


def _duration(minutes) -> str:
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


# ── Налаштування ─────────────────────────────────────────────────
class DanteSettingsForm(forms.ModelForm):
    api_key = forms.CharField(
        label="API-Key askDANTE", required=False, max_length=200,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 50}),
        help_text="askDANTE → Einstellungen → Schnittstellen → Rest API. Порожньо — залишити збережений ключ. "
                  "Ключ зберігається лише на сервері Minerva.",
    )
    clear_key = forms.BooleanField(label="Видалити збережений ключ", required=False)

    class Meta:
        model = DanteSettings
        fields = "__all__"
        widgets = {"default_start": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                   "default_stop": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                   "autofill_since": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.secrets import placeholder
        self.fields["api_key"].widget.attrs["placeholder"] = placeholder(self.instance.api_key if self.instance else "")

    def clean_api_key(self):
        value = (self.cleaned_data.get("api_key") or "").strip()
        if not value and self.instance and self.instance.pk and not self.data.get("clear_key"):
            return self.instance.api_key
        return value

    def clean_workdays(self):
        value = "".join(sorted({c for c in (self.cleaned_data.get("workdays") or "") if c in "1234567"}))
        if not value:
            raise forms.ValidationError("Вкажіть хоча б один робочий день (1 — понеділок … 7 — неділя).")
        return value

    def clean(self):
        d = super().clean()
        if d.get("clear_key"):
            d["api_key"] = ""
        if (d.get("autofill_hour") or 0) > 23:
            self.add_error("autofill_hour", "Година від 0 до 23.")
        return d


@admin.register(DanteSettings)
class DanteSettingsAdmin(admin.ModelAdmin):
    form = DanteSettingsForm
    fieldsets = [
        (None, {"fields": ["enabled", "calendar_link"]}),
        ("Підключення до askDANTE", {
            "fields": ["base_url", "api_key", "clear_key", "timeout", "check_status"],
            "description": "Minerva звертається до askDANTE зі свого сервера; браузер ключа не бачить. "
                           "askDANTE — головний облік часу: Minerva лише читає дні й дописує туди, де порожньо. "
                           "Записи askDANTE через API не виправляються й не видаляються — помилки правити в askDANTE.",
        }),
        ("Робочий день", {"fields": ["default_start", "default_stop", "workdays"]}),
        ("Автозаповнення (щодня)", {
            "fields": ["autofill_mode", "autofill_since", "autofill_lookback_days", "autofill_hour",
                       "activity_fallback", "round_minutes", "min_activity_minutes", "autofill_status"],
            "description": "Працює лише для працівників з галочкою «Автозаповнення» (👥 Працівники askDANTE). "
                           "Заповнює робочі дні до вчора, у яких в askDANTE немає жодного запису, відпустки, "
                           "лікарняного чи свята. Дні з попередженнями (понад 10 годин, не прочитано свята) "
                           "лишаються на перевірку в календарі.",
        }),
    ]
    readonly_fields = ["calendar_link", "check_status", "autofill_status"]

    def has_add_permission(self, request):
        return not DanteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:worktime_dantesettings_change", args=[DanteSettings.get().pk]))

    @admin.display(description="Календар")
    def calendar_link(self, obj):
        return format_html('<a class="button" href="{}">📅 Відкрити календар робочого часу</a>',
                           reverse("admin:worktime_workday_changelist"))

    @admin.display(description="Перевірка зв'язку")
    def check_status(self, obj):
        last = (f"{timezone.localtime(obj.last_check_at):%d.%m.%Y %H:%M} — {obj.last_check_result}"
                if obj and obj.last_check_at else "ще не перевірялось")
        synced = (f"Довідники оновлено {timezone.localtime(obj.last_sync_at):%d.%m.%Y %H:%M}"
                  if obj and obj.last_sync_at else "Довідники ще не завантажувались")
        return format_html(
            '<button type="submit" class="button" formaction="{}" formnovalidate>🔌 Перевірити зв\'язок</button> '
            '<button type="submit" class="button" formaction="{}" formnovalidate>🔄 Завантажити працівників, '
            'проєкти й види робіт</button><div style="margin-top:6px">{}</div><div class="help">{} · спершу '
            'збережіть зміни — перевіряються збережені налаштування.</div>',
            reverse("admin:worktime_dantesettings_check"), reverse("admin:worktime_dantesettings_sync"), last, synced)

    @admin.display(description="Стан")
    def autofill_status(self, obj):
        last = (f"Останній запуск {timezone.localtime(obj.last_autofill_at):%d.%m.%Y %H:%M} — {obj.last_autofill_result}"
                if obj and obj.last_autofill_at else "ще не запускалось")
        people = DanteUser.objects.filter(autofill=True, archived=False).count()
        return format_html(
            '{}<div class="help">Працівників з автозаповненням: {}.</div>'
            '<button type="submit" class="button" formaction="{}" formnovalidate>🧪 Що записалось би зараз '
            '(без запису)</button>', last, people, reverse("admin:worktime_dantesettings_dryrun"))

    def get_urls(self):
        view = self.admin_site.admin_view
        return [path("check/", view(self.check_view), name="worktime_dantesettings_check"),
                path("sync/", view(self.sync_view), name="worktime_dantesettings_sync"),
                path("dry-run/", view(self.dryrun_view), name="worktime_dantesettings_dryrun")] + super().get_urls()

    def _post_only(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied
        return redirect(reverse("admin:worktime_dantesettings_change", args=[DanteSettings.get().pk]))

    def check_view(self, request):
        back = self._post_only(request)
        if request.method != "POST":
            return back
        st = DanteSettings.get()
        try:
            c = services.client(st)
            users = c.users()
            projects = c.projects()
            result, level = (f"✅ OK · працівників: {len(users)} · проєктів: {len(projects)}"
                             + (" · ключ приймається лише параметром адреси" if st.key_in_query else "")), messages.SUCCESS
        except DanteError as e:
            result, level = f"❌ {e.message}", messages.ERROR
        st.last_check_at, st.last_check_result = timezone.now(), result[:255]
        st.save(update_fields=["last_check_at", "last_check_result"])
        self.message_user(request, result, level)
        return back

    def sync_view(self, request):
        back = self._post_only(request)
        if request.method != "POST":
            return back
        try:
            r = services.sync_directory()
            self.message_user(request, f"✅ Працівників: {r['users']} (прив'язано за email: {r['linked']}) · "
                                       f"проєктів: {r['projects']} · видів робіт: {r['activities']}", messages.SUCCESS)
        except DanteError as e:
            self.message_user(request, f"❌ {e.message}", messages.ERROR)
        return back

    def dryrun_view(self, request):
        back = self._post_only(request)
        if request.method != "POST":
            return back
        st = DanteSettings.get()
        if st.autofill_mode == DanteSettings.MODE_OFF:
            self.message_user(request, "Автозаповнення вимкнено — виберіть режим і збережіть.", messages.WARNING)
            return back
        date_from, date_to = services.autofill_range(st)
        try:
            result = services.autofill(st, dry_run=True)
            self.message_user(request, f"{date_from:%d.%m}–{date_to:%d.%m}: {result}. Нічого не записано.",
                              messages.INFO)
        except DanteError as e:
            self.message_user(request, f"❌ {e.message}", messages.ERROR)
        return back


# ── Працівники ───────────────────────────────────────────────────
class DanteUserForm(forms.ModelForm):
    class Meta:
        model = DanteUser
        fields = "__all__"
        widgets = {"start": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                   "stop": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}


@admin.register(DanteUser)
class DanteUserAdmin(admin.ModelAdmin):
    form = DanteUserForm
    list_display = ["__str__", "email", "personnel_number", "user", "autofill", "start", "stop", "archived",
                    "calendar"]
    list_editable = ["user", "autofill", "start", "stop"]
    list_filter = ["archived", "autofill"]
    search_fields = ["first_name", "last_name", "email", "personnel_number"]
    readonly_fields = ["dante_id", "first_name", "last_name", "email", "personnel_number", "archived",
                       "open_since", "synced_at"]
    fields = ["dante_id", "first_name", "last_name", "email", "personnel_number", "archived", "user",
              "autofill", "start", "stop", "open_since", "synced_at"]

    def has_add_permission(self, request):
        return False   # працівники приходять з askDANTE («Завантажити працівників…» в налаштуваннях)

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="")
    def calendar(self, obj):
        return format_html('<a href="{}?u={}">📅</a>', reverse("admin:worktime_workday_changelist"), obj.pk)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["subtitle"] = "Працівники приходять з askDANTE; користувача Minerva підбирає за email"
        return super().changelist_view(request, extra_context)


# ── Журнал ───────────────────────────────────────────────────────
@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["created_at", "dante_user", "date", "interval", "source", "status", "reason", "error",
                    "created_by"]
    list_filter = ["status", "source", "kind", "dante_user"]
    date_hierarchy = "date"
    search_fields = ["error", "reason", "comment"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Час")
    def interval(self, obj):
        if obj.start and obj.stop:
            return f"{hhmm(obj.start)}–{hhmm(obj.stop)} ({_duration(obj.duration)})"
        return _duration(obj.duration) if obj.duration else "—"


# ── Календар ─────────────────────────────────────────────────────
@admin.register(WorkDay)
class WorkDayAdmin(admin.ModelAdmin):
    """Список днів = календар робочого часу (свій — для кожного, усі — для адміністратора)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return (super().has_view_permission(request, obj) or services.is_manager(request.user)
                or DanteUser.objects.filter(user=request.user).exists())

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def get_urls(self):
        return [path("", self.admin_site.admin_view(self.calendar_view), name="worktime_workday_changelist")] \
            + super().get_urls()

    # ── сторінка ─────────────────────────────────────────────────
    def calendar_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        st = DanteSettings.get()
        people = list(services.users_for(request.user))
        du = self._pick(request, people)
        if request.method == "POST":
            return self._post(request, st, du)

        today = services.local_today()
        month = _date((request.GET.get("m") or "") + "-01") or today.replace(day=1)
        first = month.replace(day=1)
        last = first.replace(day=pycal.monthrange(first.year, first.month)[1])
        ctx = {**self.admin_site.each_context(request), "title": "📅 Робочий час (askDANTE)",
               "opts": self.model._meta, "st": st, "people": people, "du": du, "today": today,
               "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
               "users_url": reverse("admin:worktime_danteuser_changelist"),
               "is_manager": services.is_manager(request.user), "weekdays": WEEKDAYS}
        if du is None or not st.configured:
            return render(request, "admin/worktime/calendar.html", ctx)

        days, error = {}, ""
        if st.enabled:
            try:
                days = services.refresh_days(du, first, min(last, today))
                if getattr(days, "attendance_error", ""):
                    error = f"Свята й відсутності askDANTE не прочитано: {days.attendance_error}"
            except DanteError as e:
                error = f"askDANTE: {e.message} Показано останні збережені дані."
        cached = services.cached_days(du, first, last)
        cached.update(days)

        weeks, totals = self._grid(st, du, first, last, cached, today)
        prev_m = (first - datetime.timedelta(days=1)).replace(day=1)
        next_m = (last + datetime.timedelta(days=1))
        ctx.update({
            "error": error, "weeks": weeks, "totals": totals, "month_label": f"{MONTHS[first.month]} {first.year}",
            "prev_m": f"{prev_m:%Y-%m}", "next_m": f"{next_m:%Y-%m}" if next_m <= today else "",
            "cur_m": f"{first:%Y-%m}", "def_start": hhmm(du.times(st)[0]), "def_stop": hhmm(du.times(st)[1]),
            "open_since": timezone.localtime(du.open_since) if du.open_since else None,
            "open_duration": _duration((timezone.now() - du.open_since).total_seconds() // 60) if du.open_since else "",
            "modes": [m for m in DanteSettings.MODES if m[0] != DanteSettings.MODE_OFF],
        })
        ctx.update(self._preview(request, st, du, today))
        return render(request, "admin/worktime/calendar.html", ctx)

    @staticmethod
    def _pick(request, people):
        want = request.POST.get("u") or request.GET.get("u")
        if want:
            return next((p for p in people if str(p.pk) == str(want)), None)
        return next((p for p in people if p.user_id == request.user.pk), None) or (people[0] if people else None)

    @staticmethod
    def _grid(st, du, first, last, days, today):
        weeks, week = [], []
        totals = {"minutes": 0, "worked": 0, "empty": 0, "absent": 0}
        start = first - datetime.timedelta(days=first.weekday())
        end = last + datetime.timedelta(days=6 - last.weekday())
        day = start
        while day <= end:
            wd = days.get(day)
            cell = {"date": day, "in_month": first <= day <= last, "today": day == today, "times": [],
                    "minutes": 0, "kind": "", "label": "", "activity": None}
            if cell["in_month"]:
                blocker = services.day_blocker(st, wd, day)
                cell["times"] = (wd.times if wd else None) or []
                cell["minutes"] = wd.minutes if wd else 0
                cell["dur"] = _duration(cell["minutes"])
                if cell["times"]:
                    cell["kind"] = "filled"
                    totals["worked"] += 1
                    totals["minutes"] += cell["minutes"]
                elif day > today:
                    cell["kind"] = "future"
                    cell["label"] = blocker if blocker and blocker != "вихідний" else ""
                elif blocker:
                    cell["kind"] = "off" if blocker in ("вихідний", "неробочий день") else "absent"
                    cell["label"] = blocker
                    totals["absent"] += cell["kind"] == "absent"
                else:
                    cell["kind"] = "empty"
                    totals["empty"] += day < today
                if day <= today and du.user_id:
                    span = services.activity_span(du.user, day)
                    if span:
                        cell["activity"] = f"{span[0]:%H:%M}–{span[1]:%H:%M} · {span[2]}"
            week.append(cell)
            if len(week) == 7:
                weeks.append(week)
                week = []
            day += datetime.timedelta(days=1)
        totals["hours"] = _duration(totals["minutes"])
        return weeks, totals

    def _preview(self, request, st, du, today):
        """Перегляд «Заповнити пропуски» (GET): що буде записано в кожен день — нічого не записує."""
        g = request.GET
        mode = g.get("mode") if g.get("mode") in (DanteSettings.MODE_FIXED, DanteSettings.MODE_ACTIVITY) \
            else DanteSettings.MODE_FIXED
        out = {"fill_from": st.autofill_since or today.replace(day=1), "fill_to": today - datetime.timedelta(days=1),
               "fill_mode": mode, "fill_fallback": st.activity_fallback}
        if "fill_from" not in g:
            return out
        date_from, date_to = _date(g.get("fill_from")), _date(g.get("fill_to"))
        start, stop = _time(g.get("start")), _time(g.get("stop"))
        out.update(fill_from=date_from or out["fill_from"], fill_to=date_to or out["fill_to"],
                   fill_start=hhmm(start), fill_stop=hhmm(stop), fill_fallback=g.get("fallback") == "1")
        if not date_from or not date_to or date_to < date_from:
            out["fill_error"] = "Вкажіть період: від і до."
        elif (date_to - date_from).days >= MAX_FILL_DAYS:
            out["fill_error"] = f"Не більше {MAX_FILL_DAYS} днів за раз."
        elif not st.enabled:
            out["fill_error"] = "askDANTE вимкнено в налаштуваннях."
        else:
            try:
                days = services.refresh_days(du, date_from, min(date_to, today))
                plan = services.propose(st, du, days, date_from, date_to, mode, start, stop,
                                        fallback=out["fill_fallback"])
                out["plan"] = plan
                out["plan_book"] = sum(p.book for p in plan)
                out["plan_minutes"] = _duration(sum(p.minutes for p in plan if p.book))
                out["plan_attendance_error"] = getattr(days, "attendance_error", "")
            except DanteError as e:
                out["fill_error"] = e.message
        return out

    # ── дії ──────────────────────────────────────────────────────
    def _post(self, request, st, du):
        p = request.POST
        back_q = f"?u={du.pk}&m={p.get('m', '')}" if du else ""
        back = redirect(reverse("admin:worktime_workday_changelist") + back_q)
        if du is None:
            messages.error(request, "Працівника не знайдено або немає доступу.")
            return back
        if not st.enabled:
            messages.error(request, "askDANTE вимкнено в налаштуваннях.")
            return back
        action = p.get("action")
        try:
            if action == "fill":
                self._do_fill(request, st, du)
            elif action == "book":
                day, start, stop = _date(p.get("date")), _time(p.get("start")), _time(p.get("stop"))
                if not (day and start and stop):
                    raise DanteError("bad_request", "Вкажіть дату, прихід і відхід.")
                b = services.book_day(du, day, start, stop, user=request.user, st=st)
                self._report_booking(request, b)
            elif action == "clock_in":
                since = services.clock_in(du)
                messages.success(request, f"▶ Прийшов о {timezone.localtime(since):%H:%M}. В askDANTE запис піде, "
                                          "коли натиснете «Пішов».")
            elif action == "clock_out":
                for b in services.clock_out(du, user=request.user):
                    self._report_booking(request, b)
            elif action == "clock_cancel":
                services.clock_cancel(du)
                messages.info(request, "Відмітку «Прийшов» скасовано, в askDANTE нічого не записано.")
        except DanteError as e:
            messages.error(request, f"❌ {e.message}")
        return back

    @staticmethod
    def _report_booking(request, b):
        if b.status == Booking.SENT:
            messages.success(request, f"✅ Записано в askDANTE: {b.date:%d.%m.%Y} {hhmm(b.start)}–{hhmm(b.stop)}")
        else:
            messages.error(request, f"❌ {b.date:%d.%m.%Y}: {b.error}")

    @staticmethod
    def _do_fill(request, st, du):
        p = request.POST
        date_from, date_to = _date(p.get("fill_from")), _date(p.get("fill_to"))
        dates = {d for d in (_date(x) for x in p.getlist("dates")) if d}
        if not (date_from and date_to and dates):
            raise DanteError("bad_request", "Не вибрано жодного дня.")
        mode = p.get("mode") if p.get("mode") in (DanteSettings.MODE_FIXED, DanteSettings.MODE_ACTIVITY) \
            else DanteSettings.MODE_FIXED
        r = services.fill(du, date_from, date_to, mode, _time(p.get("start")), _time(p.get("stop")),
                          fallback=p.get("fallback") == "1", only_dates=dates, user=request.user, st=st)
        if r["sent"]:
            messages.success(request, f"✅ Записано в askDANTE днів: {len(r['sent'])}.")
        for d, why in r["skipped"]:
            messages.warning(request, f"{d:%d.%m.%Y} пропущено: {why}")
        for d, why in r["failed"]:
            messages.error(request, f"❌ {d:%d.%m.%Y}: {why}")


# ── Проєкти askDANTE ↔ Minerva ───────────────────────────────────
@admin.register(DanteProject)
class DanteProjectAdmin(admin.ModelAdmin):
    """Список = сторінка зв'язків: проєкти Minerva ↔ askDANTE (askDANTE лишається своїм довідником)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [path("", self.admin_site.admin_view(self.mapping_view), name="worktime_danteproject_changelist")] \
            + super().get_urls()

    def mapping_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        from projects.models import Project
        show = request.GET.get("show", "open")
        if request.method == "POST":
            if not services.is_manager(request.user):
                raise PermissionDenied
            self._mapping_post(request)
            return redirect(f"{reverse('admin:worktime_danteproject_changelist')}?show={show}")

        dante = list(DanteProject.objects.filter(gone=False).select_related("parent")
                     .annotate(n_maps=Count("maps")))
        qs = Project.objects.select_related("customer", "dante_map__dante_project").order_by("-updated_at")
        if show != "all":
            qs = qs.filter(status__in=Project.OPEN_STATUSES)
        rows = []
        usable = [d for d in dante if not d.archived and d.time_trackable]
        for pr in qs[:500]:
            link = getattr(pr, "dante_map", None)
            rows.append({"project": pr, "link": link,
                         "suggestion": None if link else mapping.suggest(pr, usable)})
        st = DanteSettings.get()
        ctx = {**self.admin_site.each_context(request), "title": "📁 Проєкти askDANTE ↔ Minerva",
               "opts": self.model._meta, "rows": rows, "dante": dante, "usable": usable, "show": show,
               "st": st, "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
               "can_edit": services.is_manager(request.user),
               "unlinked_dante": [d for d in dante if not d.n_maps and not d.archived]}
        return render(request, "admin/worktime/mapping.html", ctx)

    @staticmethod
    def _mapping_post(request):
        from projects.models import Project
        p = request.POST
        if p.get("accept"):
            pr = Project.objects.filter(pk=p["accept"]).first()
            dp = DanteProject.objects.filter(pk=p.get(f"suggest_{p['accept']}")).first()
            if pr and dp:
                mapping.link(pr, dp, request.user)
                messages.success(request, f"{pr.code} → {dp}")
            return
        if p.get("create"):
            pr = Project.objects.filter(pk=p["create"]).first()
            if pr is None:
                return
            try:
                dp = mapping.create_in_dante(pr, request.user)
                messages.success(request, f"✅ В askDANTE створено проєкт «{dp.name}» і прив'язано до {pr.code}.")
            except DanteError as e:
                messages.error(request, f"❌ {e.message}")
            return
        changed = 0
        for key, value in p.items():
            if not key.startswith("map_"):
                continue
            pr = Project.objects.filter(pk=key[4:]).first()
            if pr is None:
                continue
            current = ProjectMap.objects.filter(project=pr).values_list("dante_project_id", flat=True).first()
            new = int(value) if str(value).isdigit() else None
            if new == current:
                continue
            if new is None:
                ProjectMap.objects.filter(project=pr).delete()
            else:
                dp = DanteProject.objects.filter(pk=new).first()
                if dp:
                    mapping.link(pr, dp, request.user)
            changed += 1
        messages.success(request, f"Збережено зв'язків: {changed}." if changed else "Змін немає.")
