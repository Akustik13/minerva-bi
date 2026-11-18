"""✉️ Підсумок наради листом — «📝 Підсумок наради» (meetings/ai_brief.py) і агенда з результатами одним листом.

Лист — HTML для будь-якої пошти (Outlook теж: таблиці, стилі в рядку, без класів і max-width) + той самий текст без
HTML: шапка, «коротко», 📋 агенда й результати — таблиця «Пункт агенди → Результат» (стан пункту, як його відмічено
на сторінці: ☑ — обговорили, відкритий з «що вирішили» — ще відкрито, відкритий без нього — на наступну нараду,
⏭ — не потрібно; перенесені не йдуть), домовленості, завдання (таблиця «Хто · Що · До коли»), терміни («Коли · Що»),
цифри й факти, ризики, відкрите, «що обговорили» — що саме, вибирає людина (`PARTS`, `defaults`). Посилань на
розшифровку в листі немає (вони ведуть у Minerva).

Мова листа — uk / de / en (`LANGS`): пункти агенди — збережений переклад (agenda._translated), підсумок — мовою, якою
його склали, або перекладом (`brief["tr"][мова]`, відбиток оригіналу `src`); чого бракує, за галочкою «🌐 перекласти»
перекладає Claude (core/ai_texts, бюджет AI) і запам'ятовує; переклад підсумку — у протоколі згоди (cloud, task
translate). `send` — чернетка у власній скриньці з підписом (`EmailDraft.body_html`), лист надсилає людина.
"""
from __future__ import annotations

import hashlib
import html
import json

from django.utils import timezone

from . import agenda, ai_brief
from .models import MeetingAgendaItem

LANGS = {"uk": "Українська", "de": "Deutsch", "en": "English"}
PARTS = ["about", "agenda", "agreements", "tasks", "deadlines", "facts", "open", "risks", "topics"]
TR_MODEL = "claude-haiku-5-5"

L = {
    "uk": {"subject": "Підсумок наради: {title} ({date})", "hello": "Добрий день!",
           "intro": "Нижче — підсумок нашої наради «{title}» від {date}.",
           "outro": "Якщо щось записано неточно чи пропущено — напишіть, будь ласка, у відповіді.",
           "agenda": "📋 Агенда й результати", "num": "№", "item": "Пункт агенди", "result": "Результат",
           "done": "✅ обговорили", "partly": "🟡 ще відкрито", "next": "⏭ не обговорили — на наступну нараду",
           "skipped": "— не потрібно", "who": "Хто", "what": "Що", "due": "До коли", "when": "Коли",
           "sections": {"topics": "🗂 Що обговорили", "agreements": "🤝 Про що домовились", "tasks": "✅ Завдання",
                        "deadlines": "⏰ Терміни", "facts": "ℹ️ Важливі цифри й факти", "risks": "⚠️ Ризики",
                        "open": "❓ Відкрите — ще не вирішили"}},
    "de": {"subject": "Besprechungsprotokoll: {title} ({date})", "hello": "Guten Tag,",
           "intro": "anbei die Zusammenfassung unserer Besprechung „{title}“ vom {date}.",
           "outro": "Falls etwas ungenau festgehalten ist oder fehlt, antworten Sie bitte einfach auf diese E-Mail.",
           "agenda": "📋 Tagesordnung und Ergebnisse", "num": "Nr.", "item": "Tagesordnungspunkt",
           "result": "Ergebnis", "done": "✅ besprochen", "partly": "🟡 noch offen",
           "next": "⏭ nicht besprochen — nächste Besprechung", "skipped": "— entfällt", "who": "Wer", "what": "Was",
           "due": "Bis wann", "when": "Wann",
           "sections": {"topics": "🗂 Besprochene Themen", "agreements": "🤝 Vereinbarungen", "tasks": "✅ Aufgaben",
                        "deadlines": "⏰ Termine", "facts": "ℹ️ Wichtige Zahlen und Fakten", "risks": "⚠️ Risiken",
                        "open": "❓ Offene Punkte"}},
    "en": {"subject": "Meeting summary: {title} ({date})", "hello": "Hello,",
           "intro": "please find below the summary of our meeting “{title}” on {date}.",
           "outro": "If anything is inaccurate or missing, just reply to this e-mail.",
           "agenda": "📋 Agenda and results", "num": "No.", "item": "Agenda item", "result": "Result",
           "done": "✅ discussed", "partly": "🟡 still open", "next": "⏭ not discussed — next meeting",
           "skipped": "— not needed", "who": "Who", "what": "What", "due": "Due", "when": "When",
           "sections": {"topics": "🗂 Topics discussed", "agreements": "🤝 Agreements", "tasks": "✅ Action items",
                        "deadlines": "⏰ Dates and deadlines", "facts": "ℹ️ Key figures and facts", "risks": "⚠️ Risks",
                        "open": "❓ Open points"}},
}
STATUS_COLOR = {"done": "#1e7d32", "partly": "#a86400", "next": "#666666", "skipped": "#888888"}
BLUE, LINE, HEAD_BG, NOTE_BG = "#1f3b73", "#e3e7ee", "#eef2f8", "#f3f6fb"
FONT = "font-family:Arial,Helvetica,sans-serif;"


