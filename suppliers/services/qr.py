"""
suppliers/services/qr.py — 🔳 QR-код з візитки: текст коду (розшифровує браузер, jsQR) → поля постачальника, без AI.

  vCard (BEGIN:VCARD, 2.1 / 3.0 / 4.0, quoted-printable), MeCard (MECARD:…), посилання (сайт; wa.me — WhatsApp),
  mailto:, tel:. Дані з QR — точні, тому при злитті з тим, що прочитав AI, вони головні (merge).
  Текст коду — дані з чужої візитки: лише розбирається й показується екранованим.
"""
import quopri
import re

TEXT_MAX = 4000
SEPARATOR = "\n\n----\n\n"          # лицьовий і зворотний бік у SupplierMedia.qr_text
FIELDS = ("name", "website", "email", "phone", "addr_street", "addr_zip", "addr_city", "addr_country",
          "contact_name", "contact_position", "contact_email", "contact_phone", "contact_mobile", "notes")
LABELS = {"name": "фірма", "website": "сайт", "email": "email", "phone": "телефон", "addr_street": "вулиця",
          "addr_zip": "індекс", "addr_city": "місто", "addr_country": "країна", "contact_name": "ім'я",
          "contact_position": "посада", "contact_email": "email контакту", "contact_phone": "телефон контакту",
          "contact_mobile": "мобільний", "notes": "примітка"}
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def clean_text(value) -> str:
    """Текст QR з браузера: без керівних символів, не довше TEXT_MAX."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).strip()
    return text[:TEXT_MAX]


def join(*texts) -> str:
    return SEPARATOR.join(t for t in (clean_text(x) for x in texts) if t)[:TEXT_MAX * 2]


def _country(value) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return value.upper()
    try:
        from offers.pricing import _country_in_text
        return _country_in_text(value) or ""
    except Exception:  # noqa: BLE001
        return ""


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";")
            .replace("\\:", ":").replace("\\\\", "\\")).strip()


def _split(value: str, sep=";") -> list:
    """Поділ за `sep`, крім екранованого «\\;»."""
    parts, cur, esc = [], "", False
    for ch in value:
        if esc:
            cur += "\\" + ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == sep:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur + ("\\" if esc else ""))
    return parts


def _website(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url[:200] if re.match(r"https?://[\w.-]+\.[a-z]{2,}", url, re.I) else ""


def _vcard(text: str) -> dict:
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"=\n", "", raw) if "QUOTED-PRINTABLE" in raw.upper() else raw   # м'які переноси QP
    lines = []
    for ln in raw.split("\n"):
        if ln[:1] in (" ", "\t") and lines:      # продовження попереднього рядка
            lines[-1] += ln[1:]
        elif ln.strip():
            lines.append(ln)
    out, tels = {}, []
    for ln in lines:
        if ":" not in ln:
            continue
        head, value = ln.split(":", 1)
        parts = head.split(";")
        name = parts[0].split(".")[-1].upper()           # «item1.TEL» → TEL
        params = ";".join(parts[1:]).upper()
        if "QUOTED-PRINTABLE" in params:
            charset = re.search(r"CHARSET=([\w-]+)", params)
            try:
                value = quopri.decodestring(value.encode("latin-1", "ignore")).decode(
                    charset.group(1) if charset else "utf-8", "replace")
            except LookupError:
                value = quopri.decodestring(value.encode("latin-1", "ignore")).decode("utf-8", "replace")
        if name == "FN":
            out.setdefault("contact_name", _unescape(value))
        elif name == "N" and "contact_name" not in out:
            comp = [_unescape(x) for x in _split(value)] + ["", "", ""]
            full = " ".join(x for x in (comp[3], comp[1], comp[0]) if x)    # префікс, ім'я, прізвище
            if full:
                out["n_name"] = full
        elif name == "ORG":
            out.setdefault("name", _unescape(_split(value)[0]))
        elif name in ("TITLE", "ROLE"):
            out.setdefault("contact_position", _unescape(value))
        elif name == "TEL":
            tels.append((params, _unescape(value).replace("tel:", "")))
        elif name == "EMAIL":
            m = _EMAIL_RE.search(value)
            if m:
                out.setdefault("contact_email", m.group(0).lower())
        elif name == "URL":
            site = _website(_unescape(value))
            if site:
                out.setdefault("website", site)
        elif name == "ADR":
            comp = [_unescape(x) for x in _split(value)] + [""] * 7
            out.setdefault("addr_street", " ".join(x for x in (comp[1], comp[2]) if x))
            out.setdefault("addr_city", comp[3])
            out.setdefault("addr_zip", comp[5])
            out.setdefault("addr_country", _country(comp[6]))
        elif name == "NOTE":
            out.setdefault("notes", _unescape(value))
    if "contact_name" not in out and out.get("n_name"):
        out["contact_name"] = out["n_name"]
    out.pop("n_name", None)
    for params, number in tels:
        if any(k in params for k in ("CELL", "MOBILE", "IPHONE")):
            out.setdefault("contact_mobile", number)
        elif "FAX" not in params:
            out.setdefault("contact_phone", number)
    return out


def _mecard(text: str) -> dict:
    body = text.split(":", 1)[1] if ":" in text else ""
    out = {}
    for item in _split(body):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        key, value = key.strip().upper(), _unescape(value)
        if not value:
            continue
        if key == "N":
            last, _, first = value.partition(",")
            out.setdefault("contact_name", " ".join(x.strip() for x in (first, last) if x.strip()))
        elif key == "ORG":
            out.setdefault("name", value)
        elif key in ("TITLE", "ROLE"):
            out.setdefault("contact_position", value)
        elif key == "TEL":
            out.setdefault("contact_phone", value)
        elif key in ("TEL-AV", "CELL"):
            out.setdefault("contact_mobile", value)
        elif key == "EMAIL":
            m = _EMAIL_RE.search(value)
            if m:
                out.setdefault("contact_email", m.group(0).lower())
        elif key == "URL":
            site = _website(value)
            if site:
                out.setdefault("website", site)
        elif key == "ADR":
            out.setdefault("addr_street", value.replace("\n", ", ")[:300])
            out.setdefault("addr_country", _country(value.split(",")[-1]))
        elif key in ("NOTE", "MEMO"):
            out.setdefault("notes", value)
    return out


def parse(text) -> dict:
    """Текст одного QR → поля постачальника (лише знайдені, непорожні) + kind (vcard / mecard / url / …)."""
    t = clean_text(text)
    low = t.lower()
    if low.startswith("begin:vcard"):
        out, kind = _vcard(t), "vcard"
    elif low.startswith("mecard:"):
        out, kind = _mecard(t), "mecard"
    elif low.startswith("mailto:"):
        m = _EMAIL_RE.search(t)
        out, kind = ({"contact_email": m.group(0).lower()} if m else {}), "email"
    elif low.startswith("tel:"):
        out, kind = {"contact_phone": t[4:].strip()}, "phone"
    elif re.match(r"(https?://)?(wa\.me|api\.whatsapp\.com)/", low):
        number = re.search(r"(\d{7,15})", t)
        out, kind = ({"contact_mobile": "+" + number.group(1)} if number else {}), "whatsapp"
    elif re.match(r"(https?://|www\.)", low):
        out, kind = {"website": _website(t.split()[0])}, "url"
    else:
        return {}
    if out.get("contact_email") and not out.get("email"):
        out["email"] = out["contact_email"]          # візитка людини — і email фірми, як у листі
    if out.get("contact_phone") and not out.get("phone"):
        out["phone"] = out["contact_phone"]
    out = {k: str(v).strip()[:300] for k, v in out.items() if k in FIELDS and str(v or "").strip()}
    if out:
        out["kind"] = kind
    return out


def parse_all(qr_text) -> dict:
    """SupplierMedia.qr_text (лицьовий і зворотний бік) → одне злиття полів, перший бік головний."""
    out = {}
    for part in str(qr_text or "").split(SEPARATOR):
        for k, v in parse(part).items():
            out.setdefault(k, v)
    return out


def merge(qr_fields: dict, other: dict) -> dict:
    """Те, що прочитав AI / Minerva, + QR поверх (QR точніший). from_qr — які поля взято з QR."""
    out = dict(other or {})
    keys = [k for k in FIELDS if (qr_fields or {}).get(k)]
    for k in keys:
        out[k] = qr_fields[k]
    if keys:
        out["from_qr"] = keys
    return out


def labels(keys) -> str:
    return ", ".join(LABELS.get(k, k) for k in keys or ())
