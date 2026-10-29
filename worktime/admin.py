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
MATCH_LIMIT = 200       # проєктів Minerva з балами зв'язку за одну сторінку
AUTO_ACCEPT = 80        # «✓ Прийняти всі з балом ≥ …»


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


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _activity_label(span) -> str:
    """👣 08:28 · 1 дія  /  👣 08:05–17:40 · 52 дії"""
    first, last, n = span
    when = f"{first:%H:%M}" if f"{first:%H:%M}" == f"{last:%H:%M}" else f"{first:%H:%M}–{last:%H:%M}"
    return f"{when} · {n} {_plural(n, 'дія', 'дії', 'дій')}"


def _activity_title(span) -> str:
    first, last, n = span
    return (f"Сліди роботи в Minerva за день: перша дія о {first:%H:%M}, остання о {last:%H:%M}, усього {n} "
            f"(вхід, збереження записів, надіслані листи). Одна дія — зазвичай лише вхід. Так бачить день режим "
            f"автозаповнення «за активністю»; в askDANTE це не записується саме.")


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
            "fields": ["default_activity", "durations_allowed", "activity_required", "attendance_required",
                       "records_days", "records_status"],
            "description": "Час на проєкти пишеться в askDANTE з календаря (☆ мої проєкти). Години проєктів і "
                           "клієнтів у Minerva — з копії записів askDANTE, оновлюється щоночі й кнопкою.",
        }),
        ("✉️ Час на пошту", {
            "fields": ["mail_time", "mail_projects", "mail_projects_auto", "mail_reply_minutes", "mail_read_minutes",
                       "mail_step", "comment_lang", "comment_ai", "mail_time_status"],
            "description": "Minerva оцінює за нормами, скільки часу пішло на листи: свої категорії листів "
                           "(🏷 DigiKey, магазин… — проєкт askDANTE і хвилини задаються в самій категорії) і "
                           "гілки проєктів Minerva, зв'язаних з askDANTE. Оцінка по днях — «✉️ Час на пошту»; "
                           "в askDANTE записується після ✓ там або щодня автоматично (галочка в категорії). "
                           "Записане не дублюється — наступного разу дописується лише різниця.",
        }),
    ]
    readonly_fields = ["calendar_link", "check_status", "autofill_status", "records_status", "mail_time_status"]

    @admin.display(description="Стан")
    def mail_time_status(self, obj):
        url = reverse("admin:worktime_workday_mailtime")
        cats = reverse("admin:email_assistant_mailcategory_changelist")
        return format_html('<div>{}</div><a href="{}">✉️ Час на пошту — по днях</a> · <a href="{}">🏷 Свої категорії '
                           'листів і їхні проєкти askDANTE</a>', obj.mail_time_result if obj else "", url, cats)

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
        plan = services.autofill_schedule(obj or DanteSettings.get())
        seen = (f" · cron перевіряв {timezone.localtime(obj.cron_seen_at):%d.%m %H:%M}"
                if obj and obj.cron_seen_at else "")
        last = format_html('{}<div>Наступний щоденний запуск: {}{}</div>{}', last, plan["next"], seen,
                           format_html('<div style="color:#d97706;font-weight:600">⚠️ {}</div>', plan["warning"])
                           if plan["warning"] else "")
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
    list_display = ["__str__", "email", "personnel_number", "user", "autofill", "start", "stop", "report_email",
                    "archived", "calendar"]
    list_editable = ["user", "autofill", "start", "stop", "report_email"]
    list_filter = ["archived", "autofill"]
    search_fields = ["first_name", "last_name", "email", "personnel_number"]
    readonly_fields = ["dante_id", "first_name", "last_name", "email", "personnel_number", "archived",
                       "open_since", "synced_at"]
    fields = ["dante_id", "first_name", "last_name", "email", "personnel_number", "archived", "user",
              "autofill", "start", "stop", "report_email", "open_since", "synced_at"]

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
        return [path("", self.admin_site.admin_view(self.calendar_view), name="worktime_workday_changelist"),
                path("report/", self.admin_site.admin_view(self.report_view), name="worktime_workday_report"),
                path("mail-time/", self.admin_site.admin_view(self.mail_time_view), name="worktime_workday_mailtime"),
                path("from-mail/<int:message_pk>/", self.admin_site.admin_view(self.from_mail_view),
                     name="worktime_workday_from_mail"),
                path("meetings/", self.admin_site.admin_view(self.meetings_view), name="worktime_workday_meetings"),
                path("record/describe/", self.admin_site.admin_view(self.record_describe_view),
                     name="worktime_workday_record_describe"),
                path("record/save/", self.admin_site.admin_view(self.record_save_view),
                     name="worktime_workday_record_save"),
                path("day/", self.admin_site.admin_view(self.day_view), name="worktime_workday_day")] \
            + super().get_urls()

    # ── ✍️ запис askDANTE з календаря: опис AI, правка, заміна або новий (record_ai.py) ──
    def _record_target(self, request, p):
        du = next((x for x in services.users_for(request.user) if str(x.pk) == str(p.get("u"))), None)
        if du is None:
            raise PermissionDenied
        dp = DanteProject.objects.filter(pk=p.get("project") or 0, gone=False).first()
        return du, dp

    def from_mail_view(self, request, message_pk):
        from .admin_mail import from_mail_page
        if not self.has_view_permission(request):
            raise PermissionDenied
        return from_mail_page(self, request, message_pk)

    def record_describe_view(self, request):
        """POST {u, date, project, start, stop, current, hint} → {ok, text, sources} (нічого не записує)."""
        from django.http import JsonResponse
        from . import record_ai
        if request.method != "POST" or not self.has_view_permission(request):
            raise PermissionDenied
        p = request.POST
        du, dp = self._record_target(request, p)
        day = _date(p.get("date"))
        if not (day and dp):
            return JsonResponse({"ok": False, "error": "Виберіть дату й проєкт askDANTE."})
        try:
            from email_assistant.models import EmailMessage
            msg = EmailMessage.objects.filter(pk=p["msg"], account__user=request.user).first() \
                if str(p.get("msg") or "").isdigit() else None   # з листа — опис за цим листом
            r = record_ai.describe(du, day, dp, _time(p.get("start")), _time(p.get("stop")),
                                   p.get("current", ""), p.get("hint", ""), user=request.user, message=msg)
        except DanteError as e:
            return JsonResponse({"ok": False, "error": e.message})
        return JsonResponse({"ok": True, **r})

    def record_save_view(self, request):
        """POST mode=new — новий запис; mode=replace + rec — замість запису askDANTE (після видалення там)."""
        from . import record_ai
        from .models import DanteActivity, TimeRecord
        if request.method != "POST" or not self.has_view_permission(request):
            raise PermissionDenied
        p = request.POST
        du, dp = self._record_target(request, p)
        back = f"{reverse('admin:worktime_workday_changelist')}?u={du.pk}&m={p.get('m', '')}"
        day_url = reverse("admin:worktime_workday_day")
        mail_url = reverse("admin:worktime_workday_mailtime")
        if (p.get("next") or "").startswith((day_url + "?", mail_url + "?")):   # зі сторінки дня / пошти — туди ж
            back = p["next"]
        st = DanteSettings.get()
        start, stop = _time(p.get("start")), _time(p.get("stop"))
        minutes = parse_minutes(p.get("duration"))
        activity = DanteActivity.objects.filter(pk=p.get("activity") or 0).first()
        comment = record_ai.clean(p.get("comment", ""))
        try:
            if dp is None:
                raise DanteError("bad_request", "Виберіть проєкт askDANTE.")
            if p.get("mode") == "replace":
                rec = TimeRecord.objects.filter(pk=p.get("rec") or 0, dante_user=du).first()
                if rec is None:
                    raise DanteError("not_found", "Запис askDANTE не знайдено — оновіть сторінку.")
                b = record_ai.replace(du, rec, dp, start, stop, minutes or rec.duration, activity, comment,
                                      user=request.user, st=st)
            else:
                day = _date(p.get("date"))
                if not (day and (minutes or (start and stop))):
                    raise DanteError("bad_request", "Вкажіть дату й «з … по …» (або тривалість).")
                b = services.book_project(du, day, dp, minutes, activity, comment, user=request.user, st=st,
                                          start=start, stop=stop, attendance=p.get("attendance", ""))
            self._report_project(request, b)
            if b.status != Booking.SENT and p.get("mode") == "replace":
                back += f"&rec={p.get('rec')}"
        except DanteError as e:
            messages.error(request, f"❌ {e.message}")
            if p.get("mode") == "replace":   # вікно відкриється знову з тим самим текстом (чернетка в браузері)
                back += f"&rec={p.get('rec')}"
        return redirect(back)

    def day_view(self, request):
        from .admin_day import day_page
        if not self.has_view_permission(request):
            raise PermissionDenied
        return day_page(self, request)

    def meetings_view(self, request):
        from .admin_meetings import meetings_page
        if not self.has_view_permission(request):
            raise PermissionDenied
        return meetings_page(self, request)

    def mail_time_view(self, request):
        from .admin_mail import mail_time_page
        if not self.has_view_permission(request):
            raise PermissionDenied
        return mail_time_page(self, request)

    # ── звіт по працівниках ──────────────────────────────────────
    def report_view(self, request):
        """Звіт за період: адміністратор — по всіх (або вибраних), інший — лише свій. «✉️ Надіслати» — поштою."""
        from . import reports
        from .models import WorkReport
        if not self.has_view_permission(request):
            raise PermissionDenied
        manager = services.is_manager(request.user)
        people = list(services.users_for(request.user))
        g = request.POST if request.method == "POST" else request.GET
        today = services.local_today()
        preset = g.get("preset", "")
        if preset == "last_week":
            date_from, date_to = reports.period_of(WorkReport(period=WorkReport.WEEK), today)
        elif preset == "last_month":
            date_from, date_to = reports.period_of(WorkReport(period=WorkReport.MONTH), today)
        else:
            date_from = _date(g.get("from")) or today.replace(day=1)
            date_to = _date(g.get("to")) or today
        if date_to < date_from or (date_to - date_from).days > 92:
            messages.error(request, "Період — до 3 місяців, «до» не раніше «від».")
            date_to = min(max(date_to, date_from), date_from + datetime.timedelta(days=92))
        chosen = {str(x) for x in g.getlist("u")} if hasattr(g, "getlist") else set()
        selected = [p for p in people if not chosen or str(p.pk) in chosen]
        if not manager:
            selected = [p for p in selected if p.user_id == request.user.pk]
        show_types = manager and g.get("types") == "1"
        report = reports.build(selected, date_from, date_to) if selected else None
        if request.method == "POST" and request.POST.get("send") and report:
            to = reports.addresses(request.POST.get("to", ""))
            sender = None
            if request.POST.get("sender"):
                from email_assistant.models import EmailAccount
                sender = EmailAccount.objects.filter(pk=request.POST["sender"], user=request.user).first()
            html, text = reports.render(report, "Робочий час askDANTE", show_types)
            err = reports.deliver(sender, to, f"Робочий час askDANTE · {date_from:%d.%m}–{date_to:%d.%m.%Y}",
                                  text, html)
            (messages.error if err else messages.success)(
                request, f"❌ {err}" if err else f"✉️ Надіслано: {', '.join(to)}")
        from email_assistant.models import EmailAccount
        html = reports.render(report, "Робочий час askDANTE", show_types)[0] if report else ""
        ctx = {**self.admin_site.each_context(request), "title": "📊 Звіт по працівниках askDANTE",
               "opts": self.model._meta, "people": people, "chosen": chosen, "from": date_from, "to": date_to,
               "report_html": html, "manager": manager, "show_types": show_types, "report": report,
               "accounts": EmailAccount.objects.filter(user=request.user, is_active=True),
               "me": request.user.email, "schedules": WorkReport.objects.all() if manager else [],
               "schedules_url": reverse("admin:worktime_workreport_changelist"),
               "users_url": reverse("admin:worktime_danteuser_changelist")}
        return render(request, "admin/worktime/report.html", ctx)

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
        mail = {}
        if st.mail_time and first <= today:
            from . import mail_time
            mail = mail_time.estimate(du, first, min(last, today), st)

        weeks, totals = self._grid(st, du, first, last, cached, today, records, mail)
        from .models import DanteActivity
        ctx.update({
            "balance": services.balance_lines(balance), "balance_error": balance.error if balance else "",
            "my_projects": list(services.my_projects(request.user)),
            "activities": list(DanteActivity.objects.filter(archived=False)),
            "default_activity": st.default_activity_id,
            "project_date": today if first <= today <= last else min(last, today),
            "mapping_url": reverse("admin:worktime_danteproject_changelist"),
            "mail_url": reverse("admin:worktime_workday_mailtime") if st.mail_time else "",
            "day_url": reverse("admin:worktime_workday_day"),
            "rec_data": self._rec_data(records),
            "all_projects": list(DanteProject.objects.filter(archived=False, gone=False, time_trackable=True)
                                 .select_related("parent").order_by("name")),
            "meetings_url": reverse("admin:worktime_workday_meetings"),
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
    def _rec_data(records) -> dict:
        """Записи askDANTE місяця для вікна «✍️ Запис» (json_script)."""
        from .models import DanteActivity
        by_name = {a.name.lower(): a.pk for a in DanteActivity.objects.all()}
        return {str(r.pk): {"date": f"{r.day:%Y-%m-%d}", "start": r.start, "stop": r.stop, "minutes": r.duration,
                            "project": r.dante_project_id, "project_name": str(r.dante_project or r.project_name),
                            "activity": by_name.get((r.activity_name or "").lower()), "description": r.description}
                for day_records in records.values() for r in day_records}

    @staticmethod
    def _pick(request, people):
        want = request.POST.get("u") or request.GET.get("u")
        if want:
            return next((p for p in people if str(p.pk) == str(want)), None)
        return next((p for p in people if p.user_id == request.user.pk), None) or (people[0] if people else None)

    @staticmethod
    def _grid(st, du, first, last, days, today, records=None, mail=None):
        weeks, week = [], []
        records, mail = records or {}, mail or {}
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
                        cell["activity"] = _activity_label(span)
                        cell["activity_title"] = _activity_title(span)
                if cell["kind"] == "filled":   # пів дня відпустки поруч із роботою
                    cell["absences"] = services.absences_text(wd)
                cell["projects"] = [{"pk": r.pk, "name": str(r.dante_project or r.project_name),
                                     "dur": _duration(r.duration),
                                     "title": " · ".join(x for x in (r.activity_name, r.description) if x)}
                                    for r in records.get(day, [])]
                totals["project_minutes"] += sum(r.duration for r in records.get(day, []))
                rows = mail.get(day, {}).get("rows", [])
                if rows:
                    todo = sum(r["to_book"] for r in rows)
                    cell["mail"] = {"dur": _duration(sum(r["minutes"] for r in rows)),
                                    "todo": _duration(todo) if todo else "",
                                    "title": " · ".join(f"{r['label']} {_duration(r['minutes'])}" for r in rows)}
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
        start, stop = _time(p.get("start")), _time(p.get("stop"))
        dp = services.my_projects(user).filter(pk=p.get("project") or 0).first() \
            or DanteProject.objects.filter(pk=p.get("project") or 0, gone=False).first()
        if not (day and dp and (minutes or (start and stop))):
            raise DanteError("bad_request", "Вкажіть дату, проєкт і час: «з … по …» або тривалість (1:30, 90).")
        activity = DanteActivity.objects.filter(pk=p.get("activity") or 0).first()
        return services.book_project(du, day, dp, minutes, activity, p.get("comment", ""), user=user, source=source,
                                     st=st, start=start, stop=stop)

    @staticmethod
    def _report_project(request, b):
        if b.status == Booking.SENT:
            span = f"{hhmm(b.start)}–{hhmm(b.stop)} · " if b.start else ""
            messages.success(request, f"✅ Записано в askDANTE: {b.date:%d.%m.%Y} · {b.dante_project} · "
                                      f"{span}{_duration(b.duration)}")
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
        return [path("", self.admin_site.admin_view(self.mapping_view), name="worktime_danteproject_changelist"),
                path("categories/", self.admin_site.admin_view(self.categories_view),
                     name="worktime_danteproject_categories")] + super().get_urls()

    def categories_view(self, request):
        from .admin_setup import categories_page
        return categories_page(self, request)

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
        qs = base.select_related("parent", "crm_customer").prefetch_related("maps__project",
                                                                             "mail_categories__category") \
            .annotate(pinned=Exists(pinned), minutes=Subquery(minutes))
        qs = qs.filter(archived=True) if f == "archived" else qs.filter(archived=False)
        qs = {"mine": qs.filter(pinned=True), "nocrm": qs.filter(crm_customer__isnull=True),
              "nominerva": qs.filter(maps__isnull=True)}.get(f, qs)
        if q:
            qs = qs.filter(Q_(name__icontains=q) | Q_(reference_number__icontains=q) | Q_(customer_name__icontains=q)
                           | Q_(crm_customer__company__icontains=q) | Q_(crm_customer__name__icontains=q))
        g = request.GET
        cust, crm, top = g.get("cust", ""), g.get("crm", ""), g.get("top", "")
        sort = g.get("sort") if g.get("sort") in ("customer", "name", "hours") else "customer"
        if cust:
            qs = qs.filter(customer_name="") if cust == "-" else qs.filter(customer_name=cust)
        if crm:
            qs = qs.filter(crm_customer__isnull=True) if crm == "-" else qs.filter(crm_customer_id=crm or 0)
        if top.isdigit():
            qs = qs.filter(pk__in=services._tree(DanteProject.objects.filter(pk=top)))
        can_edit = services.is_manager(user)
        index = mapping.customer_index() if can_edit else []
        parents = {p.pk: p for p in base.only("pk", "name", "parent_id")}

        def lineage(dp):   # корінь і глибина в дереві askDANTE
            depth, node = 0, dp
            while node.parent_id and node.parent_id in parents and depth < 10:
                node, depth = parents[node.parent_id], depth + 1
            return node.name.lower(), depth

        rows = []
        for dp in qs.distinct()[:500]:
            root, depth = lineage(dp)
            group = str(dp.crm_customer) if dp.crm_customer_id else (dp.customer_name or "")
            rows.append({"dp": dp, "maps": list(dp.maps.all()), "hm": _duration(dp.minutes) if dp.minutes else "",
                         "group": group, "depth": depth, "root": root,
                         "suggest": mapping.suggest_customer(dp, index) if can_edit and not dp.crm_customer_id
                         else None})
        suggest = g.get("suggest") == "1" and can_edit
        if suggest:   # на вимогу: найкращий проєкт Minerva для проєктів askDANTE без зв'язку
            from projects.models import Project
            from . import matching
            free = [r for r in rows if not r["maps"]]
            ctx_m = matching.Context([r["dp"] for r in free])
            projects = list(Project.objects.filter(status__in=Project.OPEN_STATUSES, dante_map__isnull=True)
                            .select_related("customer").prefetch_related("participants")[:150])
            cache = {}
            for r in free:
                best = matching.best_minerva_project(r["dp"], projects, ctx_m, cache)
                r["minerva_suggest"] = (best[0], best[1], "; ".join(best[2])) if best else None
        if sort == "hours":
            rows.sort(key=lambda r: (-(r["dp"].minutes or 0), r["dp"].name.lower()))
        elif sort == "name":
            rows.sort(key=lambda r: (r["root"], r["depth"], r["dp"].name.lower()))
        else:   # за клієнтом: групи, усередині — дерево проєктів; без клієнта — в кінці
            rows.sort(key=lambda r: (r["group"] == "", r["group"].lower(), r["root"], r["depth"], r["dp"].name.lower()))
        if sort == "customer":
            prev = None
            for r in rows:
                r["header"] = r["group"] != prev
                prev = r["group"]
            sizes = {}
            for r in rows:
                sizes[r["group"]] = sizes.get(r["group"], 0) + 1
            for r in rows:
                r["group_size"] = sizes[r["group"]]
        live = base.filter(archived=False)
        from django.db.models import Count as Count_
        cust_options = list(live.exclude(customer_name="").values("customer_name").annotate(n=Count_("pk"))
                            .order_by("customer_name"))
        crm_options = list(live.exclude(crm_customer=None).values("crm_customer", "crm_customer__company",
                                                                   "crm_customer__name").annotate(n=Count_("pk"))
                           .order_by("crm_customer__company", "crm_customer__name"))
        top_options = list(live.filter(parent=None).annotate(n=Count_("children")).order_by("name"))
        keep = {k: v for k, v in g.items() if k in ("view", "f", "q", "cust", "crm", "top", "sort") and v}

        def link(**override):
            from urllib.parse import urlencode
            params = {**keep, "view": "dante", **override}
            return "?" + urlencode({k: v for k, v in params.items() if v})
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
               "filters": [(k, label, counts[k], link(f=k)) for k, label in self.FILTERS], "can_edit": can_edit,
               "cust": cust, "crm": crm, "top": top, "sort": sort, "cust_options": cust_options,
               "crm_options": crm_options, "top_options": top_options,
               "reset_url": link(cust="", crm="", top="", q=""), "narrowed": bool(cust or crm or top or q),
               "suggest": suggest, "suggest_url": link(suggest="1"),
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
        from . import matching
        from .models import MatchRule
        rows = []
        usable = [d for d in dante if not d.archived and d.time_trackable]
        ctx_m = matching.Context(usable)
        scored = 0
        for pr in qs.prefetch_related("participants")[:500]:
            link = getattr(pr, "dante_map", None)
            options = []
            if link is None and scored < MATCH_LIMIT:   # бали — для неприв'язаних, не більше MATCH_LIMIT за сторінку
                options = matching.ranked(pr, ctx_m)
                scored += 1
            rows.append({"project": pr, "link": link, "options": options,
                         "suggestion": (options[0][0], options[0][1], "; ".join(options[0][2])) if options else None})
        st = DanteSettings.get()
        ctx = {**self.admin_site.each_context(request), "title": "📁 Проєкти askDANTE ↔ Minerva", "view": "minerva",
               "opts": self.model._meta, "rows": rows, "dante": dante, "usable": usable, "show": show,
               "st": st, "settings_url": reverse("admin:worktime_dantesettings_change", args=[st.pk]),
               "can_edit": services.is_manager(request.user), "min_score": AUTO_ACCEPT,
               "n_strong": sum(1 for r in rows if r["suggestion"] and r["suggestion"][1] >= AUTO_ACCEPT),
               "rules": list(MatchRule.objects.select_related("dante_project", "customer")),
               "rule_kinds": MatchRule.KINDS,
               "unlinked_dante": [d for d in dante if not d.n_maps and not d.archived]}
        return render(request, "admin/worktime/mapping.html", ctx)

    @staticmethod
    def _mapping_post(request):
        from projects.models import Project
        p = request.POST
        from .models import MatchRule
        if p.get("pair"):   # «проєкт Minerva:проєкт askDANTE» — вибрана підказка (з будь-якого боку)
            pr_pk, _, dp_pk = p["pair"].partition(":")
            pr, dp = Project.objects.filter(pk=pr_pk or 0).first(), DanteProject.objects.filter(pk=dp_pk or 0).first()
            if pr and dp:
                mapping.link(pr, dp, request.user)
                messages.success(request, f"{pr.code} → {dp}")
            return
        if p.get("accept_all"):   # бали рахуються заново на сервері, не з браузера
            from . import matching
            threshold = max(AUTO_ACCEPT, int(p.get("min_score") or AUTO_ACCEPT))
            ctx_m = matching.Context(DanteProject.objects.filter(gone=False, archived=False, time_trackable=True))
            linked = 0
            for pr in Project.objects.filter(status__in=Project.OPEN_STATUSES, dante_map__isnull=True)[:MATCH_LIMIT]:
                best = matching.ranked(pr, ctx_m, limit=1)
                if best and best[0][1] >= threshold:
                    mapping.link(pr, best[0][0], request.user)
                    linked += 1
            messages.success(request, f"Зв'язано за підказками з балом ≥ {threshold}: {linked}.")
            return
        if p.get("rule_add"):
            dp = DanteProject.objects.filter(pk=p.get("rule_project") or 0).first()
            kind = p.get("rule_kind") if p.get("rule_kind") in dict(MatchRule.KINDS) else MatchRule.KEYWORD
            value = (p.get("rule_value") or "").strip().lower()
            customer = mapping.find_customer(value) if kind == MatchRule.CUSTOMER else None
            if dp is None or not value or (kind == MatchRule.CUSTOMER and customer is None):
                messages.error(request, "Правило: виберіть проєкт askDANTE і вкажіть значення "
                                        "(для клієнта — назву, email або #номер одного клієнта CRM).")
                return
            MatchRule.objects.create(kind=kind, value="" if customer else value.lstrip("@"), customer=customer,
                                     dante_project=dp, created_by=request.user)
            messages.success(request, "Правило додано — підказки вже його враховують.")
            return
        if p.get("rule_delete"):
            MatchRule.objects.filter(pk=p["rule_delete"]).delete()
            messages.success(request, "Правило видалено.")
            return
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
                dp = mapping.create_in_dante(pr, request.user, p.get(f"create_name_{pr.pk}") or "")
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


# ── Звіти поштою ─────────────────────────────────────────────────
class WorkReportForm(forms.ModelForm):
    class Meta:
        from .models import WorkReport
        model = WorkReport
        fields = "__all__"
        widgets = {"send_to": forms.Textarea(attrs={"rows": 2, "cols": 60})}

    def clean(self):
        from . import reports
        d = super().clean()
        if (d.get("hour") or 0) > 23:
            self.add_error("hour", "Година від 0 до 23.")
        if not 1 <= (d.get("month_day") or 1) <= 28:
            self.add_error("month_day", "День місяця — від 1 до 28.")
        import re
        bad = [a for a in re.split(r"[\s,;]+", d.get("send_to") or "") if a and "@" not in a]
        if bad:
            self.add_error("send_to", "Не схоже на адресу: " + ", ".join(b.strip() for b in bad))
        if not reports.addresses(d.get("send_to") or "") and not d.get("send_to_employees"):
            self.add_error("send_to", "Вкажіть, кому надсилати, або увімкніть «Кожному працівнику».")
        return d


def _register_reports():
    from .models import WorkReport

    @admin.register(WorkReport)
    class WorkReportAdmin(admin.ModelAdmin):
        form = WorkReportForm
        list_display = ["name", "enabled", "frequency", "period", "send_to", "send_to_employees", "last_sent_at",
                        "last_result"]
        filter_horizontal = ["people"]
        readonly_fields = ["last_sent_at", "last_result", "preview"]
        fieldsets = [
            (None, {"fields": ["name", "enabled", "preview"]}),
            ("Що й за який період", {"fields": ["period", "days", "people", "show_absence_types"]}),
            ("Коли", {"fields": ["frequency", "weekday", "month_day", "hour"]}),
            ("Кому й звідки", {"fields": ["send_to", "send_to_employees", "sender"],
                               "description": "Працівнику — лише його частина, на «Email для звіту» "
                                              "(👥 Працівники askDANTE)."}),
            ("Останнє надсилання", {"fields": ["last_sent_at", "last_result"]}),
        ]

        def has_module_permission(self, request):
            return services.is_manager(request.user)

        def _manager(self, request, obj=None):
            return services.is_manager(request.user)

        has_view_permission = has_add_permission = has_change_permission = has_delete_permission = _manager

        @admin.display(description="Перегляд")
        def preview(self, obj):
            url = reverse("admin:worktime_workday_report")
            if obj and obj.pk:
                return format_html('<a class="button" href="{}">📊 Відкрити звіт</a> '
                                   '<button type="submit" class="button" name="_send_now" value="1">✉️ Зберегти й '
                                   'надіслати зараз</button>', url)
            return format_html('<a class="button" href="{}">📊 Відкрити звіт</a>', url)

        def response_change(self, request, obj):
            if request.POST.get("_send_now"):
                from . import reports
                self.message_user(request, "✉️ " + reports.send(obj))
                return redirect(reverse("admin:worktime_workreport_change", args=[obj.pk]))
            return super().response_change(request, obj)


_register_reports()
