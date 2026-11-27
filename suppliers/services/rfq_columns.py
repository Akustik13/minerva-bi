"""➕ Свої колонки таблиці пропозицій запиту постачальникам: «Сума з ENIG», «Hard gold», «Покриття», «Зразки до»…
Колонки — у запиті (SupplierRequest.columns, спільні для всіх), значення — у рядку постачальника (Recipient.extra).

  KINDS          — price: число на кожну кількість (сума чи ціна — як назвали колонку), у валюті пропозиції
                   постачальника; таблиця переводить у валюту Minerva й позначає найнижче; text — короткий текст.
  columns(req)   — чисті колонки [{key, name, kind, hint}];
  add / edit / delete — змінити колонки (delete прибирає й значення в усіх рядках);
  set_value(r, key, qty, value, user, src) — вписати вручну (src user) або з пропозиції агента після ✓ людини (src
                   agent, MCP — agent/tools_rfq_table.py); такого AI не перезаписує;
  number(value)  — «711,46» / «1.291,69» → Decimal ≥ 0 (None — не число);
  ai_prompt(req) / apply_ai(r, data) — що просити в 🧠 «Розібрати відповідь» і як записати (лише не вписане людиною);
  cells(cmp, req) — для шаблону й ⚖ порівняння: на кожен рядок — колонки, по кількостях значення, найнижче.
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone

KINDS = {"price": "💶 Число на кожну кількість", "text": "📝 Текст"}
MAX_COLUMNS = 8
NAME_MAX, HINT_MAX, TEXT_MAX = 40, 200, 300
CENT = Decimal("0.01")


class ColumnError(Exception):
    pass


def _clean(c) -> dict:
    if not isinstance(c, dict) or not re.fullmatch(r"c\d{1,3}", str(c.get("key") or "")):
        return None
    name = " ".join(str(c.get("name") or "").split())[:NAME_MAX]
    if not name:
        return None
    return {"key": c["key"], "name": name, "kind": c.get("kind") if c.get("kind") in KINDS else "price",
            "hint": " ".join(str(c.get("hint") or "").split())[:HINT_MAX]}


def columns(req) -> list:
    return [c for c in (_clean(x) for x in (req.columns or [])) if c]


def add(req, name, kind="price", hint="") -> dict:
    cols = columns(req)
    if len(cols) >= MAX_COLUMNS:
        raise ColumnError(f"Своїх колонок — щонайбільше {MAX_COLUMNS}.")
    name = " ".join(str(name or "").split())[:NAME_MAX]
    if not name:
        raise ColumnError("Назвіть колонку — напр. «Сума з ENIG».")
    if any(c["name"].lower() == name.lower() for c in cols):
        raise ColumnError("Така колонка вже є.")
    used = {int(c["key"][1:]) for c in cols}
    key = f"c{next(i for i in range(1, 1000) if i not in used)}"
    col = _clean({"key": key, "name": name, "kind": kind, "hint": hint})
    req.columns = cols + [col]
    req.save(update_fields=["columns"])
    return col


def edit(req, key, name=None, hint=None) -> dict:
    cols = columns(req)
    col = next((c for c in cols if c["key"] == key), None)
    if col is None:
        raise ColumnError("Такої колонки немає.")
    if name is not None:
        name = " ".join(str(name).split())[:NAME_MAX]
        if not name:
            raise ColumnError("Назва не може бути порожньою.")
        if any(c["name"].lower() == name.lower() and c["key"] != key for c in cols):
            raise ColumnError("Така колонка вже є.")
        col["name"] = name
    if hint is not None:
        col["hint"] = " ".join(str(hint).split())[:HINT_MAX]
    req.columns = cols
    req.save(update_fields=["columns"])
    return col


def delete(req, key):
    from suppliers.models import SupplierRequestRecipient as R
    req.columns = [c for c in columns(req) if c["key"] != key]
    req.save(update_fields=["columns"])
    for r in R.objects.filter(request=req).exclude(extra={}):
        if key in (r.extra or {}):
            x = dict(r.extra)
            x.pop(key, None)
            R.objects.filter(pk=r.pk).update(extra=x)


_EXP = re.compile(r"\d+(?:\.\d+)?[eE][+-]?\d+")


def _num(value):
    raw = str(value or "").strip()
    if _EXP.fullmatch(raw):              # «4.5E+2» — так до 11.2026 зберігалось кругле 450 (і показувалось 4,52)
        try:
            d = Decimal(raw)
        except InvalidOperation:
            return None
        return d.quantize(Decimal("0.0001"), ROUND_HALF_UP) if d >= 0 else None
    s = re.sub(r"[^\d,.\-]", "", raw).strip()
    if not s:
        return None
    if "," in s and "." in s:            # 1.291,69 / 1,291.69 — остання — десятковий знак
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d.quantize(Decimal("0.0001"), ROUND_HALF_UP) if d >= 0 else None


number = _num


def plain(d) -> str:
    """Decimal → рядок без експоненти: 450 → «450» (str(normalize()) дає «4.5E+2»)."""
    return format(d.normalize(), "f")

KEPT = ("user", "agent")        # вписала людина / агент після ✓ — 🧠 AI не перезаписує


def set_value(r, key, qty, value, user=None, src="user") -> dict:
    """Вписати значення: ціна — для кількості qty (порожньо — прибрати), текст — увесь. src — user / agent."""
    from suppliers.models import SupplierRequestRecipient as R
    col = next((c for c in columns(r.request) if c["key"] == key), None)
    if col is None:
        raise ColumnError("Такої колонки немає.")
    x = dict(r.extra or {})
    cur = dict(x.get(key) or {})
    if col["kind"] == "text":
        v = " ".join(str(value or "").split())[:TEXT_MAX]
        cur["v"] = v
    else:
        try:
            q = int(qty)
        except (TypeError, ValueError):
            raise ColumnError("Не вказано кількість.") from None
        vals = dict(cur.get("v") or {}) if isinstance(cur.get("v"), dict) else {}
        d = _num(value)
        if str(value or "").strip() and d is None:
            raise ColumnError("Не схоже на число — напр. 711,46.")
        if d is None:
            vals.pop(str(q), None)
        else:
            vals[str(q)] = plain(d)
        cur["v"] = vals
    cur.update(src=src if src in KEPT else "user", at=timezone.now().isoformat())
    if cur["v"] in ("", {}):
        x.pop(key, None)
    else:
        x[key] = cur
    R.objects.filter(pk=r.pk).update(extra=x)
    r.extra = x
    return x.get(key) or {}


# ── 🧠 AI ──

def ai_prompt(req) -> tuple:
    """(опис колонок для AI, шматок JSON) — '' якщо колонок немає."""
    cols = columns(req)
    if not cols:
        return "", ""
    lines = []
    for c in cols:
        what = ("число на кожну кількість — рівно як назвав постачальник, у валюті його пропозиції; немає — не пиши"
                if c["kind"] == "price" else "короткий текст (до 200 знаків); немає — не пиши")
        lines.append(f'"{c["key"]}" — «{c["name"]}»{(" (" + c["hint"] + ")") if c["hint"] else ""}: {what}')
    sample = ", ".join(f'"{c["key"]}": ' + ('{"100": 12.5}' if c["kind"] == "price" else '"…"') for c in cols)
    return ("Свої колонки таблиці (поле extra): " + "; ".join(lines) + ". Лише те, що постачальник справді назвав.",
            ', "extra": {' + sample + '}')


def clean_ai(req, data) -> dict:
    """extra з відповіді AI → {key: v} лише відомих колонок, ціни — числа ≥ 0 на кількість."""
    out = {}
    raw = data.get("extra") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return out
    for c in columns(req):
        v = raw.get(c["key"])
        if c["kind"] == "text":
            t = " ".join(str(v or "").split())[:TEXT_MAX] if isinstance(v, (str, int, float)) else ""
            if t:
                out[c["key"]] = t
        elif isinstance(v, dict):
            vals = {}
            for q, n in list(v.items())[:12]:
                try:
                    qi = int(Decimal(str(q)))
                except (InvalidOperation, ValueError):
                    continue
                d = _num(n) if not isinstance(n, bool) else None
                if qi > 0 and d is not None:
                    vals[str(qi)] = plain(d)
            if vals:
                out[c["key"]] = vals
    return out


def apply_ai(r, extra: dict) -> None:
    """Значення від AI — у колонки, які людина (чи агент з її ✓) не вписала сама (src user / agent лишається)."""
    if not extra:
        return
    from suppliers.models import SupplierRequestRecipient as R
    x = dict(r.extra or {})
    now = timezone.now().isoformat()
    for key, v in extra.items():
        if (x.get(key) or {}).get("src") in KEPT:
            continue
        x[key] = {"v": v, "src": "ai", "at": now}
    R.objects.filter(pk=r.pk).update(extra=x)
    r.extra = x


# ── таблиця ──

def cells(cmp, req) -> dict:
    """{recipient pk: [{key, name, kind, src, qs: [{qty, text, home_text, v, best, sort}] | text}]} + найнижче по кількостях."""
    from suppliers.services.rfq_compare import money
    cols = columns(req)
    out = {}
    if not cols:
        return out
    qtys = cmp["qtys"]
    for row in cmp["rows"]:
        r, rate = row["recipient"], row.get("rate")
        cur = (r.currency or cmp["home"]).upper()
        items = []
        for c in cols:
            e = (r.extra or {}).get(c["key"]) or {}
            if c["kind"] == "text":
                items.append({**c, "src": e.get("src", ""), "text": e.get("v") if isinstance(e.get("v"), str) else ""})
                continue
            vals = e.get("v") if isinstance(e.get("v"), dict) else {}
            qs = []
            for q in qtys:
                d = _num(vals.get(str(q)))
                if d is None:
                    qs.append({"qty": q, "text": "", "sort": ""})
                    continue
                home = (d * rate).quantize(CENT, ROUND_HALF_UP) if rate is not None else None
                qs.append({"qty": q, "text": f"{money(d)} {cur}", "raw": plain(d),
                           "home_text": f"{money(home)} {cmp['home']}" if home is not None and cur != cmp["home"].upper()
                           else "", "v": float(home) if home is not None else None,
                           "main": f"{money(home)}" if home is not None else f"{money(d)} {cur}",
                           "sort": f"{float(home if home is not None else d):.6f}"})
            items.append({**c, "src": e.get("src", ""), "qs": qs, "cur": cur})
        out[r.pk] = items
    # найнижче по кожній колонці й кількості — серед тих, у кого є курс
    for ci, c in enumerate(cols):
        if c["kind"] != "price":
            continue
        for qi in range(len(qtys)):
            vals = [it[ci]["qs"][qi]["v"] for it in out.values() if it[ci]["qs"][qi].get("v") is not None]
            if len(vals) > 1:
                low = min(vals)
                for it in out.values():
                    q = it[ci]["qs"][qi]
                    q["best"] = q.get("v") == low
    return out
