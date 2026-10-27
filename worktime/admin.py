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


def parse_minutes(value) -> int:
    """Тривалість з форми: «1:30», «90» (хв), «1,5» / «1.5» (год), «2h», «45m»; 0 — не розібрано."""
    text = str(value or "").strip().lower().replace(" ", "")
    try:
        if ":" in text:
            h, m = text.split(":", 1)
            return int(h or 0) * 60 + int(m or 0)
        if text.endswith(("h", "г", "год")):
            return round(float(text.rstrip("hгод").replace(",", ".")) * 60)
        if text.endswith(("m", "хв")):
            return int(text.rstrip("mхв"))
        if "," in text or "." in text:
            return round(float(text.replace(",", ".")) * 60)
        return int(text)
    except ValueError:
        return 0


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
                           "лікарняного чи свята; день, де вже внесено прихід і відхід, пропускається. Дні з "
                           "попередженнями (понад 10 годин, не прочитано свята) лишаються на перевірку в календарі. "
                           "«▶ Заповнити зараз» — те саме одразу, без очікування години запуску; після кожного "
                           "запуску — звіт по днях: що записано, що вже було, що пропущено й чому.",
        }),
        ("📁 Час на проєкти й години", {
            "fields": ["default_activity", "records_days", "records_status"],
            "description": "Час на проєкти пишеться в askDANTE з календаря (☆ мої проєкти). Години проєктів і "
                           "клієнтів у Minerva — з копії записів askDANTE, оновлюється щоночі й кнопкою.",
        }),
    ]
    readonly_fields = ["calendar_link", "check_status", "autofill_status", "records_status"]

    @admin.display(description="Записи часу")
    def records_status(self, obj):
        from .models import TimeRecord
        last = (f"оновлено {timezone.localtime(obj.records_synced_at):%d.%m.%Y %H:%M}"
                if obj and obj.records_synced_at else "ще не оновлювались")
        return format_html('<div>{} · у Minerva записів: {}</div><button type="submit" class="button" formaction="{}" '
                           'formnovalidate>🔄 Оновити години з askDANTE</button>',
                           last, TimeRecord.objects.count(), reverse("admin:worktime_dantesettings_records"))

    def records_view(self, request):
        back = self._post_only(request)
        if request.method != "POST":
            return back
        try:
            r = services.sync_records()
            self.message_user(request, f"✅ Записів часу: {r['records']} ({r['from']:%d.%m.%Y}–{r['to']:%d.%m.%Y}), "
                                       f"прибрано зниклих: {r['removed']}.", messages.SUCCESS)
        except DanteError as e:
            self.message_user(request, f"❌ {e.message}", messages.ERROR)
        return back

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
        if obj and obj.last_autofill_at:
            how = "вручну («Заповнити зараз»)" if (obj.last_autofill_report or {}).get("manual") else "щоденний"
            last = format_html('Останній запуск {} ({}) — {} · <a href="{}">📋 Звіт по днях</a>',
                               f"{timezone.localtime(obj.last_autofill_at):%d.%m.%Y %H:%M}", how,
                               obj.last_autofill_result, reverse("admin:worktime_dantesettings_report"))
        else:
            last = "ще не запускалось"
        people = DanteUser.objects.filter(autofill=True, archived=False).count()
        date_from, date_to = services.autofill_range(obj or DanteSettings.get())
        confirm = (f"Записати в askDANTE порожні робочі дні {date_from:%d.%m.%Y}–{date_to:%d.%m.%Y} "
                   f"(працівників з автозаповненням: {people})? Дні, де вже є прихід і відхід, відпустка чи свято, "
                   "не чіпаються. Записи askDANTE через API не видаляються.")
        return format_html(
            '<div>{}</div><div class="help">Працівників з автозаповненням: {} · період зараз: {}–{} '
            '(«Перевіряти днів назад», не раніше «Не раніше дати», до вчора). Кнопки працюють зі збереженими '
            'налаштуваннями — спершу збережіть зміни.</div><div style="margin-top:6px">'
            '<button type="submit" class="button" formaction="{}" formnovalidate '
            'onclick="return confirm(this.dataset.confirm)" data-confirm="{}">▶ Заповнити зараз</button> '
            '<button type="submit" class="button" formaction="{}" formnovalidate>🧪 Що записалось би зараз '
            '(без запису)</button></div>',
            last, people, f"{date_from:%d.%m.%Y}", f"{date_to:%d.%m.%Y}",
            reverse("admin:worktime_dantesettings_fillnow"), confirm, reverse("admin:worktime_dantesettings_dryrun"))

    def get_urls(self):
        view = self.admin_site.admin_view
        return [path("check/", view(self.check_view), name="worktime_dantesettings_check"),
                path("sync/", view(self.sync_view), name="worktime_dantesettings_sync"),
                path("fill-now/", view(self.fillnow_view), name="worktime_dantesettings_fillnow"),
                path("dry-run/", view(self.dryrun_view), name="worktime_dantesettings_dryrun"),
                path("report/", view(self.report_view), name="worktime_dantesettings_report"),
                path("records/", view(self.records_view), name="worktime_dantesettings_records")] + super().get_urls()

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

    def _autofill_problem(self, st) -> str:
        if not st.configured:
            return "Спершу вкажіть адресу й API-Key askDANTE і збережіть."
        if st.autofill_mode == DanteSettings.MODE_OFF:
            return "Автозаповнення вимкнено — виберіть режим («Стандартний робочий день» або «За активністю») і збережіть."
        return ""

    def fillnow_view(self, request):
        """«▶ Заповнити зараз» — те саме, що щоденний запуск, але одразу; звіт зберігається й відкривається."""
        back = self._post_only(request)
        if request.method != "POST":
            return back
        st = DanteSettings.get()
        problem = self._autofill_problem(st) or ("" if st.enabled else "Увімкніть askDANTE (галочка вгорі) і збережіть.")
        if problem:
            self.message_user(request, problem, messages.WARNING)
            return back
        report = services.autofill_report(st, manual=True)
        failed = report["counts"]["failed"] or any(u["error"] for u in report["users"])
        self.message_user(request, report["summary"], messages.WARNING if failed else messages.SUCCESS)
        return redirect(reverse("admin:worktime_dantesettings_report"))

    def dryrun_view(self, request):
        back = self._post_only(request)
        if request.method != "POST":
            return back
        st = DanteSettings.get()
        problem = self._autofill_problem(st)
        if problem:
            self.message_user(request, problem, messages.WARNING)
            return back
        return self._render_report(request, services.autofill_report(st, dry_run=True))

    def report_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        return self._render_report(request, DanteSettings.get().last_autofill_report or {})

    def _render_report(self, request, report: dict):
        users = []
        for u in report.get("users", []):
            rows = []
            for r in u["days"]:
                day = _date(r["date"])
                rows.append({**r, "day": day, "wd": WEEKDAYS[day.weekday()], "month": f"{day:%Y-%m}",
                             "label": services.REPORT_STATUSES.get(r["status"], r["status"])})
            users.append({**u, "rows": rows})
        counts = report.get("counts", {})
        try:
            at = timezone.localtime(datetime.datetime.fromisoformat(report["at"]))
        except (KeyError, ValueError):
            at = None
        st = DanteSettings.get()
        context = {**self.admin_site.each_context(request), "title": "Звіт автозаповнення", "report": report,
                   "users": users, "at": at, "from": _date(report.get("from")), "to": _date(report.get("to")),
                   "mode": dict(DanteSettings.MODES).get(report.get("mode"), ""),
                   "counts": [(k, label, counts.get(k, 0)) for k, label in services.REPORT_STATUSES.items()
                              if counts.get(k)],
                   "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
                   "calendar_url": reverse("admin:worktime_workday_changelist"),
                   "users_url": reverse("admin:worktime_danteuser_changelist")}
        return render(request, "admin/worktime/autofill_report.html", context)


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
        balance = None
        if st.enabled and first <= today:
            try:   # час на проєкти за місяць — свіжий; місячний підсумок — кеш на годину
                services.sync_records(st, first, min(last, today))
            except DanteError as e:
                error = error or f"Записи часу askDANTE не оновлено: {e.message}"
            balance = services.balance(du, first.year, first.month)
        records = services.day_records(du, first, last)

        weeks, totals = self._grid(st, du, first, last, cached, today, records)
        from .models import DanteActivity
        ctx.update({
            "balance": services.balance_lines(balance), "balance_error": balance.error if balance else "",
            "my_projects": list(services.my_projects(request.user)),
            "activities": list(DanteActivity.objects.filter(archived=False)),
            "default_activity": st.default_activity_id,
            "project_date": today if first <= today <= last else min(last, today),
            "mapping_url": reverse("admin:worktime_danteproject_changelist"),
        })
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
    def _grid(st, du, first, last, days, today, records=None):
        weeks, week = [], []
        records = records or {}
        totals = {"minutes": 0, "worked": 0, "empty": 0, "absent": 0, "project_minutes": 0}
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
                if cell["kind"] == "filled":   # пів дня відпустки поруч із роботою
                    cell["absences"] = services.absences_text(wd)
                cell["projects"] = [{"name": str(r.dante_project or r.project_name), "dur": _duration(r.duration),
                                     "title": " · ".join(x for x in (r.activity_name, r.description) if x)}
                                    for r in records.get(day, [])]
                totals["project_minutes"] += sum(r.duration for r in records.get(day, []))
            week.append(cell)
            if len(week) == 7:
                weeks.append(week)
                week = []
            day += datetime.timedelta(days=1)
        totals["hours"] = _duration(totals["minutes"])
        totals["projects"] = _duration(totals["project_minutes"])
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
        nxt = p.get("next") or ""
        if nxt.startswith("/calendar/") and "//" not in nxt:   # форма з календаря Minerva — назад туди
            back = redirect(nxt)
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
            elif action == "project":
                self._report_project(request, self.book_project_from(p, st, du, request.user))
        except DanteError as e:
            messages.error(request, f"❌ {e.message}")
        return back

    @staticmethod
    def book_project_from(p, st, du, user, source=Booking.MANUAL):
        """Запис часу на проєкт з форми (календар робочого часу або календар Minerva)."""
        from .models import DanteActivity
        day, minutes = _date(p.get("date")), parse_minutes(p.get("duration"))
        dp = services.my_projects(user).filter(pk=p.get("project") or 0).first() \
            or DanteProject.objects.filter(pk=p.get("project") or 0, gone=False).first()
        if not (day and dp and minutes):
            raise DanteError("bad_request", "Вкажіть дату, проєкт і тривалість (напр. 1:30 або 90).")
        activity = DanteActivity.objects.filter(pk=p.get("activity") or 0).first()
        return services.book_project(du, day, dp, minutes, activity, p.get("comment", ""), user=user, source=source,
                                     st=st)

    @staticmethod
    def _report_project(request, b):
        if b.status == Booking.SENT:
            messages.success(request, f"✅ Записано в askDANTE: {b.date:%d.%m.%Y} · {b.dante_project} · "
                                      f"{_duration(b.duration)}")
        else:
            messages.error(request, f"❌ {b.date:%d.%m.%Y} · {b.dante_project}: {b.error}")

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
        if request.method == "POST":
            back = request.get_full_path()
            if request.POST.get("pin"):   # ☆ — особистий вибір, може кожен
                dp = DanteProject.objects.filter(pk=request.POST["pin"]).first()
                if dp:
                    services.toggle_pin(request.user, dp)
                return redirect(back)
            if not services.is_manager(request.user):
                raise PermissionDenied
            self._mapping_post(request)
            return redirect(back)
        if request.GET.get("view", "dante") == "dante":
            return self._dante_view(request)
        return self._minerva_view(request)

    FILTERS = [("mine", "☆ Мої"), ("all", "Усі"), ("nocrm", "Без клієнта CRM"),
               ("nominerva", "Без проєкту Minerva"), ("archived", "Архів askDANTE")]

    def _dante_view(self, request):
        """Проєкти askDANTE: ☆ мої, клієнт CRM, проєкти Minerva, години за останні N днів."""
        from django.db.models import Exists, OuterRef, Q as Q_, Subquery, Sum
        from .models import TimeRecord
        user = request.user
        pinned = DanteProject.pinned_by.through.objects.filter(danteproject_id=OuterRef("pk"), user_id=user.pk)
        minutes = TimeRecord.objects.filter(dante_project=OuterRef("pk")).values("dante_project") \
            .annotate(m=Sum("duration")).values("m")[:1]
        base = DanteProject.objects.filter(gone=False)
        has_mine = base.filter(pinned_by=user).exists()
        f = request.GET.get("f") or ("mine" if has_mine else "all")
        q = (request.GET.get("q") or "").strip()
        qs = base.select_related("parent", "crm_customer").prefetch_related("maps__project") \
            .annotate(pinned=Exists(pinned), minutes=Subquery(minutes))
        qs = qs.filter(archived=True) if f == "archived" else qs.filter(archived=False)
        qs = {"mine": qs.filter(pinned=True), "nocrm": qs.filter(crm_customer__isnull=True),
              "nominerva": qs.filter(maps__isnull=True)}.get(f, qs)
        if q:
            qs = qs.filter(Q_(name__icontains=q) | Q_(reference_number__icontains=q) | Q_(customer_name__icontains=q)
                           | Q_(crm_customer__company__icontains=q) | Q_(crm_customer__name__icontains=q))
        can_edit = services.is_manager(user)
        index = mapping.customer_index() if can_edit else []
        rows = []
        for dp in qs.distinct().order_by("-pinned", "name")[:500]:
            rows.append({"dp": dp, "maps": list(dp.maps.all()), "hm": _duration(dp.minutes) if dp.minutes else "",
                         "suggest": mapping.suggest_customer(dp, index) if can_edit and not dp.crm_customer_id
                         else None})
        live = base.filter(archived=False)
        counts = {"mine": live.filter(pinned_by=user).count(), "all": live.count(),
                  "nocrm": live.filter(crm_customer__isnull=True).count(),
                  "nominerva": live.filter(maps__isnull=True).count(), "archived": base.filter(archived=True).count()}
        st = DanteSettings.get()
        names = []
        if can_edit:
            from crm.models import Customer
            names = sorted({c or n for c, n in Customer.objects.values_list("company", "name")[:3000] if c or n})
        ctx = {**self.admin_site.each_context(request), "title": "📁 Проєкти askDANTE ↔ Minerva",
               "opts": self.model._meta, "view": "dante", "rows": rows, "f": f, "q": q, "has_mine": has_mine,
               "filters": [(k, label, counts[k]) for k, label in self.FILTERS], "can_edit": can_edit,
               "customer_names": names, "st": st, "any": base.exists(),
               "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk])}
        return render(request, "admin/worktime/mapping.html", ctx)

    def _minerva_view(self, request):
        from projects.models import Project
        show = request.GET.get("show", "open")
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
        ctx = {**self.admin_site.each_context(request), "title": "📁 Проєкти askDANTE ↔ Minerva", "view": "minerva",
               "opts": self.model._meta, "rows": rows, "dante": dante, "usable": usable, "show": show,
               "st": st, "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
               "can_edit": services.is_manager(request.user),
               "unlinked_dante": [d for d in dante if not d.n_maps and not d.archived]}
        return render(request, "admin/worktime/mapping.html", ctx)

    @staticmethod
    def _mapping_post(request):
        from projects.models import Project
        p = request.POST
        if p.get("crm_accept"):   # підказка клієнта CRM для проєкту askDANTE
            dp = DanteProject.objects.filter(pk=p["crm_accept"]).first()
            from crm.models import Customer
            customer = Customer.objects.filter(pk=p.get(f"crm_suggest_{p['crm_accept']}")).first()
            if dp and customer:
                DanteProject.objects.filter(pk=dp.pk).update(crm_customer=customer)
                messages.success(request, f"{dp} → клієнт CRM «{customer}»")
            return
        if p.get("crm_clear"):
            DanteProject.objects.filter(pk=p["crm_clear"]).update(crm_customer=None)
            messages.success(request, "Клієнта CRM відв'язано.")
            return
        if p.get("to_minerva"):
            dp = DanteProject.objects.filter(pk=p["to_minerva"]).first()
            if dp and not dp.maps.exists():
                pr = mapping.create_in_minerva(dp, request.user)
                messages.success(request, f"✅ У Minerva створено проєкт {pr.code} «{pr.name}», зв'язаний з askDANTE.")
            return
        typed = [(k[4:], v.strip()) for k, v in p.items() if k.startswith("crm_") and k[4:].isdigit() and v.strip()]
        if typed:
            done, missed = 0, []
            for pk, text in typed:
                dp = DanteProject.objects.filter(pk=pk).first()
                customer = mapping.find_customer(text)
                if dp and customer:
                    DanteProject.objects.filter(pk=dp.pk).update(crm_customer=customer)
                    done += 1
                elif dp:
                    missed.append(f"«{text}»")
            if done:
                messages.success(request, f"Прив'язано клієнтів CRM: {done}.")
            if missed:
                messages.warning(request, "Не знайдено одного клієнта CRM за: " + ", ".join(missed)
                                 + " — уточніть назву (або #номер клієнта).")
            return
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
