"""
config/country_utils.py — Централізований довідник кодів країн ISO 3166-1.

Публічне API:
    normalize_to_iso2(code)  — будь-який рядок → ISO-2 (або '' якщо невідомо)
    to_iso3(iso2)            — ISO-2 → ISO-3 (або вихідний рядок)
    display_country(iso2)    — ISO-2 → формат згідно SystemSettings.country_code_format
    country_flag_html(iso2)  — HTML з прапором + кодом (для admin list_display)
"""

# ── ISO-2 → ISO-3 ─────────────────────────────────────────────────────────────

ISO2_TO_ISO3 = {
    "AD": "AND", "AE": "ARE", "AF": "AFG", "AL": "ALB", "AM": "ARM",
    "AO": "AGO", "AR": "ARG", "AT": "AUT", "AU": "AUS", "AZ": "AZE",
    "BA": "BIH", "BD": "BGD", "BE": "BEL", "BF": "BFA", "BG": "BGR",
    "BH": "BHR", "BI": "BDI", "BJ": "BEN", "BN": "BRN", "BO": "BOL",
    "BR": "BRA", "BS": "BHS", "BT": "BTN", "BW": "BWA", "BY": "BLR",
    "BZ": "BLZ", "CA": "CAN", "CD": "COD", "CF": "CAF", "CG": "COG",
    "CH": "CHE", "CI": "CIV", "CL": "CHL", "CM": "CMR", "CN": "CHN",
    "CO": "COL", "CR": "CRI", "CU": "CUB", "CV": "CPV", "CY": "CYP",
    "CZ": "CZE", "DE": "DEU", "DJ": "DJI", "DK": "DNK", "DO": "DOM",
    "DZ": "DZA", "EC": "ECU", "EE": "EST", "EG": "EGY", "ER": "ERI",
    "ES": "ESP", "ET": "ETH", "FI": "FIN", "FJ": "FJI", "FR": "FRA",
    "GA": "GAB", "GB": "GBR", "GE": "GEO", "GH": "GHA", "GM": "GMB",
    "GN": "GIN", "GQ": "GNQ", "GR": "GRC", "GT": "GTM", "GW": "GNB",
    "GY": "GUY", "HN": "HND", "HR": "HRV", "HT": "HTI", "HU": "HUN",
    "ID": "IDN", "IE": "IRL", "IL": "ISR", "IN": "IND", "IQ": "IRQ",
    "IR": "IRN", "IS": "ISL", "IT": "ITA", "JM": "JAM", "JO": "JOR",
    "JP": "JPN", "KE": "KEN", "KG": "KGZ", "KH": "KHM", "KM": "COM",
    "KN": "KNA", "KP": "PRK", "KR": "KOR", "KW": "KWT", "KZ": "KAZ",
    "LA": "LAO", "LB": "LBN", "LC": "LCA", "LI": "LIE", "LK": "LKA",
    "LR": "LBR", "LS": "LSO", "LT": "LTU", "LU": "LUX", "LV": "LVA",
    "LY": "LBY", "MA": "MAR", "MC": "MCO", "MD": "MDA", "ME": "MNE",
    "MG": "MDG", "MK": "MKD", "ML": "MLI", "MM": "MMR", "MN": "MNG",
    "MR": "MRT", "MT": "MLT", "MU": "MUS", "MV": "MDV", "MW": "MWI",
    "MX": "MEX", "MY": "MYS", "MZ": "MOZ", "NA": "NAM", "NE": "NER",
    "NG": "NGA", "NI": "NIC", "NL": "NLD", "NO": "NOR", "NP": "NPL",
    "NR": "NRU", "NZ": "NZL", "OM": "OMN", "PA": "PAN", "PE": "PER",
    "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL", "PT": "PRT",
    "PW": "PLW", "PY": "PRY", "QA": "QAT", "RO": "ROU", "RS": "SRB",
    "RU": "RUS", "RW": "RWA", "SA": "SAU", "SB": "SLB", "SC": "SYC",
    "SD": "SDN", "SE": "SWE", "SG": "SGP", "SI": "SVN", "SK": "SVK",
    "SL": "SLE", "SM": "SMR", "SN": "SEN", "SO": "SOM", "SR": "SUR",
    "ST": "STP", "SV": "SLV", "SY": "SYR", "SZ": "SWZ", "TD": "TCD",
    "TG": "TGO", "TH": "THA", "TJ": "TJK", "TL": "TLS", "TM": "TKM",
    "TN": "TUN", "TO": "TON", "TR": "TUR", "TT": "TTO", "TV": "TUV",
    "TZ": "TZA", "UA": "UKR", "UG": "UGA", "US": "USA", "UY": "URY",
    "UZ": "UZB", "VA": "VAT", "VC": "VCT", "VE": "VEN", "VN": "VNM",
    "VU": "VUT", "WS": "WSM", "YE": "YEM", "ZA": "ZAF", "ZM": "ZMB",
    "ZW": "ZWE",
}

