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

# ── ISO-2 → англійська назва (для документів: packing list, інвойси) ─────────

ISO2_TO_NAME_EN = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AL": "Albania",
    "AM": "Armenia", "AO": "Angola", "AR": "Argentina", "AT": "Austria", "AU": "Australia",
    "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina", "BD": "Bangladesh", "BE": "Belgium",
    "BF": "Burkina Faso", "BG": "Bulgaria", "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin",
    "BN": "Brunei", "BO": "Bolivia", "BR": "Brazil", "BS": "Bahamas", "BT": "Bhutan",
    "BW": "Botswana", "BY": "Belarus", "BZ": "Belize", "CA": "Canada",
    "CD": "DR Congo", "CF": "Central African Republic", "CG": "Congo", "CH": "Switzerland",
    "CI": "Côte d'Ivoire", "CL": "Chile", "CM": "Cameroon", "CN": "China", "CO": "Colombia",
    "CR": "Costa Rica", "CU": "Cuba", "CV": "Cape Verde", "CY": "Cyprus", "CZ": "Czech Republic",
    "DE": "Germany", "DJ": "Djibouti", "DK": "Denmark", "DO": "Dominican Republic",
    "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "ER": "Eritrea",
    "ES": "Spain", "ET": "Ethiopia", "FI": "Finland", "FJ": "Fiji", "FR": "France",
    "GA": "Gabon", "GB": "United Kingdom", "GE": "Georgia", "GH": "Ghana", "GM": "Gambia",
    "GN": "Guinea", "GQ": "Equatorial Guinea", "GR": "Greece", "GT": "Guatemala",
    "GW": "Guinea-Bissau", "GY": "Guyana", "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia",
    "HT": "Haiti", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel",
    "IN": "India", "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JM": "Jamaica",
    "JO": "Jordan", "JP": "Japan", "KE": "Kenya", "KG": "Kyrgyzstan", "KH": "Cambodia",
    "KM": "Comoros", "KN": "Saint Kitts and Nevis", "KP": "North Korea", "KR": "South Korea",
    "KW": "Kuwait", "KZ": "Kazakhstan", "LA": "Laos", "LB": "Lebanon", "LC": "Saint Lucia",
    "LI": "Liechtenstein", "LK": "Sri Lanka", "LR": "Liberia", "LS": "Lesotho",
    "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "LY": "Libya", "MA": "Morocco",
    "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro", "MG": "Madagascar",
    "MK": "North Macedonia", "ML": "Mali", "MM": "Myanmar", "MN": "Mongolia",
    "MR": "Mauritania", "MT": "Malta", "MU": "Mauritius", "MV": "Maldives", "MW": "Malawi",
    "MX": "Mexico", "MY": "Malaysia", "MZ": "Mozambique", "NA": "Namibia", "NE": "Niger",
    "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands", "NO": "Norway", "NP": "Nepal",
    "NR": "Nauru", "NZ": "New Zealand", "OM": "Oman", "PA": "Panama", "PE": "Peru",
    "PG": "Papua New Guinea", "PH": "Philippines", "PK": "Pakistan", "PL": "Poland",
    "PR": "Puerto Rico", "PT": "Portugal", "PW": "Palau", "PY": "Paraguay", "QA": "Qatar",
    "RO": "Romania", "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia",
    "SB": "Solomon Islands", "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden",
    "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "SL": "Sierra Leone",
    "SM": "San Marino", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname",
    "ST": "São Tomé and Príncipe", "SV": "El Salvador", "SY": "Syria", "SZ": "Eswatini",
    "TD": "Chad", "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan", "TL": "Timor-Leste",
    "TM": "Turkmenistan", "TN": "Tunisia", "TO": "Tonga", "TR": "Turkey",
    "TT": "Trinidad and Tobago", "TV": "Tuvalu", "TW": "Taiwan", "TZ": "Tanzania",
    "UA": "Ukraine", "UG": "Uganda", "US": "United States", "UY": "Uruguay",
    "UZ": "Uzbekistan", "VA": "Vatican City", "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela", "VN": "Vietnam", "VU": "Vanuatu", "WS": "Samoa", "YE": "Yemen",
    "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}

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


def country_name_en(code: str) -> str:
    """Будь-який код/назва → англійська назва країни. Невідомо → вихідний рядок."""
    iso2 = normalize_to_iso2(code)
    return ISO2_TO_NAME_EN.get(iso2) or (code or "").strip()


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

    Ієрархія:
    1. <img> з flagcdn.com PNG — реальний PNG прапор, рендериться на всіх платформах
       (Windows/Mac/Linux), не залежить від emoji-підтримки ОС.
    2. onerror → показує emoji span (.mv-flag-emoji) з правильним font-family
       (Apple Color Emoji / Segoe UI Emoji / Noto Color Emoji).
    3. Завжди показує ISO-2 код текстом поруч.
    """
    c = normalize_to_iso2(iso2)
    if not c:
        return "—"
    emoji = FLAG_MAP.get(c, "🌍")
    c_lower = c.lower()
    # onerror: ховаємо img і показуємо emoji-span
    return (
        f'<span style="white-space:nowrap;vertical-align:middle" title="{c}">'
        f'<img src="https://flagcdn.com/16x12/{c_lower}.png" width="16" height="12"'
        f' style="vertical-align:middle;margin-right:3px;border-radius:1px"'
        f' onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline\'"'
        f' alt="{emoji}">'
        f'<span class="mv-flag-emoji"'
        f' style="display:none;margin-right:2px;vertical-align:middle">{emoji}</span>'
        f'<span style="vertical-align:middle;font-size:.9em">{c}</span>'
        f'</span>'
    )
