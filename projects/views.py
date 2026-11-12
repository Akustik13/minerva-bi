"""JSON для пошти: проєкти гілки, «📁 До проєкту», створення з листування, ✓/✗ пропозицій;
пульт проєкту (projects/cockpit.py): шкала, 📌 важливі моменти, пропозиції прив'язок, «🔎 Знайти пов'язане»."""
import json
from datetime import date

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .models import Project, ProjectHighlight, ProjectLink


def _thread_for(request, thread_pk):
    """Гілка лише зі скриньок користувача (суперкористувач — будь-яка)."""
    from email_assistant.models import EmailThread
    qs = EmailThread.objects.all() if request.user.is_superuser else EmailThread.objects.filter(
        account__user=request.user)
    return get_object_or_404(qs, pk=thread_pk)


def _project_dict(p):
    return {"id": p.pk, "code": p.code, "name": p.name, "status": p.get_status_display(),
            "customer": p.customer.name if p.customer_id else "",
            "url": f"/admin/projects/project/{p.pk}/change/", "mail_url": f"/email/?project={p.pk}"}


def _brief(project) -> dict:
    """Фаза, колір стану й відкриті кроки для прев'ю листа; збій — без них (прев'ю однаково показує проєкт)."""
    from . import plan
    try:
        return plan.brief(project)
    except Exception:  # noqa: BLE001
        return {}


def thread_state(thread, viewer=None) -> dict:
    """Проєкти гілки для блоку «📁» у прев'ю листа; фаза й кроки (brief) — лише з правом бачити проєкти
    (viewer None — без перевірки: тести, внутрішні виклики)."""
    info = services.thread_projects(thread)
    show = viewer is None or viewer.has_perm("projects.view_project")
    return {
        "thread": thread.pk,
        "confirmed": [{"link": l.pk, "project": _project_dict(l.project), "reason": l.reason,
                       "brief": _brief(l.project) if show else {}} for l in info["confirmed"]],
        "suggested": [{"link": l.pk, "project": _project_dict(l.project), "reason": l.reason}
                      for l in info["suggested"]],
    }


def _body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


@staff_member_required
def thread_view(request, thread_pk):
    thread = _thread_for(request, thread_pk)
    state = thread_state(thread, request.user)
    linked = {c["project"]["id"] for c in state["confirmed"]}
    state["candidates"] = [_project_dict(p) for p in services.candidate_projects(thread) if p.pk not in linked]
    state["name_suggestion"] = services.suggest_name([thread])
    return JsonResponse(state)


@staff_member_required
@require_POST
def link_view(request, thread_pk):
    thread = _thread_for(request, thread_pk)
    project = get_object_or_404(Project, pk=_body(request).get("project_id") or 0)
    services.link_thread(project, thread, user=request.user, reason="додано з пошти")
    return JsonResponse({"ok": True, **thread_state(thread, request.user)})


@staff_member_required
@require_POST
def create_view(request, thread_pk):
    thread = _thread_for(request, thread_pk)
    project = services.create_project(name=_body(request).get("name", ""), user=request.user, threads=[thread])
    return JsonResponse({"ok": True, "project": _project_dict(project), **thread_state(thread, request.user)})


def _link_for(request, link_pk):
    link = get_object_or_404(ProjectLink.objects.select_related("project", "thread"), pk=link_pk)
    if link.thread_id:
        _thread_for(request, link.thread_id)   # перевірка доступу до скриньки
    elif not request.user.is_superuser:
        from django.http import Http404
        raise Http404
    return link


@staff_member_required
@require_POST
def decide_view(request, link_pk):
    link = _link_for(request, link_pk)
    services.decide(link, bool(_body(request).get("accept")), user=request.user)
    return JsonResponse({"ok": True, **(thread_state(link.thread, request.user) if link.thread_id else {})})


@staff_member_required
@require_POST
def remove_view(request, link_pk):
    link = _link_for(request, link_pk)
    thread = link.thread
    services.unlink(link)
    return JsonResponse({"ok": True, **(thread_state(thread, request.user) if thread else {})})


# ── Огляд проєкту ────────────────────────────────────────────────────────────

def _project_for(request, pk, perm):
    """Огляд — як сторінка проєкту: переглядати з правом view, складати (пише в БД, витрачає AI-запит) — change."""
    from django.http import Http404
    if not request.user.has_perm(f"projects.{perm}_project"):
        raise Http404
    return get_object_or_404(Project, pk=pk)


def _project_for_change(request, pk):
    return _project_for(request, pk, "change")


@staff_member_required
@require_POST
def rename_view(request, pk):
    """✏️ Перейменувати проєкт / 🎨 значок (правий клік у пошті): {name} і / або {icon} — право change;
    код P-… не змінюється."""
    project = _project_for_change(request, pk)
    body, fields = _body(request), ["updated_at"]
    if "name" in body:
        name = str(body.get("name") or "").strip()[:200]
        if not name:
            return JsonResponse({"ok": False, "error": "Вкажіть назву"}, status=400)
        project.name = name
        fields.append("name")
    if "icon" in body:
        project.icon = str(body.get("icon") or "").strip()[:8]
        fields.append("icon")
    project.save(update_fields=fields)
    return JsonResponse({"ok": True, "project": _project_dict(project)})


