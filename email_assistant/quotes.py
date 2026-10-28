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
    headers = {}
    while i < len(lines):
        m = HEADER_RE.match(lines[i])
        if m:
            headers.setdefault(m.group(1).lower(), m.group(2).strip())
        elif lines[i].strip() or headers:
            break
        i += 1
    sender = next((headers[h] for h in SENDER_HEADERS if h in headers), "")
    address = ADDRESS_RE.search(sender)
    if not address:
        return None
    return {"from": address.group(0).lower(), "name": sender.split("<")[0].strip(' "\''),
            "subject": next((headers[h] for h in SUBJECT_HEADERS if h in headers), ""),
            "text": new_text("\n".join(lines[i:]))}


def new_text(body) -> str:
    """Текст листа без процитованої історії (відповідь клієнта зверху, далі — наш попередній лист)."""
    out = []
    for line in (body or "").splitlines():
        if QUOTE_START_RE.match(line):
            break
        out.append(line)
    return "\n".join(out)[:20000]
