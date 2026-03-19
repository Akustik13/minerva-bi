"""
sales/utils.py — shared helpers for sales app
"""

# Canonical courier names: keyword (lowercase) → canonical label
_COURIER_MAP = [
    # DHL variants first (most common)
    ("dhl",        "DHL"),
    # UPS
    ("ups",        "UPS"),
    # FedEx
    ("fedex",      "FedEx"),
    ("fed ex",     "FedEx"),
    # Nova Poshta
    ("nova poshta","Nova Poshta"),
    ("nova post",  "Nova Poshta"),
    ("novapost",   "Nova Poshta"),
    ("нова пошта", "Nova Poshta"),
    ("нп",         "Nova Poshta"),
    # DPD
    ("dpd",        "DPD"),
    # GLS
    ("gls",        "GLS"),
    # TNT
    ("tnt",        "TNT"),
    # Hermes / Evri
    ("hermes",     "Hermes"),
    ("evri",       "Hermes"),
    # DB Schenker
    ("schenker",   "DB Schenker"),
    # PostAG / Österreichische Post
    ("post ag",    "Post AG"),
    ("österreichische post", "Post AG"),
    # Jumingo (internal label)
    ("jumingo",    "Jumingo"),
]


def normalize_courier(value: str) -> str:
    """
    Normalise a courier name to a canonical label.

    Examples:
        "dhl express"  → "DHL"
        "DHL Paket"    → "DHL"
        "ups"          → "UPS"
        "  FedEx  "    → "FedEx"
        ""             → ""
    """
    if not value:
        return ""
    v = value.strip()
    vl = v.lower()
    for keyword, canonical in _COURIER_MAP:
        if keyword in vl:
            return canonical
    # Unknown courier: return title-cased original
    return v