def _err(text):
    from .services import MeetingError
    return MeetingError(text)


def _e(text) -> str:
    return html.escape(str(text or ""))


def _date(m, lang) -> str:
    return m.day.isoformat() if lang == "en" else m.day.strftime("%d.%m.%Y")


def _hash(texts) -> str:
    return hashlib.sha1(json.dumps(texts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ── підсумок мовою листа ─────────────────────────────────────────────────────

def _brief_texts(brief) -> list:
    """Усі тексти підсумку в одному порядку (для перекладу й відбитка): about, потім text / who / due кожної тези."""
    out = [brief.get("about") or ""]
    for key in ai_brief.SECTION_KEYS:
        for it in (brief.get("sections") or {}).get(key) or []:
            out += [it.get("text") or "", it.get("who") or "", it.get("due") or ""]
    return out


def _brief_from(brief, texts) -> dict:
    """Тексти (у тому ж порядку, що _brief_texts) → {about, sections}."""
    it = iter(texts)
    about = next(it, "")
    sections = {}
    for key in ai_brief.SECTION_KEYS:
        rows = []
        for _src in (brief.get("sections") or {}).get(key) or []:
            rows.append({"text": next(it, ""), "who": next(it, ""), "due": next(it, "")})
        sections[key] = rows
    return {"about": about, "sections": sections}


def brief_in(m, lang):
    """(підсумок мовою lang | None — перекладу ще немає, мова оригіналу). Без підсумку — (None, '')."""
    _tr, brief = ai_brief._brief(m)
    if not brief:
        return None, ""
    src = brief.get("lang") or "uk"
    if src == lang:
        return _brief_from(brief, _brief_texts(brief)), src
    cached = (brief.get("tr") or {}).get(lang) or {}
    if cached.get("src") == _hash(_brief_texts(brief)) and isinstance(cached.get("texts"), list):
        return _brief_from(brief, cached["texts"]), lang
    return None, src


def translate_brief(user, m, lang) -> None:
    """Перекласти підсумок мовою lang (Claude, core/ai_texts) і запам'ятати в brief["tr"]; запис у протокол згоди."""
    from django.db import transaction

    from core import ai_texts

    from .models import Meeting, MeetingTranscript
    have, _ = brief_in(m, lang)
    if have is not None:
        return
    _tr, brief = ai_brief._brief(m)
    texts = _brief_texts(brief)
    idx = [i for i, t in enumerate(texts) if t.strip()]
    try:
        done = ai_texts.translate_list(user, [texts[i] for i in idx], lang, TR_MODEL,
                                       about="These are the summary points of a business meeting (minutes): what was "
                                             "discussed, agreed, tasks, dates. Keep names, codes and numbers.")
    except ai_texts.TranslateError as e:
        raise _err(f"{e} — або надішліть підсумок без перекладу") from None
    out = list(texts)
    for i, value in zip(idx, done):
        out[i] = " ".join(str(value or "").split())[:ai_brief.ITEM_MAX] or texts[i]
    with transaction.atomic():
        locked = Meeting.objects.select_for_update(of=("self",)).get(pk=m.pk)
        row = MeetingTranscript.objects.select_for_update().filter(meeting=locked).first()
        if row is None or not isinstance(row.brief, dict):
            return
        fresh = dict(row.brief)
        tr = dict(fresh.get("tr") or {})
        tr[lang] = {"src": _hash(_brief_texts(fresh)), "texts": out, "at": timezone.now().isoformat()}
        fresh["tr"] = tr
        MeetingTranscript.objects.filter(meeting=locked).update(brief=fresh)
        log = list(locked.consent_log or [])
        log.append({"at": timezone.now().isoformat(), "kind": "cloud", "task": "translate", "user": user.pk,
                    "model": TR_MODEL, "where": "minerva", "provider": "anthropic"})
        Meeting.objects.filter(pk=locked.pk).update(consent_log=log, updated_at=timezone.now())
    m.__dict__.pop("_ai_brief_memo", None)


# ── агенда з результатами ────────────────────────────────────────────────────

def _state(r) -> str:
    if r.status == MeetingAgendaItem.DONE:
        return "done"
    if r.status == MeetingAgendaItem.SKIPPED:
        return "skipped"
    return "partly" if r.note else "next"


def agenda_rows(m, lang) -> list:
    """[{num, level, text, note, state}] мовою lang (збережений переклад, чого немає — оригінал)."""
    rows = agenda._live(agenda.items(m))
    _title, texts, notes = agenda._translated(m, rows, lang)
    return [{"num": r.num, "level": r.level, "text": texts.get(r.pk) or r.text, "note": notes.get(r.pk) or r.note,
             "state": _state(r)} for r in rows]


# ── що можна вибрати у формі ─────────────────────────────────────────────────

def form(user, m) -> dict | None:
    """Для форми «✉️ Надіслати підсумок листом»: кому, мови (що перекладе AI), частини листа з галочками за
    замовчуванням, скільки пропозицій звірки ще не прийнято."""
    view = ai_brief.view(m)
    if not view or view.get("empty"):
        return None
    rows = agenda._live(agenda.items(m))
    sec = {s["key"]: len(s["rows"]) for s in view["sections"]}
    parts = []
    if view.get("about"):
        parts.append({"key": "about", "label": "Коротко про нараду", "on": True})
    if rows:
        parts.append({"key": "agenda", "label": f"📋 Агенда й результати ({len(rows)})", "on": True})
    for key, title in ai_brief.SECTIONS:
        if sec.get(key):
            on = key not in ("risks", "topics") or (key == "topics" and not rows)
            parts.append({"key": key, "label": f"{title} ({sec[key]})", "on": on})
    langs = []
    for code, label in LANGS.items():
        brief, _src = brief_in(m, code)
        missing = len(agenda._pieces(m, rows, code)) + (0 if brief is not None else 1)
        langs.append({"code": code, "label": label, "missing": missing, "ready": not missing})
    check = ai_brief.hints(m, agenda.items(m))
    default = view.get("lang") if view.get("lang") in LANGS else "uk"
    if m.agenda_lang in LANGS:
        default = m.agenda_lang
    return {"people": agenda.people(m, user), "langs": langs, "lang": default, "parts": parts,
            "waiting": sum(1 for h in check.values() if h.get("ok"))}


# ── лист ─────────────────────────────────────────────────────────────────────

def _h(title) -> str:
    return (f'<h3 style="{FONT}font-size:15px;margin:18px 0 6px;color:{BLUE}">{_e(title)}</h3>')


def _table(head, rows, widths=()) -> str:
    widths = list(widths) + [""] * len(head)
    th = []
    for h, w in zip(head, widths):
        width = f' width="{w}"' if w else ""
        th.append(f'<th align="left" valign="bottom"{width} style="{FONT}font-size:13px;padding:6px 8px;'
                  f'border-bottom:1px solid #c9d3e3;color:#333">{_e(h)}</th>')
    th = "".join(th)
    out = [f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse">',
           f'<tr bgcolor="{HEAD_BG}" style="background:{HEAD_BG}">{th}</tr>']
    for cells in rows:
        out.append('<tr bgcolor="#ffffff" style="background:#ffffff">' + "".join(
            f'<td valign="top" bgcolor="#ffffff" style="{FONT}font-size:13.5px;padding:6px 8px;background:#ffffff;'
            f'border-bottom:1px solid {LINE};color:#222">{c}</td>' for c in cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _list(rows) -> str:
    items = "".join(f'<li style="margin:2px 0">{r}</li>' for r in rows)
    return f'<ul style="{FONT}font-size:14px;margin:4px 0 0;padding-left:22px;color:#222">{items}</ul>'


def compose(user, m, lang, parts, ai=False) -> dict:
    """Тема, HTML і текст листа мовою lang; parts — що включити (PARTS); ai — перекласти, чого бракує."""
    if lang not in LANGS:
        raise _err("Виберіть мову листа")
    parts = [p for p in PARTS if p in set(parts or [])]
    if not parts:
        raise _err("Виберіть, що буде в листі")
    view = ai_brief.view(m)
    if not view or view.get("empty"):
        raise _err("Підсумку ще немає — спершу «📝 Скласти підсумок і звірити з агендою»")
    rows_all = agenda._live(agenda.items(m))
    if ai:
        if "agenda" in parts and rows_all:
            pieces = agenda._pieces(m, rows_all, lang)
            if pieces:
                agenda._store(m, lang, pieces, agenda.translate(
                    user, [p[2] for p in pieces], lang, hint=" — або надішліть лист без перекладу"))
        if any(p != "agenda" for p in parts):
            translate_brief(user, m, lang)
    brief, _src = brief_in(m, lang)
    if brief is None:                                   # без перекладу — як склали
        _t, raw = ai_brief._brief(m)
        brief = _brief_from(raw, _brief_texts(raw))
    t = L[lang]
    title, _texts, _notes = agenda._translated(m, rows_all, lang)
    title = title or m.title
    date = _date(m, lang)
    html_parts = [f'<div style="{FONT}font-size:14px;line-height:1.45;color:#222;background:#ffffff;'
                  f'padding:12px 14px">',
                  f"<p>{_e(t['hello'])}</p>", f"<p>{_e(t['intro'].format(title=title, date=date))}</p>"]
    text = [t["hello"], "", t["intro"].format(title=title, date=date)]
    if "about" in parts and brief["about"]:
        html_parts.append(f'<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                          f'<td bgcolor="{NOTE_BG}" style="{FONT}font-size:14px;padding:8px 12px;'
                          f'border-left:3px solid #3b6fd8;background:{NOTE_BG};color:#222">{_e(brief["about"])}</td>'
                          f"</tr></table>")
        text += ["", brief["about"]]
    if "agenda" in parts and rows_all:
        cells, lines = [], []
        for r in agenda_rows(m, lang):
            pad = "padding-left:16px;" if r["level"] else ""
            state = (f'<span style="color:{STATUS_COLOR[r["state"]]};font-weight:bold;white-space:nowrap">'
                     f'{_e(t[r["state"]])}</span>')
            result = state + (f"<br>{_e(r['note'])}" if r["note"] else "")
            cells.append([f'<span style="color:#777">{_e(r["num"])}.</span>',
                          f'<div style="{pad}{"" if r["level"] else "font-weight:bold"}">{_e(r["text"])}</div>',
                          result])
            ind = "    " if r["level"] else ""
            lines.append(f"{ind}{r['num']}. {r['text']} — {t[r['state']]}" + (f": {r['note']}" if r["note"] else ""))
        html_parts += [_h(t["agenda"]), _table([t["num"], t["item"], t["result"]], cells, ("36", "42%"))]
        text += ["", t["agenda"]] + lines
    for key in ("agreements", "tasks", "deadlines", "facts", "open", "risks", "topics"):
        items = brief["sections"].get(key) or []
        if key not in parts or not items:
            continue
        heading = t["sections"][key]
        html_parts.append(_h(heading))
        text += ["", heading]
        if key == "tasks":
            html_parts.append(_table([t["who"], t["what"], t["due"]],
                                     [[_e(i["who"]) or "—", _e(i["text"]), _e(i["due"]) or "—"] for i in items],
                                     ("22%", "", "20%")))
            text += [f"• {i['text']}" + (f" ({' · '.join(x for x in (i['who'], i['due']) if x)})"
                                         if i["who"] or i["due"] else "") for i in items]
        elif key == "deadlines":
            html_parts.append(_table([t["when"], t["what"]],
                                     [[f"<b>{_e(i['due'])}</b>" if i["due"] else "—", _e(i["text"])] for i in items],
                                     ("28%",)))
            text += [f"• {i['due'] + ': ' if i['due'] else ''}{i['text']}" for i in items]
        else:
            html_parts.append(_list([_e(i["text"]) + (f' <span style="color:#666">({_e(i["who"])})</span>'
                                                      if i["who"] else "") for i in items]))
            text += [f"• {i['text']}" + (f" ({i['who']})" if i["who"] else "") for i in items]
    html_parts += [f"<p style=\"margin-top:18px\">{_e(t['outro'])}</p>", "</div>"]
    text += ["", t["outro"]]
    return {"subject": t["subject"].format(title=title, date=date)[:500], "html": "".join(html_parts),
            "text": "\n".join(text)}


def send(user, m, to, lang, parts, ai=False) -> str:
    """Чернетка листа з підсумком у власній скриньці (з підписом) → адреса форми листа (надсилає людина)."""
    from email_assistant.models import EmailAccount, EmailDraft
    from email_assistant.views import _get_signature
    agenda._owner(user, m)
    account = EmailAccount.objects.filter(user=user, is_active=True).order_by("-is_primary").first()
    if account is None:
        raise _err("У Minerva немає вашої поштової скриньки — скопіюйте підсумок текстом («📋 Копіювати текстом»)")
    emails = agenda.clean_emails(to)
    mail = compose(user, m, lang, parts, ai=ai)
    body_html = mail["html"]
    sig = _get_signature(account)
    if sig:
        body_html += ('<br><div class="em-sig-in" style="color:#666;font-size:13px;margin-top:4px">--&nbsp;<br>'
                      f"{sig}</div>")
    draft = EmailDraft.objects.create(account=account, to_emails=emails, subject=mail["subject"], body=mail["text"],
                                      body_html=body_html, source=EmailDraft.SOURCE_MANUAL)
    return f"/email/compose/?draft={draft.pk}"