@staff_member_required
@require_http_methods(["GET", "POST"])
def overview_view(request, pk):
    """GET — стан огляду (панель у пошті); POST — скласти AI-огляд заново."""
    from . import overview
    if request.method == "POST":
        project = _project_for_change(request, pk)
        result, error = overview.generate(project, user=request.user)
        if result is None:      # як AI-функції пошти: збій AI — не помилка сервера
            return JsonResponse({"ok": False, "error": error})
    else:
        project = _project_for(request, pk, "view")
    return JsonResponse(overview.state(project, request.user))


@staff_member_required
@require_POST
def overview_next_action_view(request, pk):
    from . import overview
    project = _project_for_change(request, pk)
    text = overview.apply_next_action(project)
    return JsonResponse({"ok": bool(text), "next_action": text})


# ── Пульт проєкту ────────────────────────────────────────────────────────────

def _cockpit(request, project, notice="", extra=None):
    """Свіжий HTML пульта (JS замінює #pj-cockpit, форма картки не перезавантажується)."""
    from . import cockpit
    if request.method == "POST":
        range_key = str(_body(request).get("range") or request.GET.get("range") or "all")
    else:
        range_key = request.GET.get("range") or "all"
    return JsonResponse({"ok": True, "html": cockpit.render(project, request.user, range_key, notice), **(extra or {})})


@staff_member_required
@require_GET
def cockpit_view(request, pk):
    return _cockpit(request, _project_for(request, pk, "view"))


def _own_message(request, pk):
    """Лист лише з власних скриньок людини (і для суперкористувача — як «📬 Листи зі скриньки»)."""
    from email_assistant.models import EmailMessage
    return EmailMessage.objects.filter(pk=pk, account__user=request.user, is_deleted=False).first()


@staff_member_required
@require_POST
def highlight_view(request, pk):
    """📌 Важливий момент: {title, date YYYY-MM-DD, kind, quote, message, cockpit, range} — право змінювати проєкт.
    Лист — лише з власної скриньки. cockpit: true — у відповіді свіжий пульт (html)."""
    project = _project_for_change(request, pk)
    body = _body(request)
    message = None
    if body.get("message"):
        message = _own_message(request, body["message"]) if str(body["message"]).isdigit() else None
        if message is None:
            return JsonResponse({"ok": False, "error": "Лист не знайдено у ваших скриньках"}, status=400)
    try:
        when = date.fromisoformat(str(body.get("date"))[:10]) if body.get("date") else None
    except ValueError:
        when = None
    try:
        h = services.add_highlight(project, title=body.get("title"), date=when, kind=str(body.get("kind") or ""),
                                   quote=body.get("quote") or "", message=message, user=request.user)
    except services.HighlightError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    info = {"highlight": {"id": h.pk, "date": h.date.strftime("%d.%m.%Y"), "title": h.title},
            "project": {"id": project.pk, "code": project.code, "name": project.name}}
    if body.get("cockpit"):
        return _cockpit(request, project, f"📌 Додано: {h.title}", extra=info)
    return JsonResponse({"ok": True, **info})


HANDLED_MAX = 200
PEEK_CHARS = 900