ISO3_TO_ISO2 = {v: k for k, v in ISO2_TO_ISO3.items()}

# ── Аліаси: повні назви та нестандартні коди → ISO-2 ──────────────────────────

COUNTRY_ALIASES = {
    # Українська
    "УКРАЇНА": "UA", "НІМЕЧЧИНА": "DE", "ПОЛЬЩА": "PL", "АВСТРІЯ": "AT",
    "ШВЕЙЦАРІЯ": "CH", "ФРАНЦІЯ": "FR", "ВЕЛИКОБРИТАНІЯ": "GB",
    "США": "US", "НІДЕРЛАНДИ": "NL", "ЧЕХІЯ": "CZ", "СЛОВАЧЧИНА": "SK",
    "УГОРЩИНА": "HU", "РУМУНІЯ": "RO", "БОЛГАРІЯ": "BG", "ХОРВАТІЯ": "HR",
    "СЕРБІЯ": "RS", "СЛОВЕНІЯ": "SI", "ЛИТВА": "LT", "ЛАТВІЯ": "LV",
    "ЕСТОНІЯ": "EE", "ФІНЛЯНДІЯ": "FI", "ШВЕЦІЯ": "SE", "НОРВЕГІЯ": "NO",
    "ДАНІЯ": "DK", "БЕЛЬГІЯ": "BE", "ПОРТУГАЛІЯ": "PT", "ІСПАНІЯ": "ES",
    "ІТАЛІЯ": "IT", "ГРЕЦІЯ": "GR", "ТУРЕЧЧИНА": "TR", "КИТАЙ": "CN",
    "ЯПОНІЯ": "JP", "КОРЕЯ": "KR", "ІНДІЯ": "IN", "БРАЗИЛІЯ": "BR",
    # English
    "UKRAINE": "UA", "GERMANY": "DE", "DEUTSCHLAND": "DE", "POLAND": "PL",
    "AUSTRIA": "AT", "SWITZERLAND": "CH", "FRANCE": "FR",
    "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB",
    "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US",
    "NETHERLANDS": "NL", "HOLLAND": "NL", "CZECH REPUBLIC": "CZ",
    "CZECHIA": "CZ", "SLOVAKIA": "SK", "HUNGARY": "HU", "ROMANIA": "RO",
    "BULGARIA": "BG", "CROATIA": "HR", "SERBIA": "RS", "SLOVENIA": "SI",
    "LITHUANIA": "LT", "LATVIA": "LV", "ESTONIA": "EE", "FINLAND": "FI",
    "SWEDEN": "SE", "NORWAY": "NO", "DENMARK": "DK", "BELGIUM": "BE",
    "PORTUGAL": "PT", "SPAIN": "ES", "ITALY": "IT", "GREECE": "GR",
    "TURKEY": "TR", "CHINA": "CN", "JAPAN": "JP", "SOUTH KOREA": "KR",
    "INDIA": "IN", "BRAZIL": "BR", "CANADA": "CA", "AUSTRALIA": "AU",
    "RUSSIA": "RU", "BELARUS": "BY",
}

# ── Прапори (ISO-2 ключі — відповідає тому, що зберігається в БД) ─────────────

