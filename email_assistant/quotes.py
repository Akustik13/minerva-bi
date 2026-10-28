"""Текст листа без процитованої історії — спільне для пошти («📋 Підготувати») і Angebote (offers.detection)."""
import re

# Початок процитованої історії; «Am 01.10.2026 um 10:00 schrieb Max Muster <max@x.de>:» — ім'я після «schrieb»
QUOTE_START_RE = re.compile(
    r"^\s*(?:>|-{2,}\s*(?:original|ursprüngliche|forwarded|weitergeleitete)"
    r"|(?:on|am)\s.{5,80}(?:wrote|schrieb)(?:\s[^\n]{0,120}?)?\s*:"
    r"|von:\s|from:\s|gesendet:\s|sent:\s|від:\s|от:\s)", re.IGNORECASE)


# Пересланий лист: «-------- Ursprüngliche Nachricht --------» / «---------- Forwarded message ---------» і заголовки
FORWARD_RE = re.compile(r"^\s*-{2,}\s*(?:original\s+message|ursprüngliche\s+nachricht|forwarded\s+message|"
                        r"weitergeleitete\s+nachricht|пересланий\s+лист|пересланное\s+сообщение)", re.IGNORECASE)
HEADER_RE = re.compile(r"^\s*\*?(von|from|datum|date|gesendet|sent|an|to|cc|betreff|subject|від|от|дата|кому|тема)"
                       r"\*?\s*:\s*(.*)$", re.IGNORECASE)
SENDER_HEADERS = ("von", "from", "від", "от")
SUBJECT_HEADERS = ("betreff", "subject", "тема")
ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def forwarded(body):
    """Перший пересланий лист у тексті (FW / WG від колеги): {from, name, subject, text} — відправник із «Von: /
    From:» і його текст без власної історії; немає блоку з адресою відправника — None."""
    lines = (body or "").splitlines()
    i = next((n for n, ln in enumerate(lines) if FORWARD_RE.match(ln) or (
        HEADER_RE.match(ln) and HEADER_RE.match(ln).group(1).lower() in SENDER_HEADERS)), None)
    if i is None:
        return None
    if FORWARD_RE.match(lines[i]):
        i += 1
    headers, end = {}, i
    while i < len(lines):   # заголовки, між якими можуть бути порожні рядки; перший інший рядок — текст листа
        m = HEADER_RE.match(lines[i])
        if m:
            headers.setdefault(m.group(1).lower(), m.group(2).strip())
            end = i + 1
        elif lines[i].strip():
            break
        i += 1
    i = end
    sender = next((headers[h] for h in SENDER_HEADERS if h in headers), "")
    address = ADDRESS_RE.search(sender)
    if not address:
        return None
    return {"from": address.group(0).lower(), "name": sender.split("<")[0].strip(' "\''),
            "subject": next((headers[h] for h in SUBJECT_HEADERS if h in headers), ""),
            "text": new_text("\n".join(lines[i:]))}


SIGN_OFF_RE = re.compile(r"^\s*(?:best\s+regards?|kind\s+regards|warm\s+regards|regards|sincerely|cheers|"
                         r"mit\s+freundlichen\s+grüßen|freundliche\s+grüße|viele\s+grüße|beste\s+grüße|"
                         r"з\s+повагою|с\s+уважением)\b[\s,.!]*$", re.IGNORECASE)
CONTACT_LINE_RE = re.compile(r"^\s*(?:tel|phone|fon|mob|mobile|cell|fax|e-?mail|mail|web|www|http|skype|whatsapp)\b",
                             re.IGNORECASE)


def signature(text) -> dict:
    """Підпис відправника внизу листа: рядки після «--» або після прощання («Best regards,» окремим рядком).
    → {lines: адреса (без телефонів / email / сайтів), name: ім'я з першого рядка («Denys R. - supply manager …»)}."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    start = next((i + 1 for i in range(len(lines) - 1, -1, -1) if lines[i] in ("--", "—", "-- ")), None)
    if start is None:
        start = next((i + 1 for i in range(len(lines) - 1, -1, -1) if SIGN_OFF_RE.match(lines[i])), None)
    if start is None:
        return {"lines": [], "name": ""}
    out = [ln for ln in lines[start:] if ln and not SIGN_OFF_RE.match(ln) and len(ln) <= 120][:8]
    address = [ln for ln in out if not CONTACT_LINE_RE.match(ln) and "@" not in ln]
    name = re.split(r"\s+[-–|,]\s+", address[0])[0] if address else ""
    if not 2 < len(name) <= 60:
        return {"lines": address, "name": ""}
    return {"lines": address[1:] or address, "name": name}   # ім'я — окремо (контактна особа), далі — фірма й адреса


def new_text(body) -> str:
    """Текст листа без процитованої історії (відповідь клієнта зверху, далі — наш попередній лист)."""
    out = []
    for line in (body or "").splitlines():
        if QUOTE_START_RE.match(line):
            break
        out.append(line)
    return "\n".join(out)[:20000]
