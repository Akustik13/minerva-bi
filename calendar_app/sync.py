"""Двостороння синхронізація календарів (рівень 2): Google Calendar API, Microsoft Graph (Outlook / Microsoft 365)
і календар Minerva — правилами «звідки → куди» на сторінці /calendar/connections/.

- OAuth: ключі застосунків вводить адміністратор (CalendarSyncSettings), кожен користувач підключає свої акаунти;
  токени лише на сервері.
- Правило копіює події вікна (−days_back … +days_ahead); повторювані — окремими подіями (Google singleEvents,
  Graph calendarView).
- Захист від петель: кожна копія записана в SyncLink (оригінал → копія). Подія, яка є чиєюсь копією, ніколи не стає
  джерелом — тож Outlook → Google і Google → Outlook не множать події. Змінювати треба оригінал (джерело виграє).
- Зміни оригіналу оновлюють копію (відбиток полів), видалення — видаляє копію (propagate_delete).
- «Перегляд» показує, що буде створено / змінено / видалено, нічого не записуючи.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import requests
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from .invites import JOIN_RE
from .models import (CalendarAccount, CalendarEvent, CalendarSyncSettings, RemoteCalendar, SyncLink, SyncRule)

logger = logging.getLogger(__name__)
TIMEOUT = 20
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/calendar"
MS_SCOPES = "offline_access User.Read Calendars.ReadWrite"
GRAPH = "https://graph.microsoft.com/v1.0"


class SyncError(Exception):
    pass


# ── OAuth ───────────────────────────────────────────────────────────────────
def redirect_uri(request, provider: str) -> str:
    return request.build_absolute_uri(reverse("calendar_app:oauth_callback", args=[provider]))


def configured(provider: str) -> bool:
    s = CalendarSyncSettings.get()
    return bool(s.google_client_id and s.google_client_secret) if provider == CalendarAccount.GOOGLE \
        else bool(s.ms_client_id and s.ms_client_secret)


def _ms_base(s) -> str:
    return f"https://login.microsoftonline.com/{s.ms_tenant or 'common'}/oauth2/v2.0"


def auth_url(request, provider: str) -> str:
    s = CalendarSyncSettings.get()
    state = secrets.token_urlsafe(24)
    request.session["cal_oauth_state"] = f"{provider}:{state}"
    if provider == CalendarAccount.GOOGLE:
        return GOOGLE_AUTH + "?" + urlencode({
            "client_id": s.google_client_id, "redirect_uri": redirect_uri(request, provider), "response_type": "code",
            "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent", "state": state})
    return _ms_base(s) + "/authorize?" + urlencode({
        "client_id": s.ms_client_id, "redirect_uri": redirect_uri(request, provider), "response_type": "code",
        "response_mode": "query", "scope": MS_SCOPES, "state": state, "prompt": "select_account"})


def _token(provider: str, data: dict) -> dict:
    s = CalendarSyncSettings.get()
    if provider == CalendarAccount.GOOGLE:
        url, data = GOOGLE_TOKEN, {**data, "client_id": s.google_client_id, "client_secret": s.google_client_secret}
    else:
        url, data = _ms_base(s) + "/token", {**data, "client_id": s.ms_client_id,
                                             "client_secret": s.ms_client_secret, "scope": MS_SCOPES}
    try:
        r = requests.post(url, data=data, timeout=TIMEOUT)
    except requests.RequestException:
        raise SyncError("немає зв'язку з сервером входу")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in body:
        raise SyncError(body.get("error_description") or body.get("error") or f"HTTP {r.status_code}")
    return body


def _store(acc: CalendarAccount, body: dict):
    acc.access_token = body["access_token"]
    if body.get("refresh_token"):
        acc.refresh_token = body["refresh_token"]
    acc.expires_at = timezone.now() + datetime.timedelta(seconds=int(body.get("expires_in") or 3600) - 60)


def finish(request, provider: str, code: str) -> CalendarAccount:
    body = _token(provider, {"code": code, "redirect_uri": redirect_uri(request, provider),
                             "grant_type": "authorization_code"})
    acc = CalendarAccount(user=request.user, provider=provider)
    _store(acc, body)
    email = client(acc).email()
    existing = CalendarAccount.objects.filter(user=request.user, provider=provider, email=email).first()
    if existing:
        existing.access_token, existing.expires_at = acc.access_token, acc.expires_at
        existing.refresh_token = acc.refresh_token or existing.refresh_token
        acc = existing
    acc.email, acc.last_error = email, ""
    acc.save()
    refresh_calendars(acc)
    return acc


# ── клієнти ─────────────────────────────────────────────────────────────────
def client(acc: CalendarAccount):
    return GoogleClient(acc) if acc.provider == CalendarAccount.GOOGLE else GraphClient(acc)


class _Base:
    def __init__(self, acc: CalendarAccount):
        self.acc = acc

    def _auth(self) -> dict:
        if self.acc.pk and self.acc.expires_at and self.acc.expires_at <= timezone.now() and self.acc.refresh_token:
            self.refresh()
        return {"Authorization": f"Bearer {self.acc.access_token}"}

    def refresh(self):
        if not self.acc.refresh_token:
            raise SyncError("доступ закінчився — підключіть акаунт ще раз")
        _store(self.acc, _token(self.acc.provider, {"refresh_token": self.acc.refresh_token,
                                                    "grant_type": "refresh_token"}))
        if self.acc.pk:
            self.acc.save(update_fields=["access_token", "refresh_token", "expires_at"])

    def _req(self, method, url, retry=True, **kw):
        extra = kw.pop("headers", {})
        try:
            r = requests.request(method, url, headers={**extra, **self._auth()}, timeout=TIMEOUT, **kw)
        except requests.RequestException:
            raise SyncError(f"немає зв'язку з {self.name}")
        if r.status_code == 401 and retry and self.acc.refresh_token:
            self.refresh()
            return self._req(method, url, retry=False, headers=extra, **kw)
        if r.status_code == 404 and method == "DELETE":
            return r   # уже видалено
        if r.status_code >= 400:
            try:
                err = r.json().get("error")
                msg = err.get("message") if isinstance(err, dict) else err
            except ValueError:
                msg = ""
            raise SyncError(f"{self.name}: HTTP {r.status_code} {msg or ''}".strip())
        return r


def _dt(value: dict | None):
    """{dateTime: …, timeZone} / {date: …} → (aware datetime, весь день?)."""
    value = value or {}
    if value.get("date") and not value.get("dateTime"):
        d = parse_date(value["date"])
        return timezone.make_aware(datetime.datetime.combine(d, datetime.time.min)), True
    dt = parse_datetime(value.get("dateTime") or "")
    if dt is None:
        return None, False
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=datetime.timezone.utc)   # Graph з Prefer: UTC
    return dt, False


def _meeting(ev: dict) -> bool:
    return bool(ev.get("attendees")) or bool(JOIN_RE.search(f"{ev.get('location', '')} {ev.get('description', '')}"))


class GoogleClient(_Base):
    name = "Google"

    def email(self) -> str:
        r = self._req("GET", "https://www.googleapis.com/oauth2/v3/userinfo")
        return r.json().get("email") or ""

    def calendars(self) -> list[dict]:
        r = self._req("GET", f"{GOOGLE_API}/users/me/calendarList")
        return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"],
                 "color": (c.get("backgroundColor") or "")[:7], "primary": bool(c.get("primary")),
                 "can_write": c.get("accessRole") in ("owner", "writer")} for c in r.json().get("items", [])]

    def events(self, cal_id: str, start, end) -> list[dict]:
        out, token = [], None
        while True:
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "maxResults": 2500, "showDeleted": "false"}
            if token:
                params["pageToken"] = token
            data = self._req("GET", f"{GOOGLE_API}/calendars/{requests.utils.quote(cal_id, safe='')}/events",
                             params=params).json()
            for e in data.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                s, all_day = _dt(e.get("start"))
                en, _ = _dt(e.get("end"))
                if s is None:
                    continue
                out.append({"id": e["id"], "title": e.get("summary") or "(без назви)", "start": s, "end": en,
                            "all_day": all_day, "location": e.get("location") or "",
                            "description": e.get("description") or "", "attendees": e.get("attendees") or [],
                            "private": e.get("visibility") in ("private", "confidential")})
            token = data.get("nextPageToken")
            if not token:
                return out

    @staticmethod
    def _body(ev: dict) -> dict:
        if ev["all_day"]:
            d = timezone.localtime(ev["start"]).date()
            end = timezone.localtime(ev["end"]).date() if ev.get("end") else d + datetime.timedelta(days=1)
            when = {"start": {"date": d.isoformat()}, "end": {"date": max(end, d + datetime.timedelta(days=1)).isoformat()}}
        else:
            end = ev.get("end") or ev["start"] + datetime.timedelta(minutes=30)
            when = {"start": {"dateTime": ev["start"].isoformat()}, "end": {"dateTime": end.isoformat()}}
        return {"summary": ev["title"], "description": ev.get("description", ""), "location": ev.get("location", ""),
                **when}

    def create(self, cal_id, ev) -> str:
        return self._req("POST", f"{GOOGLE_API}/calendars/{requests.utils.quote(cal_id, safe='')}/events",
                         json=self._body(ev)).json()["id"]

    def update(self, cal_id, event_id, ev):
        self._req("PATCH", f"{GOOGLE_API}/calendars/{requests.utils.quote(cal_id, safe='')}/events/{event_id}",
                  json=self._body(ev))

    def delete(self, cal_id, event_id):
        self._req("DELETE", f"{GOOGLE_API}/calendars/{requests.utils.quote(cal_id, safe='')}/events/{event_id}")


class GraphClient(_Base):
    name = "Outlook"

    def email(self) -> str:
        me = self._req("GET", f"{GRAPH}/me").json()
        return me.get("mail") or me.get("userPrincipalName") or ""

    def calendars(self) -> list[dict]:
        data = self._req("GET", f"{GRAPH}/me/calendars").json()
        return [{"id": c["id"], "name": c.get("name") or "Календар", "color": (c.get("hexColor") or "")[:7],
                 "primary": bool(c.get("isDefaultCalendar")), "can_write": bool(c.get("canEdit", True))}
                for c in data.get("value", [])]

    def events(self, cal_id, start, end) -> list[dict]:
        out = []
        url = f"{GRAPH}/me/calendars/{cal_id}/calendarView"
        params = {"startDateTime": start.astimezone(datetime.timezone.utc).isoformat(),
                  "endDateTime": end.astimezone(datetime.timezone.utc).isoformat(), "$top": 500}
        while url:
            data = self._req("GET", url, params=params, headers={"Prefer": 'outlook.timezone="UTC"'}).json()
            for e in data.get("value", []):
                if e.get("isCancelled"):
                    continue
                s, _ = _dt(e.get("start"))
                en, _ = _dt(e.get("end"))
                if s is None:
                    continue
                all_day = bool(e.get("isAllDay"))
                if all_day:   # дата з UTC-півночі, без зсуву поясу
                    s = timezone.make_aware(datetime.datetime.combine(s.date(), datetime.time.min))
                    en = timezone.make_aware(datetime.datetime.combine(en.date(), datetime.time.min)) if en else None
                out.append({"id": e["id"], "title": e.get("subject") or "(без назви)", "start": s, "end": en,
                            "all_day": all_day, "location": (e.get("location") or {}).get("displayName") or "",
                            "description": e.get("bodyPreview") or "", "attendees": e.get("attendees") or [],
                            "private": e.get("sensitivity") in ("private", "confidential")})
            url, params = data.get("@odata.nextLink"), None
        return out

    @staticmethod
    def _body(ev: dict) -> dict:
        if ev["all_day"]:
            d = timezone.localtime(ev["start"]).date()
            end = timezone.localtime(ev["end"]).date() if ev.get("end") else d + datetime.timedelta(days=1)
            end = max(end, d + datetime.timedelta(days=1))
            when = {"isAllDay": True, "start": {"dateTime": f"{d}T00:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": f"{end}T00:00:00", "timeZone": "UTC"}}
        else:
            end = ev.get("end") or ev["start"] + datetime.timedelta(minutes=30)
            when = {"isAllDay": False,
                    "start": {"dateTime": ev["start"].astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                              "timeZone": "UTC"},
                    "end": {"dateTime": end.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                            "timeZone": "UTC"}}
        return {"subject": ev["title"], "body": {"contentType": "text", "content": ev.get("description", "")},
                "location": {"displayName": ev.get("location", "")}, **when}

    def create(self, cal_id, ev) -> str:
        return self._req("POST", f"{GRAPH}/me/calendars/{cal_id}/events", json=self._body(ev)).json()["id"]

    def update(self, cal_id, event_id, ev):
        self._req("PATCH", f"{GRAPH}/me/events/{event_id}", json=self._body(ev))

    def delete(self, cal_id, event_id):
        self._req("DELETE", f"{GRAPH}/me/events/{event_id}")


def refresh_calendars(acc: CalendarAccount) -> int:
    items = client(acc).calendars()
    seen = set()
    for c in items:
        seen.add(c["id"])
        RemoteCalendar.objects.update_or_create(account=acc, remote_id=c["id"], defaults={
            "name": c["name"][:200], "color": c["color"], "primary": c["primary"], "can_write": c["can_write"]})
    RemoteCalendar.objects.filter(account=acc).exclude(remote_id__in=seen).delete()
    return len(items)


# ── сторони правила: зовнішній календар або Minerva ─────────────────────────
class MinervaSide:
    name = "Minerva"

    def __init__(self, user):
        self.user = user
        self.key_prefix = "m:0:"

    def events(self, start, end) -> list[dict]:
        out = []
        for ev in CalendarEvent.objects.filter(user=self.user, start_at__gte=start, start_at__lte=end):
            out.append({"id": str(ev.pk), "title": ev.title, "start": ev.start_at, "end": ev.end_at,
                        "all_day": ev.all_day, "location": "", "description": ev.description or "",
                        "attendees": [1] if ev.event_type == CalendarEvent.TYPE_MEETING else [],
                        "from_mail": bool(ev.email_message_id), "private": False})
        return out

    def create(self, ev) -> str:
        meeting = bool(ev.get("attendees")) or bool(JOIN_RE.search(f"{ev.get('location', '')} {ev.get('description', '')}"))
        desc = "\n".join(x for x in (f"📍 {ev['location']}" if ev.get("location") else "", ev.get("description", "")) if x)
        obj = CalendarEvent.objects.create(
            user=self.user, title=ev["title"][:300], description=desc[:5000], start_at=ev["start"],
            end_at=ev.get("end"), all_day=ev["all_day"],
            event_type=CalendarEvent.TYPE_MEETING if meeting else CalendarEvent.TYPE_OTHER)
        return str(obj.pk)

    def update(self, event_id, ev):
        desc = "\n".join(x for x in (f"📍 {ev['location']}" if ev.get("location") else "", ev.get("description", "")) if x)
        CalendarEvent.objects.filter(pk=event_id, user=self.user).update(
            title=ev["title"][:300], description=desc[:5000], start_at=ev["start"], end_at=ev.get("end"),
            all_day=ev["all_day"])

    def delete(self, event_id):
        CalendarEvent.objects.filter(pk=event_id, user=self.user).delete()


class RemoteSide:
    def __init__(self, cal: RemoteCalendar):
        self.cal, self.c = cal, client(cal.account)
        self.name = cal.name
        self.key_prefix = f"r:{cal.pk}:"

    def events(self, start, end):
        return self.c.events(self.cal.remote_id, start, end)

    def create(self, ev) -> str:
        return self.c.create(self.cal.remote_id, ev)

    def update(self, event_id, ev):
        self.c.update(self.cal.remote_id, event_id, ev)

    def delete(self, event_id):
        self.c.delete(self.cal.remote_id, event_id)


def side(user, cal: RemoteCalendar | None):
    return MinervaSide(user) if cal is None else RemoteSide(cal)


def _fingerprint(ev: dict) -> str:
    raw = "|".join(str(ev.get(k) or "") for k in ("title", "start", "end", "all_day", "location", "description"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _shape(rule: SyncRule, ev: dict, source_name: str) -> dict:
    if rule.mode == SyncRule.BUSY or ev.get("private"):
        return {**ev, "title": "Зайнято", "description": "", "location": ""}
    if rule.mode == SyncRule.PREFIX:
        return {**ev, "title": f"[{source_name}] {ev['title']}"}
    return ev


def run(rule: SyncRule, dry_run: bool = False) -> dict:
    """Одне правило. {"create": [...], "update": [...], "delete": [...], "errors": [...]} — назви подій."""
    now = timezone.now()
    start, end = now - datetime.timedelta(days=rule.days_back), now + datetime.timedelta(days=rule.days_ahead)
    if rule.target is not None and not rule.target.can_write:
        raise SyncError(f"у календар «{rule.target}» немає права запису")
    src, dst = side(rule.user, rule.source), side(rule.user, rule.target)
    copies = set(SyncLink.objects.filter(rule__user=rule.user, target_key__startswith=src.key_prefix)
                 .values_list("target_key", flat=True))   # копії, зроблені Minerva, — не джерело
    links = {l.source_key: l for l in SyncLink.objects.filter(rule=rule)}
    plan = {"create": [], "update": [], "delete": [], "errors": []}
    seen = set()
    for ev in src.events(start, end):
        key = src.key_prefix + ev["id"]
        if key in copies:
            continue
        if rule.what == SyncRule.MEETINGS and not _meeting(ev):
            continue
        if rule.what == SyncRule.MAIL and not ev.get("from_mail"):
            continue
        if rule.keyword and rule.keyword.lower() not in ev["title"].lower():
            continue
        seen.add(key)
        shaped = _shape(rule, ev, src.name)
        fp = _fingerprint(shaped)
        link = links.get(key)
        label = f"{timezone.localtime(ev['start']):%d.%m %H:%M} {shaped['title']}"
        try:
            if link is None:
                plan["create"].append(label)
                if not dry_run:
                    tid = dst.create(shaped)
                    SyncLink.objects.create(rule=rule, source_key=key, target_key=dst.key_prefix + tid, fingerprint=fp)
            elif link.fingerprint != fp:
                plan["update"].append(label)
                if not dry_run:
                    dst.update(link.target_key[len(dst.key_prefix):], shaped)
                    link.fingerprint = fp
                    link.save(update_fields=["fingerprint", "updated_at"])
        except SyncError as e:
            plan["errors"].append(f"{label}: {e}")
            if len(plan["errors"]) >= 5:
                break
    for key, link in links.items():
        if key in seen or not link.target_key.startswith(dst.key_prefix):
            continue
        # оригінал зник у вікні (видалено або перенесено за межі) — копію прибрати
        plan["delete"].append(key)
        if not dry_run:
            try:
                if rule.propagate_delete:
                    dst.delete(link.target_key[len(dst.key_prefix):])
                link.delete()
            except SyncError as e:
                plan["errors"].append(str(e))
    if not dry_run:
        rule.last_run = timezone.now()
        rule.last_result = summary(plan)[:255]
        rule.save(update_fields=["last_run", "last_result"])
    return plan


def summary(plan: dict) -> str:
    parts = [f"створено {len(plan['create'])}", f"змінено {len(plan['update'])}", f"прибрано {len(plan['delete'])}"]
    if plan["errors"]:
        parts.append(f"помилок {len(plan['errors'])}: {plan['errors'][0]}")
    return " · ".join(parts)


def run_due(now=None) -> list[str]:
    now = now or timezone.now()
    interval = datetime.timedelta(minutes=max(5, CalendarSyncSettings.get().interval_minutes))
    out = []
    for rule in SyncRule.objects.filter(enabled=True).select_related("source__account", "target__account", "user"):
        if rule.last_run and now - rule.last_run < interval:
            continue
        try:
            out.append(f"{rule}: {summary(run(rule))}")
        except SyncError as e:
            rule.last_run, rule.last_result = now, f"❌ {e}"[:255]
            rule.save(update_fields=["last_run", "last_result"])
            out.append(f"{rule}: ❌ {e}")
    return out


# ── сторінка й дії ──────────────────────────────────────────────────────────
def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def page_context(request) -> dict:
    from core.secrets import placeholder
    from worktime.services import is_manager
    s = CalendarSyncSettings.get()
    accounts = list(CalendarAccount.objects.filter(user=request.user).prefetch_related("calendars"))
    cals = [c for a in accounts for c in a.calendars.all()]
    return {
        "sync_on": True, "sync_admin": is_manager(request.user), "sync_settings": s,
        "google_ready": configured(CalendarAccount.GOOGLE), "ms_ready": configured(CalendarAccount.MICROSOFT),
        "google_secret_ph": placeholder(s.google_client_secret), "ms_secret_ph": placeholder(s.ms_client_secret),
        "google_redirect": redirect_uri(request, CalendarAccount.GOOGLE),
        "ms_redirect": redirect_uri(request, CalendarAccount.MICROSOFT),
        "accounts": accounts, "remote_calendars": cals,
        "rules": list(SyncRule.objects.filter(user=request.user).select_related("source__account", "target__account")),
        "rule_what": SyncRule.WHAT, "rule_modes": SyncRule.MODES,
        "preview": request.session.pop("cal_sync_preview", None),
    }


@staff_member_required
def oauth_start(request, provider):
    if provider not in (CalendarAccount.GOOGLE, CalendarAccount.MICROSOFT) or not configured(provider):
        messages.error(request, "Спершу адміністратор має внести ключі застосунку (нижче на сторінці).")
        return redirect("/calendar/connections/")
    return redirect(auth_url(request, provider))


@staff_member_required
def oauth_callback(request, provider):
    want = request.session.pop("cal_oauth_state", "")
    if want != f"{provider}:{request.GET.get('state', '')}":
        return HttpResponseBadRequest("Неправильний state — почніть підключення ще раз.")
    if request.GET.get("error"):
        messages.error(request, f"Підключення скасовано: {request.GET.get('error_description') or request.GET['error']}")
        return redirect("/calendar/connections/")
    try:
        acc = finish(request, provider, request.GET.get("code", ""))
        messages.success(request, f"✅ Підключено {acc}: календарів {acc.calendars.count()}.")
    except SyncError as e:
        messages.error(request, f"❌ {e}")
    return redirect("/calendar/connections/")


@staff_member_required
@require_POST
def sync_action(request):
    from worktime.services import is_manager
    p, user = request.POST, request.user
    action = p.get("action")
    back = redirect("/calendar/connections/#sync")
    if action == "keys":
        if not is_manager(user):
            return back
        s = CalendarSyncSettings.get()
        s.google_client_id = p.get("google_client_id", "").strip()
        s.ms_client_id = p.get("ms_client_id", "").strip()
        s.ms_tenant = p.get("ms_tenant", "").strip() or "common"
        for f in ("google_client_secret", "ms_client_secret"):
            if p.get(f, "").strip():
                setattr(s, f, p[f].strip())
            if p.get(f"clear_{f}"):
                setattr(s, f, "")
        s.interval_minutes = max(5, min(240, _int(p.get("interval_minutes"), 10)))
        s.save()
        messages.success(request, "Ключі збережено.")
        return back
    if action == "disconnect":
        CalendarAccount.objects.filter(pk=p.get("account") or 0, user=user).delete()
        messages.success(request, "Акаунт відключено (правила з ним видалено, події лишились).")
        return back
    if action == "calendars":
        acc = CalendarAccount.objects.filter(pk=p.get("account") or 0, user=user).first()
        if acc:
            try:
                messages.success(request, f"Календарів: {refresh_calendars(acc)}")
            except SyncError as e:
                messages.error(request, f"❌ {e}")
        return back
    own = {c.pk: c for c in RemoteCalendar.objects.filter(account__user=user)}

    def pick(name):
        v = p.get(name) or ""
        return own.get(int(v)) if v.isdigit() else None

    if action == "rule_add":
        source, target = pick("source"), pick("target")
        if source == target:
            messages.error(request, "«Звідки» і «куди» мають бути різними календарями.")
            return back
        rule = SyncRule.objects.create(
            user=user, source=source, target=target,
            what=p.get("what") if p.get("what") in dict(SyncRule.WHAT) else SyncRule.ALL,
            mode=p.get("mode") if p.get("mode") in dict(SyncRule.MODES) else SyncRule.FULL,
            keyword=(p.get("keyword") or "").strip()[:100],
            days_back=max(0, min(365, _int(p.get("days_back"), 7))),
            days_ahead=max(1, min(730, _int(p.get("days_ahead"), 90))),
            propagate_delete=p.get("propagate_delete") == "1", enabled=False)
        messages.success(request, f"Правило «{rule}» додано вимкненим — спершу «👁 Перегляд», потім увімкніть.")
        return back
    rule = SyncRule.objects.filter(pk=p.get("rule") or 0, user=user).select_related(
        "source__account", "target__account").first()
    if rule is None:
        return back
    if action == "rule_delete":
        rule.delete()
        messages.success(request, "Правило видалено (уже скопійовані події лишились).")
    elif action == "rule_toggle":
        rule.enabled = not rule.enabled
        rule.save(update_fields=["enabled"])
    elif action in ("rule_preview", "rule_run"):
        try:
            plan = run(rule, dry_run=action == "rule_preview")
            if action == "rule_preview":
                request.session["cal_sync_preview"] = {"rule": str(rule), **{k: v[:30] for k, v in plan.items()},
                                                       "counts": {k: len(v) for k, v in plan.items()}}
            else:
                messages.success(request, f"{rule}: {summary(plan)}")
        except SyncError as e:
            messages.error(request, f"❌ {e}")
    return back