@staff_member_required
@require_GET
def mail_peek_view(request):
    """👁 Лист чи гілка при наведенні в пульті проєкту: ?m=<лист> або ?t=<гілка> (останній лист і скільки листів) —
    лише з власних скриньок людини (чужий — 404, як і «📬 Листи зі скриньки»). Новий текст без цитат і підпису."""
    import re
    from django.utils import timezone as tz

    from email_assistant import quotes
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.models import EmailMessage
    qs = EmailMessage.objects.filter(account__user=request.user, is_deleted=False)
    m, count = None, 0
    if str(request.GET.get("m") or "").isdigit():
        m = qs.filter(pk=int(request.GET["m"])).first()
    elif str(request.GET.get("t") or "").isdigit():
        thread = qs.filter(thread_id=int(request.GET["t"]))
        count = thread.count()
        m = thread.order_by("-sent_at", "-pk").first()
    if m is None:
        return JsonResponse({"ok": False, "error": "Лист не знайдено у ваших скриньках"}, status=404)
    text = quotes.strip_signature(quotes.new_text(m.body_text or "")).strip() or (m.body_text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    more = len(text) > PEEK_CHARS
    files = [a.get("name") for a in (m.attachments or []) if isinstance(a, dict) and not a.get("is_inline")
             and a.get("name")][:8]
    when = tz.localtime(m.sent_at) if m.sent_at and tz.is_aware(m.sent_at) else m.sent_at
    return JsonResponse({
        "ok": True, "subject": m.subject or "(без теми)", "from": m.from_name or m.from_email,
        "from_email": m.from_email, "to": ", ".join((m.to_emails or [])[:3]), "date": when.strftime("%d.%m.%Y %H:%M")
        if when else "", "out": is_own(m.from_email, own_addresses()), "text": text[:PEEK_CHARS].rstrip(),
        "more": more, "files": files, "count": count, "url": f"/email/message/{m.pk}/"})


def _talk_title(text) -> str:
    """Назва моменту з «про що йшла мова»: перший рядок, до 120 символів."""
    first = " ".join(str(text or "").strip().splitlines()[0].split()) if str(text or "").strip() else ""
    return first if len(first) <= 120 else first[:119].rstrip() + "…"


def _date_or_today(value):
    from django.utils import timezone
    try:
        d = date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        d = None
    today = timezone.localdate()
    return d if d and d <= today else today


@staff_member_required
@require_POST
def event_note_view(request, pk):
    """🤝 Зустріч / ⏰ термін з «Що зараз», «Ризиків» чи ромба шкали: {events: [pk…], summary, date, done, undo,
    highlight} — право змінювати проєкт; лише події власного календаря, що є в проєкті (з його листів або прив'язані).
    done — ✓ відбулась / виконано (усі однакові події); summary — 📌 момент «про що йшла мова» (🤝 для зустрічі,
    подія й лист — у моменті: сюди модуль запису нарад додасть транскрипцію). undo — назад: не виконано, момент
    прибрано."""
    from . import overview, terms
    project = _project_for_change(request, pk)
    body = _body(request)
    raw = body.get("events") if isinstance(body.get("events"), list) else []
    ids = {int(i) for i in raw if str(i).isdigit()}
    events = [e for e in overview._deadlines([x["msg"].pk for x in overview._messages(project)], project)
              if e.pk in ids and e.user_id == request.user.pk]
    if not events:
        return JsonResponse({"ok": False, "error": "Подію не знайдено у вашому календарі серед подій проєкту"},
                            status=400)
    first = events[0]
    if body.get("undo"):
        for e in events:
            if e.is_done:
                e.is_done = False
                e.save(update_fields=["is_done"])
        hid = body.get("highlight")
        if str(hid or "").isdigit():
            ProjectHighlight.objects.filter(pk=int(hid), project=project, created_by=request.user).delete()
        return _cockpit(request, project, "↶ Скасовано", extra={"notice": "↶ Скасовано"})
    if body.get("done"):
        for e in events:
            if not e.is_done:
                e.is_done = True
                e.save(update_fields=["is_done"])
    summary = str(body.get("summary") or "").strip()[:2000]
    moment = None
    meeting = terms.is_meeting(first)
    if summary:
        msg = first.email_message if (first.email_message_id and first.email_message.account_id
                                      and first.email_message.account.user_id == request.user.pk) else None
        when = body.get("date") or (timezone_date(first.start_at))
        moment = services.add_highlight(project, title=_talk_title(summary), date=_date_or_today(when),
                                        kind=ProjectHighlight.MEETING if meeting else ProjectHighlight.OTHER,
                                        quote=summary, message=msg, user=request.user, event=first)
    parts = []
    if body.get("done"):
        parts.append("✓ Відбулась" if meeting else "✓ Виконано")
    if moment:
        parts.append("📌 записано в моменти")
    notice = " · ".join(parts) or "✓"
    return _cockpit(request, project, notice, extra={"notice": notice, "events": [e.pk for e in events],
                                                     "highlight": moment.pk if moment else None})


def timezone_date(value):
    from django.utils import timezone
    if value is None:
        return None
    return (timezone.localtime(value) if timezone.is_aware(value) else value).date().isoformat()


@staff_member_required
@require_POST
def mail_handled_view(request, pk):
    """✓ «Відповіли поза поштою» з «Що зараз» пульта: {messages: [pk…], how, summary, date, undo, range} — право
    змінювати проєкт; лише листи цього проєкту з власних скриньок людини. how — MailHandled.HOW_CHOICES; summary —
    «про що йшла мова»: примітка позначки й 📌 момент (📞 / 🤝, з найновішим листом, дата — date або сьогодні);
    undo: true — зняти позначку і її момент (лист знову в «Що зараз»). У відповіді — свіжий пульт і marked."""
    from django.db.models import Q

    from . import state
    from .models import MailHandled
    project = _project_for_change(request, pk)
    body = _body(request)
    raw = body.get("messages") if isinstance(body.get("messages"), list) else []
    ids = [int(i) for i in raw if str(i).isdigit()][:HANDLED_MAX]
    undo = bool(body.get("undo"))
    how = str(body.get("how") or "")
    hows = dict(MailHandled.HOW_CHOICES)
    if not undo and how not in hows:
        return JsonResponse({"ok": False, "error": "Виберіть, як відповіли"}, status=400)
    own = sorted(services.project_messages(project).filter(pk__in=ids, account__user=request.user, is_deleted=False)
                 .values_list("pk", flat=True).distinct())
    if not own:
        return JsonResponse({"ok": False, "error": "Листи не знайдено серед ваших листів цього проєкту"}, status=400)
    if undo:
        marks = MailHandled.objects.filter(message_id__in=own)
        ProjectHighlight.objects.filter(pk__in=marks.exclude(highlight=None).values("highlight_id"), project=project,
                                        created_by=request.user).delete()
        marks.delete()
        notice = ("↶ Позначку знято — лист знову в «Що зараз»" if len(own) == 1 else
                  f"↶ Позначки знято: {len(own)} — листи знову в «Що зараз»")
    else:
        summary = str(body.get("summary") or body.get("note") or "").strip()[:2000]
        note = " ".join(summary.split())[:255]
        moment = None
        if summary:
            from email_assistant.models import EmailMessage as _EM
            last = _EM.objects.filter(pk__in=own).order_by("-sent_at", "-pk").first()
            kind = {MailHandled.PHONE: ProjectHighlight.CALL,
                    MailHandled.MEETING: ProjectHighlight.MEETING}.get(how, ProjectHighlight.OTHER)
            moment = services.add_highlight(project, title=_talk_title(summary), date=_date_or_today(body.get("date")),
                                            kind=kind, quote=summary, message=last, user=request.user)
        for mid in own:
            MailHandled.objects.update_or_create(message_id=mid, defaults={"how": how, "note": note,
                                                                           "user": request.user, "highlight": moment})
        notice = f"✓ {hows[how]}" + (f": листів {len(own)}" if len(own) > 1 else "") + (
            " · 📌 записано в моменти" if moment else "")
    from email_assistant.models import EmailMessage
    threads = EmailMessage.objects.filter(pk__in=own).exclude(thread=None).values_list("thread_id", flat=True)
    state.mark_dirty(set(ProjectLink.objects.filter(Q(thread_id__in=threads) | Q(message_id__in=own))
                         .values_list("project_id", flat=True)) | {project.pk})
    return _cockpit(request, project, notice, extra={"marked": own, "undo": undo, "notice": notice})


@staff_member_required
@require_POST
def export_view(request, pk):
    """📤 Діаграма проєкту: multipart {image (PNG з браузера), what plan|timeline|chain, lang de|en|uk,
    formats pdf,png,xlsx, table, send} — право перегляду. send — чернетка листа у власній скриньці з файлами
    (→ {compose}); інакше — один файл (pdf або xlsx) на завантаження."""
    from django.http import HttpResponse

    from . import export
    project = _project_for(request, pk, "view")
    what = request.POST.get("what") if request.POST.get("what") in export.WHATS else "plan"
    lang = request.POST.get("lang") if request.POST.get("lang") in export.LANGS else "de"
    formats = [f for f in str(request.POST.get("formats") or "").split(",") if f in ("pdf", "png", "xlsx")]
    if what != "plan":
        formats = [f for f in formats if f != "xlsx"]
    if not formats:
        return JsonResponse({"ok": False, "error": "Виберіть хоча б один файл"}, status=400)
    png = b""
    try:
        if "pdf" in formats or "png" in formats:
            upload = request.FILES.get("image")
            if upload is None or upload.size > export.IMAGE_MAX:
                raise export.ExportError("Немає картинки діаграми" if upload is None else
                                         "Картинка завелика — зменште масштаб діаграми")
            png = export.check_png(upload.read())
        files = []
        for f in formats:
            if f == "pdf":
                files.append((export.file_name(project, what, "pdf"),
                              export.pdf(project, what, lang, png, bool(request.POST.get("table")),
                                         titled=bool(request.POST.get("titled"))), "application/pdf"))
            elif f == "png":
                files.append((export.file_name(project, what, "png"), png, "image/png"))
            else:
                files.append((export.file_name(project, what, "xlsx"), export.xlsx(project, lang),
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        if request.POST.get("send"):
            return JsonResponse({"ok": True, "compose": export.mail(request.user, project, what, lang, files)})
    except export.ExportError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    if len(files) == 1:
        name, data, ctype = files[0]
    else:                        # кілька файлів — одним ZIP: браузер блокує кілька завантажень поспіль
        name, data, ctype = export.zip_name(project, what), export.zip_files(files), "application/zip"
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@staff_member_required
@require_POST
def sourcing_view(request, pk):
    """📨 Рішення щодо постачальника із запиту проєкту: {recipient, action award | reject | reset, text} — право
    змінювати проєкт і запити постачальникам (suppliers.change_supplierrequest)."""
    from . import sourcing
    project = _project_for_change(request, pk)
    if not request.user.has_perm("suppliers.change_supplierrequest"):
        return JsonResponse({"ok": False, "error": "Немає права змінювати запити постачальникам"}, status=403)
    body = _body(request)
    try:
        notice = sourcing.decide(project, body.get("recipient"), str(body.get("action") or ""),
                                 str(body.get("text") or ""), request.user)
    except sourcing.SourcingError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return _cockpit(request, project, notice)


@staff_member_required
@require_POST
def party_view(request, pk):
    """🔗 Сторона проєкту: {id, role, name, what, delivers_to, email, note} — створити / змінити (право змінювати)."""
    from . import parties
    project = _project_for_change(request, pk)
    try:
        p = parties.save(project, _body(request))
    except parties.PartyError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return _cockpit(request, project, f"🔗 Збережено: {p.name}")


@staff_member_required
@require_POST
def party_delete_view(request, pk, party_pk):
    from . import parties
    project = _project_for_change(request, pk)
    try:
        name = parties.delete(project, party_pk)
    except parties.PartyError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return _cockpit(request, project, f"🔗 Прибрано: {name}")


@staff_member_required
@require_POST
def party_suggest_view(request, pk):
    """«🔎 Підставити з даних проєкту»: замовник, ми, постачальники закупівель, JLCPCB, учасники-постачальники."""
    from . import parties
    project = _project_for_change(request, pk)
    n = parties.suggest(project)
    return _cockpit(request, project, f"🔗 Додано сторін: {n}" if n else
                    "🔗 Нового не знайдено: клієнт, закупівлі, JLC і учасники-постачальники вже серед сторін")


@staff_member_required
@require_POST
def highlight_delete_view(request, hid):
    h = get_object_or_404(ProjectHighlight.objects.select_related("project"), pk=hid)
    project = _project_for_change(request, h.project_id)
    h.delete()
    return _cockpit(request, project, "📌 Момент прибрано")


@staff_member_required
@require_POST
def link_decide_view(request, pk, link_pk):
    """✓/✗ пропозиції прив'язки з пульта. Гілку чи лист вирішує лише власник скриньки (як у пошті)."""
    project = _project_for_change(request, pk)
    link = get_object_or_404(ProjectLink.objects.select_related("thread", "message"), pk=link_pk, project=project)
    mail = link.thread or (link.message.thread if link.message_id else None)
    if mail is not None and mail.account.user_id != request.user.pk:
        return JsonResponse({"ok": False, "error": "Цю гілку вирішує власник скриньки — у пошті"}, status=403)
    accept = bool(_body(request).get("accept"))
    services.decide(link, accept, user=request.user)
    return _cockpit(request, project, "🔗 Прив'язано" if accept else "🔗 Більше не пропонуватиметься")


@staff_member_required
@require_POST
def scan_view(request, pk):
    """«🔎 Знайти пов'язане»: закупівлі й JLC-замовлення з товарами проєкту → пропозиції ✓/✗."""
    project = _project_for_change(request, pk)
    if not project.products.exists():
        notice = "Додайте «Товари проєкту» в полях проєкту — за ними Minerva шукає закупівлі й JLC-замовлення"
    else:
        n = services.scan(project)
        notice = (f"🔎 Нових пропозицій: {n}" if n else
                  "🔎 Нового не знайдено: закупівлі й JLC-замовлення з товарами проєкту вже запропоновані або їх немає")
    return _cockpit(request, project, notice)


# ── 📅 План (projects/plan.py, schedule.py) ──────────────────────────────────

def _plan_action(request, pk, fn):
    """Дія плану, ризику чи статусу: право змінювати проєкт; помилка даних (PlanError, RiskError, ValueError) →
    400 з поясненням; успіх → свіжий пульт."""
    project = _project_for_change(request, pk)
    try:
        notice = fn(project, _body(request))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return _cockpit(request, project, notice)


# ── ⚠️ Ризики й 📝 тижневий статус (projects/risks.py, risk_rules.py, health.py) ─────────────

@staff_member_required
@require_POST
def risk_view(request, pk):
    """Створити / змінити ризик: {id, title, description, category, probability, impact, owner, mitigation, status,
    review_on, steps, message, quote} — message лише з власної скриньки (ризик з листа)."""
    from . import risks

    def run(project, body):
        message = None
        if str(body.get("message") or "").isdigit() and not body.get("id"):
            message = _own_message(request, int(body["message"]))
            if message is None:
                raise risks.RiskError("Лист не знайдено у ваших скриньках")
        r = risks.save(project, body, user=request.user, message=message)
        return f"⚠️ Ризик збережено: {r.title}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def risk_delete_view(request, pk, rid):
    from . import risks
    return _plan_action(request, pk, lambda project, body: "⚠️ Ризик видалено" if risks.delete(project, rid)
                        else "Ризик уже видалено")


@staff_member_required
@require_POST
def status_view(request, pk):
    """📝 Тижневий статус: {health on_track | at_risk | off_track, text, ai} — зі знімком плану й ризиків."""
    from . import health

    def run(project, body):
        u = health.save(project, request.user, body.get("health"), body.get("text"), ai=bool(body.get("ai")))
        return f"📝 Статус збережено: {u.get_health_display()}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def status_draft_view(request, pk):
    """🧠 AI-чернетка тижневого статусу (Claude Haiku 5.5, бюджет AI) → {ok, text, error} — нічого не зберігає."""
    from . import health
    project = _project_for_change(request, pk)
    text, error = health.ai_draft(project, request.user)
    return JsonResponse({"ok": bool(text), "text": text, "error": error})


@staff_member_required
@require_POST
def plan_template_view(request, pk):
    """«🧩 Скласти план з шаблону»: {template, start YYYY-MM-DD}."""
    from . import plan
    from .models import ProjectTemplate

    def run(project, body):
        tpl = ProjectTemplate.objects.filter(pk=body.get("template") or 0, is_active=True).first() \
            if str(body.get("template") or "").isdigit() else None
        if tpl is None:
            raise plan.PlanError("Виберіть шаблон")
        n = plan.apply_template(project, tpl, plan._date(body.get("start")), user=request.user)
        return f"🧩 План з шаблону «{tpl.name}»: кроків {n}. Перевірте дати й зафіксуйте базовий план"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_step_view(request, pk):
    from . import plan

    def run(project, body):
        step = plan.save_step(project, body, user=request.user)
        return f"📋 Збережено: {step.title}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_step_delete_view(request, pk, sid):
    from . import plan
    return _plan_action(request, pk, lambda project, body: "📋 Крок прибрано" if plan.delete_step(project, sid)
                        else "Крок уже прибрано")


@staff_member_required
@require_POST
def plan_phase_view(request, pk):
    from . import plan

    def run(project, body):
        phase = plan.save_phase(project, body)
        return f"📋 Фаза збережена: {phase.name}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_phase_delete_view(request, pk, fid):
    from . import plan
    return _plan_action(request, pk, lambda project, body: "📋 Фазу прибрано, її кроки лишились без фази"
                        if plan.delete_phase(project, fid) else "Фазу вже прибрано")


@staff_member_required
@require_POST
def plan_baseline_view(request, pk):
    from . import plan
    return _plan_action(request, pk, lambda project, body:
                        f"📌 Базовий план зафіксовано: кроків {plan.baseline(project, request.user)}")


@staff_member_required
@require_POST
def plan_save_template_view(request, pk):
    from . import plan

    def run(project, body):
        tpl = plan.save_as_template(project, body.get("name"), user=request.user)
        return f"💾 Шаблон «{tpl.name}» збережено — він з'явиться у виборі шаблонів"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_clear_view(request, pk):
    from . import plan

    def run(project, body):
        plan.clear(project)
        return "🗑 План очищено"
    return _plan_action(request, pk, run)


# ── 🧠 AI-помічник проєкту (projects/ai_scan.py, ai_plan.py, forgotten.py, delegate.py) ───────────────────────────
# Усе — з правом змінювати проєкт; пошта — лише власні скриньки людини (перевіряють самі модулі).

PLAN_POLL_MS = 3000         # браузер питає про фонову дію AI (план, моменти й ризики) кожні 3 с


@staff_member_required
@require_http_methods(["GET", "POST"])
def ai_scan_view(request, pk):
    """🧠 Знайти моменти й ризики в листах (у фоні): POST — наступна порція власних непрочитаних листів проєкту,
    GET — хід; прочитано → свіжий пульт."""
    from . import ai_scan
    project = _project_for_change(request, pk)
    if request.method == "POST":
        st = ai_scan.start(project, request.user)
    else:
        st = ai_scan.run_state(project, request.user)
    if st.get("running"):
        return JsonResponse({"ok": True, "pending": True, "poll_ms": PLAN_POLL_MS})
    if st.get("error"):
        return JsonResponse({"ok": False, "error": st["error"]}, status=400)
    res = st.get("result") if st.get("done") else None
    if not res:
        return JsonResponse({"ok": False, "error": "Пошук ще не запускали"}, status=400)
    if res["error"]:
        return JsonResponse({"ok": False, "error": res["error"]}, status=400)
    if not res["read"]:
        notice = "🧠 Нових листів немає — усі ваші листи проєкту вже прочитано"
    else:
        notice = f"🧠 Прочитано листів: {res['read']} · нових пропозицій: {res['found']}"
        if res["left"]:
            notice += f" · ще не прочитано: {res['left']}"
    return _cockpit(request, project, notice)


@staff_member_required
@require_http_methods(["GET", "POST"])
def ai_risks_view(request, pk):
    """🧠 AI-аналіз ризиків (у фоні): POST {wish} — запустити, GET — хід; готово → свіжий пульт з пропозиціями ✓/✗."""
    from . import ai_risks
    project = _project_for_change(request, pk)
    if request.method == "POST":
        st = ai_risks.start(project, request.user, str(_body(request).get("wish") or ""))
    else:
        st = ai_risks.run_state(project, request.user)
    if st.get("running"):
        return JsonResponse({"ok": True, "pending": True, "poll_ms": PLAN_POLL_MS})
    if st.get("error"):
        return JsonResponse({"ok": False, "error": st["error"]}, status=400)
    res = st.get("result") if st.get("done") else None
    if not res:
        return JsonResponse({"ok": False, "error": "Аналіз ще не запускали"}, status=400)
    if res["error"]:
        return JsonResponse({"ok": False, "error": res["error"]}, status=400)
    if res["found"]:
        notice = f"🧠 Нових ризиків на перевірку: {res['found']} — ✓ додати, ✗ ні"
    elif res["proposed"]:
        notice = "🧠 AI не знайшов нових ризиків — запропоновані вже є в реєстрі або відхилені раніше"
    else:
        notice = "🧠 AI не бачить нових ризиків"
    return _cockpit(request, project, notice, extra={"summary": res.get("summary", "")})


@staff_member_required
@require_POST
def ai_suggestion_view(request, pk, sid):
    """✓ / ✗ пропозиції AI: {accept: true|false} — лише та людина, з чиїх листів пропозиція."""
    from . import ai_scan

    def run(project, body):
        if body.get("accept"):
            return ai_scan.accept(project, request.user, sid)
        return ai_scan.reject(project, request.user, sid)
    return _plan_action(request, pk, run)


@staff_member_required
@require_http_methods(["GET", "POST"])
def ai_plan_view(request, pk):
    """🧠 Чернетка плану з листування (у фоні, нічого не пише в план): POST — почати, GET — хід; готова — JSON
    для вікна перегляду."""
    from . import ai_plan
    project = _project_for_change(request, pk)
    if request.method == "POST":
        from .plan import _date
        body = _body(request)
        try:
            st = ai_plan.start(project, request.user, wish=str(body.get("wish") or ""),
                               since=_date(body.get("since")) if "since" in body else ai_plan.KEEP,
                               replace=bool(body.get("replace")), start_date=_date(body.get("start")))
        except ValueError as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
    else:
        st = ai_plan.run_state(project, request.user)
    if st.get("running"):
        return JsonResponse({"ok": True, "pending": True, "poll_ms": PLAN_POLL_MS})
    if st.get("error"):
        return JsonResponse({"ok": False, "error": st["error"]}, status=400)
    draft = ai_plan.saved(project, request.user) if st.get("done") else None
    if draft is None:
        return JsonResponse({"ok": False, "error": "Чернетки немає — натисніть «🧠 Скласти план з листування»"},
                            status=400)
    return JsonResponse({"ok": True, "draft": draft})


@staff_member_required
@require_POST
def ai_settings_view(request, pk):
    """📅 «Листи для AI — з» і старт проєкту (блок «🧠 AI-помічник»): {since, start}; порожньо — усі листи."""
    from .plan import _date

    def run(project, body):
        since, start = _date(body.get("since")), _date(body.get("start"))
        project.ai_mail_since = since
        fields = ["ai_mail_since", "updated_at"]
        if start:
            project.start_date = start
            fields.append("start_date")
        project.save(update_fields=fields)
        return (f"🧠 AI читає листи проєкту з {since:%d.%m.%Y}" if since else "🧠 AI читає всі листи проєкту") + \
            (f" · старт проєкту {start:%d.%m.%Y}" if start else "")
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def ai_plan_apply_view(request, pk):
    """✓ Застосувати чернетку: {skip: [номери кроків], start} → план → свіжий пульт."""
    from . import ai_plan

    def run(project, body):
        skip = body.get("skip") if isinstance(body.get("skip"), list) else []
        return f"🧠 План з листування складено: кроків {ai_plan.apply(project, request.user, skip, body.get('start'))}"
    return _plan_action(request, pk, run)


def _rag_state(project, user, job) -> dict:
    from . import forgotten
    from rag_assistant.models import RetrievalJob
    if job is None:
        return {"rag": False, "job": "", "done": True, "rag_rows": [], "message": ""}
    done = job.is_terminal
    rows = forgotten.rag_rows(project, user, job) if job.status == RetrievalJob.COMPLETED else []
    return {"rag": True, "job": "" if done else str(job.pk), "done": done, "rag_rows": rows,
            "message": "" if job.status == RetrievalJob.COMPLETED else (job.message or job.get_status_display()),
            "poll_ms": max(800, min(5000, int(job.poll_after_ms or 1500)))}


@staff_member_required
@require_POST
def forgotten_view(request, pk):
    """🔎 Забуті листи: одразу — знайдене без AI; база знань — завдання RAG, браузер опитує forgotten_poll."""
    from . import forgotten
    project = _project_for_change(request, pk)
    rows = forgotten.local(project, request.user)
    return JsonResponse({"ok": True, "rows": rows, **_rag_state(project, request.user,
                                                                 forgotten.rag_start(project, request.user))})


@staff_member_required
def forgotten_poll_view(request, pk, job_id):
    from rag_assistant import retrieval
    from rag_assistant.models import RetrievalJob
    project = _project_for_change(request, pk)
    job = get_object_or_404(RetrievalJob, pk=job_id, user=request.user, purpose="project_forgotten")
    retrieval.poll(job)
    return JsonResponse({"ok": True, **_rag_state(project, request.user, job)})


@staff_member_required
@require_POST
def forgotten_decide_view(request, pk):
    """✓ / ✗ забутої гілки: {thread, accept} — лише гілки власних скриньок."""
    from . import forgotten
    project = _project_for_change(request, pk)
    body = _body(request)
    try:
        notice = forgotten.decide(project, request.user, int(body.get("thread") or 0), bool(body.get("accept")))
    except (TypeError, ValueError) as e:
        return JsonResponse({"ok": False, "error": str(e) or "Гілку не знайдено"}, status=400)
    return JsonResponse({"ok": True, "notice": notice})


@staff_member_required
def step_agent_view(request, pk, sid):
    """🤖 Доручити крок агенту: GET — варіанти доручення й завдання кроку; POST {instruction, mail_to, model, budget}
    — завдання агенту від імені людини, прив'язане до кроку, запуск у фоні → свіжий пульт і адреса перевірки."""
    from . import cockpit, delegate
    from .models import ProjectStep
    project = _project_for_change(request, pk)
    step = get_object_or_404(ProjectStep.objects.select_related("project__customer", "phase"), pk=sid, project=project)
    if request.method == "GET":
        return JsonResponse(delegate.editor(step, request.user))
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST"}, status=405)
    try:
        task = delegate.create(step, request.user, _body(request))
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "url": f"/admin/agent/agenttask/{task.pk}/review/",
                         "html": cockpit.render(project, request.user, str(_body(request).get("range") or "all"),
                                                f"🤖 Завдання агенту #{task.pk}: «{step.title}» — агент працює")})


@staff_member_required
@require_POST
def plan_step_dates_view(request, pk, sid):
    """📋 Класична діаграма Ганта: смугу перетягнули — {start, end} → нові планові дати кроку → свіжий пульт."""
    from . import plan

    def run(project, body):
        step = plan.move_step(project, sid, body.get("start"), body.get("end"))
        when = f"{step.end:%d.%m.%Y}" if step.kind == step.MILESTONE else f"{step.start:%d.%m} – {step.end:%d.%m.%Y}"
        return f"📋 «{step.title}»: {when}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_phase_shift_view(request, pk, fid):
    """📋 Фазу перетягнули на діаграмі Ганта: {days} → невиконані кроки фази на стільки днів → свіжий пульт."""
    from . import plan

    def run(project, body):
        phase, moved = plan.shift_phase(project, fid, body.get("days"))
        days = int(body.get("days"))
        return f"📋 Фаза «{phase.name}»: {'+' if days > 0 else ''}{days} дн., кроків зсунуто: {moved}"
    return _plan_action(request, pk, run)


def _mail_of(request, body):
    """Лист з тіла запиту ({message}) — лише з власних скриньок; немає / чужий → None."""
    pk = body.get("message")
    return _own_message(request, int(pk)) if str(pk or "").isdigit() else None


@staff_member_required
@require_POST
def plan_quick_step_view(request, pk):
    """➕ Крок з листа (прев'ю листа): {title, message} → крок у поточній фазі."""
    from . import plan

    def run(project, body):
        step = plan.quick_step(project, body.get("title"), _mail_of(request, body), request.user)
        return f"➕ Крок додано: {step.title}"
    return _plan_action(request, pk, run)


@staff_member_required
@require_POST
def plan_step_close_view(request, pk, sid):
    """✅ Закрити крок з листа: {message} → «виконано» датою листа."""
    from . import plan

    def run(project, body):
        step = plan.close_step(project, sid, _mail_of(request, body), request.user)
        return f"✅ Крок виконано: {step.title}"
    return _plan_action(request, pk, run)


# ── 📅 Подія календаря → проєкт (шкала: «📅 ＋ Подія з календаря», меню ромба — «✕ Прибрати з проєкту») ──────────

EVENTS_BACK, EVENTS_AHEAD, EVENTS_SHOWN = 45, 90, 30


@staff_member_required
def project_events_view(request, pk):
    """GET — власні події календаря навколо сьогодні, ще не пов'язані з проєктом (ні вручну, ні через лист)."""
    from datetime import timedelta

    from django.utils import timezone

    from calendar_app.models import CalendarEvent
    project = _project_for_change(request, pk)
    now = timezone.now()
    linked = set(ProjectLink.objects.filter(project=project).exclude(event=None).values_list("event_id", flat=True))
    mails = services.project_messages(project).values("pk")
    rows = []
    for e in (CalendarEvent.objects.filter(user=request.user, start_at__gte=now - timedelta(days=EVENTS_BACK),
                                           start_at__lte=now + timedelta(days=EVENTS_AHEAD))
              .exclude(pk__in=linked).exclude(email_message__in=mails)):
        rows.append({"id": e.pk, "title": e.title, "date": timezone.localtime(e.start_at).strftime("%d.%m.%Y"),
                     "type": e.get_event_type_display(), "far": abs((e.start_at - now).total_seconds())})
    rows.sort(key=lambda r: r["far"])
    for r in rows:
        r.pop("far")
    return JsonResponse({"ok": True, "events": rows[:EVENTS_SHOWN]})


@staff_member_required
@require_POST
def project_event_link_view(request, pk):
    """{event, link: true|false} — власна подія календаря до проєкту / геть з проєкту."""
    from calendar_app.models import CalendarEvent

    def run(project, body):
        ev = CalendarEvent.objects.filter(pk=body.get("event") if str(body.get("event") or "").isdigit() else 0,
                                          user=request.user).first()
        if ev is None:
            raise ValueError("Подію не знайдено у вашому календарі")
        if body.get("link", True):
            services.link_object(project, user=request.user, reason="📅 вручну з календаря", event=ev)
            return f"📅 Подію додано до проєкту: {ev.title}"
        ProjectLink.objects.filter(project=project, event=ev).delete()
        return f"✕ Подію прибрано з проєкту: {ev.title}"
    return _plan_action(request, pk, run)
