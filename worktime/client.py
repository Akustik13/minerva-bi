"""Клієнт REST API askDANTE (документація: https://my.askdante.com/rest-api/index.html).

Ключ apiAuthenticationToken іде заголовком (так радить askDANTE); якщо askDANTE відхилив — параметром адреси,
і це запам'ятовується (DanteSettings.key_in_query). Адреса з ключем не потрапляє в повідомлення про помилки.

Методи повертають прості словники (дати — date, час — "HH:MM"), формат askDANTE знає лише цей файл:
- POST /timetracking/{userId} — один готовий запис: дата + початок і кінець (окремих «Kommen»/«Gehen» немає);
- GET /v1/workingTime/{yyyyMMdd}-{yyyyMMdd} — записи за період (до 100 днів), JSON;
- GET /v1/attendances — план дня й відсутності (до 31 дня), задокументовано лише XML.
"""
from __future__ import annotations

import datetime
import json
import xml.etree.ElementTree as ET

import requests

JSON = "application/json;charset=UTF-8"
XML = "application/xml;charset=UTF-8"
TOKEN = "apiAuthenticationToken"


class DanteError(Exception):
    """Помилка askDANTE з кодом для інтерфейсу: unconfigured, auth, bad_request, not_found, busy, unavailable, network."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


_MESSAGES = {
    401: ("auth", "askDANTE відхилив API-Key — перевірте ключ і чи ввімкнено Rest API в askDANTE."),
    403: ("auth", "askDANTE заборонив цю дію для API-Key."),
    404: ("not_found", "askDANTE не знайшов запис (перевірте адресу або ID)."),
    429: ("busy", "askDANTE просить зачекати — забагато запитів."),
}


def hhmm(value) -> str:
    """'08:05:00' / time(8, 5) → '08:05'."""
    if value is None:
        return ""
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    return str(value)[:5]


def minutes_between(start: str, stop: str) -> int:
    """Хвилини між 'HH:MM'; кінець 00:00 означає 24:00 (так askDANTE позначає кінець доби)."""
    h1, m1 = map(int, start.split(":")[:2])
    h2, m2 = map(int, stop.split(":")[:2])
    a, b = h1 * 60 + m1, h2 * 60 + m2
    if b <= a:
        b += 24 * 60
    return b - a


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el, name, default=""):
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else default


def _list(data, key: str) -> list:
    """askDANTE повертає {"users": [...]}, інколи список або один об'єкт — завжди список."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        value = data.get(key, data if "id" in data else [])
        if isinstance(value, dict):   # {"projects": {"project": [...]}}
            value = next(iter(value.values()), []) if len(value) == 1 and "id" not in value else [value]
        return value if isinstance(value, list) else [value]
    return []


def _bool(value) -> bool:
    return value is True or str(value).lower() == "true"


