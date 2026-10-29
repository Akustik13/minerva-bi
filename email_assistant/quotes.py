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


def _forward_blocks(lines):
    """Переслані листи в тексті по черзі (зовнішній — першим): {from, name, subject, text}."""
    start = 0
    while True:
        i = next((n for n in range(start, len(lines)) if FORWARD_RE.match(lines[n]) or (
            HEADER_RE.match(lines[n]) and HEADER_RE.match(lines[n]).group(1).lower() in SENDER_HEADERS)), None)
        if i is None:
            return
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
        start = max(end, i) if end > start else i + 1   # далі — наступний пересланий лист усередині
        sender = next((headers[h] for h in SENDER_HEADERS if h in headers), "")
        address = ADDRESS_RE.search(sender)
        if address:
            yield {"from": address.group(0).lower(), "name": sender.split("<")[0].strip(' "\''),
                   "subject": next((headers[h] for h in SUBJECT_HEADERS if h in headers), ""),
                   "text": new_text("\n".join(lines[end:]))}


def forwarded(body, skip=None):
    """Пересланий лист у тексті (FW / WG від колеги): {from, name, subject, text} — відправник із «Von: /
    From:» і його текст без власної історії; немає блоку з адресою відправника — None.
    skip(адреса) → True — пропустити (свої адреси: колега переслав власний FW — «WG: FW: …»), береться
    наступний пересланий лист усередині."""
    for block in _forward_blocks((body or "").splitlines()):
        if skip is None or not skip(block["from"]):
            return block
    return None


SIGN_OFF_RE = re.compile(r"^\s*(?:best\s+regards?|kind\s+regards|warm\s+regards|regards|sincerely|cheers|"
                         r"mit\s+freundlichen\s+grüßen|freundliche\s+grüße|viele\s+grüße|beste\s+grüße|"
                         r"з\s+повагою|с\s+уважением)\b[\s,.!]*$", re.IGNORECASE)
CONTACT_LINE_RE = re.compile(r"^\s*(?:tel|phone|fon|mob|mobile|cell|fax|e-?mail|mail|web|www|http|skype|whatsapp)\b",
                             re.IGNORECASE)
PHONE_RE = re.compile(r"(?:^\s*(?:tel|phone|fon|mob|mobile|cell|whatsapp)\b\.?[\s:.]*|^\s*)(\+?\d[\d\s()./-]{6,}\d)\s*$",
                      re.IGNORECASE)


def signature(text) -> dict:
    """Підпис відправника внизу листа: рядки після «--» або після прощання («Best regards,» окремим рядком).
    → {lines: адреса (без телефонів / email / сайтів), name: ім'я з першого рядка («Denys R. - supply manager …»),
    phone: перший телефон (не факс)}."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    start = next((i + 1 for i in range(len(lines) - 1, -1, -1) if lines[i] in ("--", "—", "-- ")), None)
    if start is None:
        start = next((i + 1 for i in range(len(lines) - 1, -1, -1) if SIGN_OFF_RE.match(lines[i])), None)
    if start is None:
        return {"lines": [], "name": "", "phone": ""}
    out = [ln for ln in lines[start:] if ln and not SIGN_OFF_RE.match(ln) and len(ln) <= 120][:8]
    phone = next((m.group(1).strip() for ln in out if not re.match(r"^\s*fax", ln, re.I)
                  for m in [PHONE_RE.match(ln)] if m), "")
    address = [ln for ln in out if not CONTACT_LINE_RE.match(ln) and "@" not in ln and not PHONE_RE.match(ln)]
    name = re.split(r"\s+[-–|,]\s+", address[0])[0] if address else ""
    if not 2 < len(name) <= 60:
        return {"lines": address, "name": "", "phone": phone}
    # ім'я — окремо (контактна особа), далі — фірма й адреса
    return {"lines": address[1:] or address, "name": name, "phone": phone}


def new_text(body) -> str:
    """Текст листа без процитованої історії (відповідь клієнта зверху, далі — наш попередній лист)."""
    out = []
    for line in (body or "").splitlines():
        if QUOTE_START_RE.match(line):
            break
        out.append(line)
    return "\n".join(out)[:20000]


# Прощання й підпис: короткий рядок «Mit freundlichen Grüßen / Best regards», «-- », «=====»
SIGNOFF_RE = re.compile(
    r"^\s*(?:(?:mit\s+freundlichen|freundliche|viele|beste|liebe)\s+gr(?:ü|ue)(?:ß|ss)(?:en|e)|"
    r"best\s+regards?|kind\s+regards|warm\s+regards|regards|sincerely|cheers|з\s+повагою|с\s+уважением)"
    r"(?:[\s,.!/:-].{0,50})?$|^\s*(?:--|={4,}|_{4,})\s*$", re.IGNORECASE)
_REPLY_HEADER_RE = re.compile(r"(?:schrieb|wrote|geschrieben|написав|написал)\s*:\s*$", re.IGNORECASE)
_LINKS_RE = re.compile(r"<(?:mailto:|https?://)[^>]{0,300}>|\[(?:https?://|mailto:)[^\]]{0,300}\]")


def strip_signature(text) -> str:
    """Текст до прощання й підпису (перший рядок підписом не вважається) — для пошукового запиту."""
    lines = (text or "").splitlines()
    for n, line in enumerate(lines[1:], 1):
        if SIGNOFF_RE.match(line):
            return "\n".join(lines[:n]).rstrip()
    return "\n".join(lines).rstrip()


def tidy(text) -> str:
    """
    Фрагмент листа для показу одним абзацом: без «>» цитування, без підписів (від прощання до наступного
    заголовка листа — «Von: …», «… schrieb:»), без посилань <mailto:…> і зайвих пробілів. Зміст цитат лишається.
    """
    out, skipping = [], False
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:>\s?)+", "", raw)
        if skipping:
            if not (FORWARD_RE.match(line) or HEADER_RE.match(line) or _REPLY_HEADER_RE.search(line)):
                continue
            skipping = False
        if out and SIGNOFF_RE.match(line):
            skipping = True
            continue
        out.append(line)
    return " ".join(_LINKS_RE.sub("", " ".join(out)).split())


def around(text, terms, size=420) -> str:
    """Шматок тексту довжиною ~size навколо першого збігу з terms (без збігу — початок)."""
    low = text.lower()
    hits = [low.find(t.lower()) for t in terms if t and low.find(t.lower()) >= 0]
    if not hits or len(text) <= size:
        return text[:size] + ("…" if len(text) > size else "")
    start = max(0, min(hits) - size // 3)
    piece = text[start:start + size]
    return ("…" if start else "") + piece + ("…" if start + size < len(text) else "")


def terms_of(query, limit=12) -> list:
    """Значущі слова запиту для підсвічування: артикули (з цифрами) першими, далі слова від 5 літер."""
    words = re.findall(r"[\w][\w./-]{2,}", query or "")
    codes = [w.strip("./-") for w in words if any(ch.isdigit() for ch in w) and len(w) >= 4]
    rest = [w for w in words if not any(ch.isdigit() for ch in w) and len(w) >= 5]
    seen, out = set(), []
    for w in codes + rest:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out[:limit]
