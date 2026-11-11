"""Тексти згоди на запис (§ 201 StGB, ст. 6 і 13 DSGVO; чернетки — meetings/docs/consent-drafts.md).

- Що сказати вголос перед ▶ — meetings/consent/{de,en,uk}.txt: питання («ask»), рядок «---», фраза після старту
  запису («after»: «Danke, die Aufnahme läuft jetzt.»). Без ремарок у квадратних дужках — їх показує вікно запису
  окремо мовою інтерфейсу. «⚙️ Налаштування нарад» можуть замінити текст мови (порожньо — вбудований).
- Примітка для запрошення — meetings/consent/invite_{de,en,uk}.txt з числами строків наради й посиланням на
  інформацію за ст. 13 (MeetingSettings.privacy_url; немає — рядка з посиланням немає).

Тексти лежать у .txt, а не в Python: збирач написів інтерфейсу (core/ui_text) їх не бачить, тож браузер їх не
«перекладає» — що прочитали вголос, те й лежить у протоколі (Meeting.consent_text). На сторінках — лише translate="no".
"""
from functools import lru_cache
from pathlib import Path

LANGS = ("de", "en", "uk")
DEFAULT_LANG = "de"
SEPARATOR = "---"
FOLDER = Path(__file__).resolve().parent / "consent"


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    try:
        return (FOLDER / f"{name}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def lang_of(lang) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def _split(text: str) -> tuple:
    lines = (text or "").replace("\r\n", "\n").strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip() == SEPARATOR:
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i + 1:]).strip()
    return "\n".join(lines).strip(), ""


def _settings(settings):
    if settings is not None:
        return settings
    try:
        from .models import MeetingSettings
        return MeetingSettings.load()
    except Exception:  # noqa: BLE001 — немає таблиці (міграції) — вбудовані тексти
        return None


def parts_for(lang, settings=None) -> dict:
    """{"ask": що спитати до ▶, "after": що сказати, щойно запис почався}."""
    lang = lang_of(lang)
    ask, after = _split(_read(lang))
    s = _settings(settings)
    own = (getattr(s, f"consent_text_{lang}", "") or "").strip() if s is not None else ""
    if own:
        own_ask, own_after = _split(own)
        ask, after = own_ask or ask, own_after or after
    return {"ask": ask, "after": after}


def text_for(lang, settings=None) -> str:
    """Увесь текст однією мовою — так він лягає в протокол (Meeting.consent_text)."""
    p = parts_for(lang, settings)
    return "\n\n".join(x for x in (p["ask"], p["after"]) if x)


def texts(settings=None) -> dict:
    """Усі мови — для вікна запису (перемикач de / en / uk): {lang: {"ask", "after"}}."""
    s = _settings(settings)
    return {lang: parts_for(lang, s) for lang in LANGS}


# ── Примітка для запрошення ─────────────────────────────────────────────────

def _plural_uk(n, one, few, many) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def duration_text(days, lang, months=True) -> str:
    """Строк словами для примітки: 365 → «12 Monaten» / «12 months» / «12 місяців»; не ціле число місяців (або
    months=False — аудіо: «30 Tagen», як у чернетці) — дні."""
    days = max(int(days or 0), 1)
    months = (days // 365 * 12 if days % 365 == 0 else (days // 30 if days % 30 == 0 else 0)) if months else 0
    lang = lang_of(lang)
    if months:
        if lang == "de":
            return "einem Monat" if months == 1 else f"{months} Monaten"
        if lang == "en":
            return "1 month" if months == 1 else f"{months} months"
        return f"{months} {_plural_uk(months, 'місяць', 'місяці', 'місяців')}"
    if lang == "de":
        return "einem Tag" if days == 1 else f"{days} Tagen"
    if lang == "en":
        return "1 day" if days == 1 else f"{days} days"
    return f"{days} {_plural_uk(days, 'день', 'дні', 'днів')}"


def invitation_note(lang, *, audio_days=30, transcript_days=365, privacy_url="") -> str:
    """Примітка в запрошення (Teams / Outlook) з числами цієї наради; без посилання — без рядка про ст. 13."""
    lang = lang_of(lang)
    text = (_read(f"invite_{lang}")
            .replace("{audio}", duration_text(audio_days, lang, months=False))
            .replace("{transcript}", duration_text(transcript_days, lang)))
    lines = []
    for line in text.split("\n"):
        if "{privacy}" in line:
            if not privacy_url:
                continue
            line = line.replace("{privacy}", privacy_url)
        lines.append(line)
    return "\n".join(lines).strip()


def invitation_notes(meeting=None, settings=None) -> dict:
    """{lang: примітка} — числа строків з наради (знімок при створенні), інакше з налаштувань."""
    s = _settings(settings)
    audio = getattr(meeting, "audio_days", None) or getattr(s, "audio_retention_days", 30) or 30
    transcript = getattr(meeting, "retention_days", None) or getattr(s, "transcript_retention_days", 365) or 365
    privacy = (getattr(s, "privacy_url", "") or "") if s is not None else ""
    return {lang: invitation_note(lang, audio_days=audio, transcript_days=transcript, privacy_url=privacy)
            for lang in LANGS}