class DanteClient:
    def __init__(self, st):
        if not st.configured:
            raise DanteError("unconfigured", "askDANTE не налаштовано (адреса або API-Key).")
        self.st = st
        self.base = st.base_url.rstrip("/") + "/rest/api"
        self.timeout = max(5, int(st.timeout or 20))

    # ── транспорт ────────────────────────────────────────────────
    def _send(self, method, path, params, body, accept, in_query):
        headers = {"Accept": accept}
        params = dict(params or {})
        if in_query:
            params[TOKEN] = self.st.api_key
        else:
            headers[TOKEN] = self.st.api_key
        data = None
        if body is not None:
            headers["Content-Type"] = JSON
            data = json.dumps(body).encode("utf-8")
        return requests.request(method, self.base + path, params=params, data=data, headers=headers,
                                timeout=self.timeout)

    def _call(self, method: str, path: str, params=None, body=None, accept=JSON):
        try:
            r = self._send(method, path, params, body, accept, self.st.key_in_query)
            if r.status_code in (401, 403) and not self.st.key_in_query:
                retry = self._send(method, path, params, body, accept, True)
                if retry.status_code not in (401, 403) and self.st.pk:
                    self.st.key_in_query = True
                    type(self.st).objects.filter(pk=self.st.pk).update(key_in_query=True)
                r = retry
        except requests.exceptions.SSLError:
            raise DanteError("network", "Помилка HTTPS-сертифіката askDANTE.")
        except requests.exceptions.Timeout:
            raise DanteError("network", "askDANTE не відповів вчасно — невідомо, чи запит виконано.")
        except requests.exceptions.RequestException:
            raise DanteError("network", "Немає зв'язку з askDANTE.")
        if r.status_code == 400:
            raise DanteError("bad_request", "askDANTE не прийняв запит: " + self._field_errors(r), 400)
        if r.status_code in _MESSAGES:
            code, msg = _MESSAGES[r.status_code]
            raise DanteError(code, msg, r.status_code)
        if r.status_code >= 400:
            raise DanteError("unavailable", f"askDANTE повернув помилку HTTP {r.status_code}.", r.status_code)
        return r

    @staticmethod
    def _field_errors(r) -> str:
        try:
            data = r.json()
            errors = data.get("fieldErrors") or data.get("errors") or []
            parts = [f"{e.get('on')}: {e.get('message')}" if e.get("on") else str(e.get("message"))
                     for e in errors if isinstance(e, dict)]
            if parts:
                return "; ".join(parts)
            if data.get("message"):
                return str(data["message"])
        except ValueError:
            pass
        return (r.text or "без пояснення")[:300]

    @staticmethod
    def _json(r):
        try:
            return r.json()
        except ValueError:
            raise DanteError("unavailable", "Відповідь askDANTE не JSON — перевірте адресу askDANTE.")

    # ── довідники ────────────────────────────────────────────────
    def users(self) -> list[dict]:
        data = self._json(self._call("GET", "/users", {"includeArchived": "true"}))
        out = []
        for u in _list(data, "users"):
            access = u.get("onlineAccess") or {}
            out.append({
                "id": str(u.get("id", "")),
                "first_name": u.get("firstName") or "",
                "last_name": u.get("lastName") or "",
                "email": (access.get("email") or u.get("email") or "").strip(),
                "personnel_number": str(u.get("personnelNumber") or ""),
                "archived": _bool(u.get("archived")),
            })
        return [u for u in out if u["id"]]

    def projects(self) -> list[dict]:
        """Усі проєкти з підпроєктами (плоский список, підпроєкт знає parent)."""
        data = self._json(self._call("GET", "/projects"))
        out = []

        def walk(items, parent=None):
            for p in items:
                if not isinstance(p, dict) or not p.get("id"):
                    continue
                customer = p.get("customer") or {}
                out.append({
                    "id": str(p["id"]),
                    "name": p.get("name") or f"#{p['id']}",
                    "reference_number": p.get("referenceNumber") or "",
                    "customer_name": customer.get("name") or "",
                    "archived": _bool(p.get("archived")),
                    "time_trackable": p.get("timeTrackable") is not False,
                    "parent": parent,
                })
                subs = p.get("subProjects") or {}
                walk(_list(subs, "projects"), str(p["id"]))

        walk(_list(data, "projects"))
        return out

    def activities(self) -> list[dict]:
        data = self._json(self._call("GET", "/activities"))
        return [{"id": str(a["id"]), "name": a.get("name") or f"#{a['id']}", "archived": _bool(a.get("archived"))}
                for a in _list(data, "activities") if isinstance(a, dict) and a.get("id")]

    # ── дні ──────────────────────────────────────────────────────
    def working_time(self, date_from: datetime.date, date_to: datetime.date, dante_id: str) -> dict:
        """{date: [{"coming": "08:00", "leaving": "16:30" | None, "source": "MANUAL", "minutes": 510}]}"""
        out = {}
        start = date_from
        while start <= date_to:
            end = min(date_to, start + datetime.timedelta(days=99))
            data = self._json(self._call("GET", f"/v1/workingTime/{start:%Y%m%d}-{end:%Y%m%d}", {"id": dante_id}))
            for day in _list(data, "days"):
                try:
                    d = datetime.date.fromisoformat(str(day.get("date"))[:10])
                except ValueError:
                    continue
                times = []
                for emp in day.get("employee") or []:
                    if str(emp.get("id", dante_id)) != str(dante_id):
                        continue
                    for t in emp.get("times") or []:
                        coming = hhmm((t.get("coming") or {}).get("time"))
                        leaving = hhmm((t.get("leaving") or {}).get("time")) or None
                        times.append({
                            "coming": coming, "leaving": leaving,
                            "source": (t.get("coming") or {}).get("source") or "",
                            "minutes": minutes_between(coming, leaving) if coming and leaving else 0,
                        })
                out[d] = sorted(times, key=lambda t: t["coming"])
            start = end + datetime.timedelta(days=1)
        return out

    def attendances(self, date_from: datetime.date, date_to: datetime.date,
                    email: str = "", personnel_number: str = "") -> dict:
        """{date: {"planned": "BUSINESS_DAY", "absences": [{"type", "reason", "ratio"}]}}; без логіна — {}."""
        if not (email or personnel_number):
            return {}
        who = {"login": email} if email else {"personnelNumber": personnel_number}
        out = {}
        start = date_from
        while start <= date_to:
            end = min(date_to, start + datetime.timedelta(days=30))
            r = self._call("GET", "/v1/attendances",
                           {**who, "date": start.isoformat(), "enddate": end.isoformat()}, accept=XML)
            out.update(self._parse_attendances(r))
            start = end + datetime.timedelta(days=1)
        return out

    @staticmethod
    def _parse_attendances(r) -> dict:
        body = (r.text or "").lstrip()
        out = {}
        if body.startswith("<"):
            try:
                root = ET.fromstring(body.encode("utf-8"))
            except ET.ParseError:
                raise DanteError("unavailable", "askDANTE повернув некоректний XML відсутностей.")
            for day in (el for el in root.iter() if _local(el.tag) == "attendanceDay"):
                absences = []
                box = _child(day, "absences")
                for a in (box if box is not None else []):
                    absences.append({"type": _text(a, "absenceType"), "reason": _text(a, "reason"),
                                     "ratio": float(_text(a, "dayRatio", "1") or 1)})
                _put_attendance(out, _text(day, "date"), _text(day, "plannedState"), absences)
            return out
        try:
            data = json.loads(body or "{}")
        except ValueError:
            raise DanteError("unavailable", "Відповідь askDANTE про відсутності не XML і не JSON.")

        def walk(node):
            if isinstance(node, dict):
                if "plannedState" in node and "date" in node:
                    raw = node.get("absences") or []
                    if isinstance(raw, dict):
                        raw = raw.get("absence") or []
                    raw = raw if isinstance(raw, list) else [raw]
                    _put_attendance(out, node.get("date"), node.get("plannedState"),
                                    [{"type": a.get("absenceType") or "", "reason": a.get("reason") or "",
                                      "ratio": float(a.get("dayRatio") or 1)} for a in raw if isinstance(a, dict)])
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        return out

    # ── запис ────────────────────────────────────────────────────
    def track(self, dante_id: str, record: dict) -> None:
        """Один запис: {"date": "2026-09-10", "start": "08:00", "stop": "16:30"} або час на проєкт з projectData."""
        self._call("POST", f"/timetracking/{dante_id}", body=record)

    def create_project(self, name: str, reference_number: str = "") -> dict:
        body = {"name": name[:255], "timeTrackable": True, "archived": False}
        if reference_number:
            body["referenceNumber"] = reference_number
        r = self._call("POST", "/projects", body=body)
        try:
            p = r.json()
        except ValueError:
            p = {}
        return {"id": str(p.get("id") or ""), "name": p.get("name") or name,
                "reference_number": p.get("referenceNumber") or reference_number}


def _put_attendance(out: dict, date_value, planned, absences):
    try:
        d = datetime.date.fromisoformat(str(date_value)[:10])
    except ValueError:
        return
    out[d] = {"planned": (planned or "").strip(), "absences": absences}