FLAG_MAP = {
    "AD": "🇦🇩", "AE": "🇦🇪", "AT": "🇦🇹", "AU": "🇦🇺", "AZ": "🇦🇿",
    "BA": "🇧🇦", "BE": "🇧🇪", "BG": "🇧🇬", "BR": "🇧🇷", "BY": "🇧🇾",
    "CA": "🇨🇦", "CH": "🇨🇭", "CN": "🇨🇳", "CY": "🇨🇾", "CZ": "🇨🇿",
    "DE": "🇩🇪", "DK": "🇩🇰", "EE": "🇪🇪", "EG": "🇪🇬", "ES": "🇪🇸",
    "FI": "🇫🇮", "FR": "🇫🇷", "GB": "🇬🇧", "GE": "🇬🇪", "GR": "🇬🇷",
    "HK": "🇭🇰", "HR": "🇭🇷", "HU": "🇭🇺", "ID": "🇮🇩", "IE": "🇮🇪",
    "IL": "🇮🇱", "IN": "🇮🇳", "IS": "🇮🇸", "IT": "🇮🇹", "JP": "🇯🇵",
    "KR": "🇰🇷", "KZ": "🇰🇿", "LT": "🇱🇹", "LU": "🇱🇺", "LV": "🇱🇻",
    "MA": "🇲🇦", "MD": "🇲🇩", "ME": "🇲🇪", "MK": "🇲🇰", "MT": "🇲🇹",
    "MX": "🇲🇽", "MY": "🇲🇾", "NL": "🇳🇱", "NO": "🇳🇴", "NZ": "🇳🇿",
    "PH": "🇵🇭", "PL": "🇵🇱", "PT": "🇵🇹", "RO": "🇷🇴", "RS": "🇷🇸",
    "RU": "🇷🇺", "SA": "🇸🇦", "SE": "🇸🇪", "SG": "🇸🇬", "SI": "🇸🇮",
    "SK": "🇸🇰", "TH": "🇹🇭", "TN": "🇹🇳", "TR": "🇹🇷", "TW": "🇹🇼",
    "UA": "🇺🇦", "US": "🇺🇸", "UZ": "🇺🇿", "VN": "🇻🇳", "ZA": "🇿🇦",
}


# ── Публічні функції ──────────────────────────────────────────────────────────

def normalize_to_iso2(code: str) -> str:
    """Нормалізує будь-який рядок до ISO-2 коду країни.

    Приймає: "DE", "DEU", "Germany", "GERMANY", "Німеччина", "DE ", "de"
    Повертає: "DE" (або '' якщо нерозпізнано)
    """
    if not code:
        return ""
    c = code.strip().upper()
    if not c:
        return ""
    # Вже ISO-2
    if len(c) == 2 and c in ISO2_TO_ISO3:
        return c
    # ISO-3
    if len(c) == 3 and c in ISO3_TO_ISO2:
        return ISO3_TO_ISO2[c]
    # Аліас (повна назва або нестандарт)
    if c in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[c]
    # Fallback: взяти перші 2 символи якщо вони схожі на ISO-2
    if len(c) >= 2:
        candidate = c[:2]
        if candidate in ISO2_TO_ISO3:
            return candidate
    return ""


def to_iso3(iso2: str) -> str:
    """ISO-2 → ISO-3. Повертає вихідний рядок якщо не знайдено."""
    c = (iso2 or "").strip().upper()
    return ISO2_TO_ISO3.get(c, c)


def get_country_format() -> str:
    """Читає country_code_format з SystemSettings (iso2 або iso3). Default: iso2."""
    try:
        from config.models import SystemSettings
        return SystemSettings.get().country_code_format
    except Exception:
        return "iso2"


def display_country(iso2: str) -> str:
    """Повертає код країни у форматі згідно SystemSettings.

    iso2="DE" → "DE"  (якщо iso2)
    iso2="DE" → "DEU" (якщо iso3)
    """
    c = (iso2 or "").strip().upper()
    if not c:
        return ""
    if get_country_format() == "iso3":
        return ISO2_TO_ISO3.get(c, c)
    return c


def country_flag_html(iso2: str) -> str:
    """Повертає HTML: прапор + ISO-2 код для admin list_display.

    Логіка:
    - .fi.fi-xx  = flag-icons SVG sprite (CDN); коли завантажений, CSS ховає
                   emoji text через font-size:0 і показує background-image
    - .mv-flag-emoji = emoji fallback з правильним font-family; активний
                       коли CDN недоступний (.fi не визначений → font-size не 0)

    Приклад: 🇩🇪 DE (Mac/iOS: флаг SVG або emoji, Windows: завжди SVG з CDN)
    """
    c = normalize_to_iso2(iso2)
    if not c:
        return "—"
    emoji = FLAG_MAP.get(c, "🌍")
    return (
        f'<span class="fi fi-{c.lower()} mv-flag-emoji" title="{c}">{emoji}</span>'
        f'&nbsp;<span style="vertical-align:middle;font-size:.9em">{c}</span>'
    )
