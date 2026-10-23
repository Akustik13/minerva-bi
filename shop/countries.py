"""Довідник країн для доставки: код ISO, назва (uk / de / en) і регіон — для редактора зон доставки.

Регіони — як групи на DigiKey: вибрати всі країни регіону одним кліком і зняти окремі.
"""
from __future__ import annotations

# (ключ, назва українською, німецькою, англійською, [(код, uk, de, en), …])
REGIONS = [
    ("eu", "Європейський Союз", "Europäische Union", "European Union", [
        ("AT", "Австрія", "Österreich", "Austria"), ("BE", "Бельгія", "Belgien", "Belgium"),
        ("BG", "Болгарія", "Bulgarien", "Bulgaria"), ("CY", "Кіпр", "Zypern", "Cyprus"),
        ("CZ", "Чехія", "Tschechien", "Czechia"), ("DE", "Німеччина", "Deutschland", "Germany"),
        ("DK", "Данія", "Dänemark", "Denmark"), ("EE", "Естонія", "Estland", "Estonia"),
        ("ES", "Іспанія", "Spanien", "Spain"), ("FI", "Фінляндія", "Finnland", "Finland"),
        ("FR", "Франція", "Frankreich", "France"), ("GR", "Греція", "Griechenland", "Greece"),
        ("HR", "Хорватія", "Kroatien", "Croatia"), ("HU", "Угорщина", "Ungarn", "Hungary"),
        ("IE", "Ірландія", "Irland", "Ireland"), ("IT", "Італія", "Italien", "Italy"),
        ("LT", "Литва", "Litauen", "Lithuania"), ("LU", "Люксембург", "Luxemburg", "Luxembourg"),
        ("LV", "Латвія", "Lettland", "Latvia"), ("MT", "Мальта", "Malta", "Malta"),
        ("NL", "Нідерланди", "Niederlande", "Netherlands"), ("PL", "Польща", "Polen", "Poland"),
        ("PT", "Португалія", "Portugal", "Portugal"), ("RO", "Румунія", "Rumänien", "Romania"),
        ("SE", "Швеція", "Schweden", "Sweden"), ("SI", "Словенія", "Slowenien", "Slovenia"),
        ("SK", "Словаччина", "Slowakei", "Slovakia"),
    ]),
    ("europe", "Європа (поза ЄС)", "Europa (außerhalb der EU)", "Europe (non-EU)", [
        ("AD", "Андорра", "Andorra", "Andorra"), ("AL", "Албанія", "Albanien", "Albania"),
        ("AM", "Вірменія", "Armenien", "Armenia"), ("AZ", "Азербайджан", "Aserbaidschan", "Azerbaijan"),
        ("BA", "Боснія і Герцеговина", "Bosnien und Herzegowina", "Bosnia and Herzegovina"),
        ("BY", "Білорусь", "Belarus", "Belarus"), ("CH", "Швейцарія", "Schweiz", "Switzerland"),
        ("GB", "Велика Британія", "Vereinigtes Königreich", "United Kingdom"),
        ("GE", "Грузія", "Georgien", "Georgia"), ("IS", "Ісландія", "Island", "Iceland"),
        ("LI", "Ліхтенштейн", "Liechtenstein", "Liechtenstein"), ("MC", "Монако", "Monaco", "Monaco"),
        ("MD", "Молдова", "Moldau", "Moldova"), ("ME", "Чорногорія", "Montenegro", "Montenegro"),
        ("MK", "Північна Македонія", "Nordmazedonien", "North Macedonia"), ("NO", "Норвегія", "Norwegen", "Norway"),
        ("RS", "Сербія", "Serbien", "Serbia"), ("RU", "Росія", "Russland", "Russia"),
        ("SM", "Сан-Марино", "San Marino", "San Marino"), ("TR", "Туреччина", "Türkei", "Türkiye"),
        ("UA", "Україна", "Ukraine", "Ukraine"), ("VA", "Ватикан", "Vatikanstadt", "Vatican City"),
    ]),
    ("north_america", "Північна Америка", "Nordamerika", "North America", [
        ("CA", "Канада", "Kanada", "Canada"), ("MX", "Мексика", "Mexiko", "Mexico"),
        ("US", "США", "Vereinigte Staaten", "United States"),
    ]),
    ("latin_america", "Центральна й Південна Америка, Кариби", "Mittel- und Südamerika, Karibik",
     "Central & South America, Caribbean", [
        ("AR", "Аргентина", "Argentinien", "Argentina"), ("BB", "Барбадос", "Barbados", "Barbados"),
        ("BO", "Болівія", "Bolivien", "Bolivia"), ("BR", "Бразилія", "Brasilien", "Brazil"),
        ("BS", "Багамські Острови", "Bahamas", "Bahamas"), ("BZ", "Беліз", "Belize", "Belize"),
        ("CL", "Чилі", "Chile", "Chile"), ("CO", "Колумбія", "Kolumbien", "Colombia"),
        ("CR", "Коста-Рика", "Costa Rica", "Costa Rica"), ("CU", "Куба", "Kuba", "Cuba"),
        ("DO", "Домініканська Республіка", "Dominikanische Republik", "Dominican Republic"),
        ("EC", "Еквадор", "Ecuador", "Ecuador"), ("GT", "Гватемала", "Guatemala", "Guatemala"),
        ("GY", "Гаяна", "Guyana", "Guyana"), ("HN", "Гондурас", "Honduras", "Honduras"),
        ("HT", "Гаїті", "Haiti", "Haiti"), ("JM", "Ямайка", "Jamaika", "Jamaica"),
        ("NI", "Нікарагуа", "Nicaragua", "Nicaragua"), ("PA", "Панама", "Panama", "Panama"),
        ("PE", "Перу", "Peru", "Peru"), ("PR", "Пуерто-Рико", "Puerto Rico", "Puerto Rico"),
        ("PY", "Парагвай", "Paraguay", "Paraguay"), ("SR", "Суринам", "Suriname", "Suriname"),
        ("SV", "Сальвадор", "El Salvador", "El Salvador"),
        ("TT", "Тринідад і Тобаго", "Trinidad und Tobago", "Trinidad and Tobago"),
        ("UY", "Уругвай", "Uruguay", "Uruguay"), ("VE", "Венесуела", "Venezuela", "Venezuela"),
    ]),
    ("asia", "Азія", "Asien", "Asia", [
        ("AF", "Афганістан", "Afghanistan", "Afghanistan"), ("BD", "Бангладеш", "Bangladesch", "Bangladesh"),
        ("BN", "Бруней", "Brunei", "Brunei"), ("BT", "Бутан", "Bhutan", "Bhutan"),
        ("CN", "Китай", "China", "China"), ("HK", "Гонконг", "Hongkong", "Hong Kong"),
        ("ID", "Індонезія", "Indonesien", "Indonesia"), ("IN", "Індія", "Indien", "India"),
        ("JP", "Японія", "Japan", "Japan"), ("KG", "Киргизстан", "Kirgisistan", "Kyrgyzstan"),
        ("KH", "Камбоджа", "Kambodscha", "Cambodia"), ("KR", "Південна Корея", "Südkorea", "South Korea"),
        ("KZ", "Казахстан", "Kasachstan", "Kazakhstan"), ("LA", "Лаос", "Laos", "Laos"),
        ("LK", "Шрі-Ланка", "Sri Lanka", "Sri Lanka"), ("MM", "М'янма", "Myanmar", "Myanmar"),
        ("MN", "Монголія", "Mongolei", "Mongolia"), ("MO", "Макао", "Macau", "Macao"),
        ("MV", "Мальдіви", "Malediven", "Maldives"), ("MY", "Малайзія", "Malaysia", "Malaysia"),
        ("NP", "Непал", "Nepal", "Nepal"), ("PH", "Філіппіни", "Philippinen", "Philippines"),
        ("PK", "Пакистан", "Pakistan", "Pakistan"), ("SG", "Сінгапур", "Singapur", "Singapore"),
        ("TH", "Таїланд", "Thailand", "Thailand"), ("TJ", "Таджикистан", "Tadschikistan", "Tajikistan"),
        ("TL", "Східний Тимор", "Osttimor", "Timor-Leste"), ("TM", "Туркменістан", "Turkmenistan", "Turkmenistan"),
        ("TW", "Тайвань", "Taiwan", "Taiwan"), ("UZ", "Узбекистан", "Usbekistan", "Uzbekistan"),
        ("VN", "В'єтнам", "Vietnam", "Vietnam"),
    ]),
    ("middle_east", "Близький Схід", "Naher Osten", "Middle East", [
        ("AE", "ОАЕ", "Vereinigte Arabische Emirate", "United Arab Emirates"), ("BH", "Бахрейн", "Bahrain", "Bahrain"),
        ("IL", "Ізраїль", "Israel", "Israel"), ("IQ", "Ірак", "Irak", "Iraq"), ("IR", "Іран", "Iran", "Iran"),
        ("JO", "Йорданія", "Jordanien", "Jordan"), ("KW", "Кувейт", "Kuwait", "Kuwait"),
        ("LB", "Ліван", "Libanon", "Lebanon"), ("OM", "Оман", "Oman", "Oman"),
        ("PS", "Палестина", "Palästina", "Palestine"), ("QA", "Катар", "Katar", "Qatar"),
        ("SA", "Саудівська Аравія", "Saudi-Arabien", "Saudi Arabia"), ("SY", "Сирія", "Syrien", "Syria"),
        ("YE", "Ємен", "Jemen", "Yemen"),
    ]),
    ("africa", "Африка", "Afrika", "Africa", [
        ("AO", "Ангола", "Angola", "Angola"), ("BF", "Буркіна-Фасо", "Burkina Faso", "Burkina Faso"),
        ("BI", "Бурунді", "Burundi", "Burundi"), ("BJ", "Бенін", "Benin", "Benin"),
        ("BW", "Ботсвана", "Botsuana", "Botswana"),
        ("CD", "ДР Конго", "Demokratische Republik Kongo", "DR Congo"),
        ("CF", "Центральноафриканська Республіка", "Zentralafrikanische Republik", "Central African Republic"),
        ("CG", "Республіка Конго", "Republik Kongo", "Republic of the Congo"),
        ("CI", "Кот-д'Івуар", "Elfenbeinküste", "Côte d'Ivoire"), ("CM", "Камерун", "Kamerun", "Cameroon"),
        ("CV", "Кабо-Верде", "Kap Verde", "Cape Verde"), ("DJ", "Джибуті", "Dschibuti", "Djibouti"),
        ("DZ", "Алжир", "Algerien", "Algeria"), ("EG", "Єгипет", "Ägypten", "Egypt"),
        ("ER", "Еритрея", "Eritrea", "Eritrea"), ("ET", "Ефіопія", "Äthiopien", "Ethiopia"),
        ("GA", "Габон", "Gabun", "Gabon"), ("GH", "Гана", "Ghana", "Ghana"), ("GM", "Гамбія", "Gambia", "Gambia"),
        ("GN", "Гвінея", "Guinea", "Guinea"), ("GQ", "Екваторіальна Гвінея", "Äquatorialguinea", "Equatorial Guinea"),
        ("GW", "Гвінея-Бісау", "Guinea-Bissau", "Guinea-Bissau"), ("KE", "Кенія", "Kenia", "Kenya"),
        ("KM", "Коморські Острови", "Komoren", "Comoros"), ("LR", "Ліберія", "Liberia", "Liberia"),
        ("LS", "Лесото", "Lesotho", "Lesotho"), ("LY", "Лівія", "Libyen", "Libya"),
        ("MA", "Марокко", "Marokko", "Morocco"), ("MG", "Мадагаскар", "Madagaskar", "Madagascar"),
        ("ML", "Малі", "Mali", "Mali"), ("MR", "Мавританія", "Mauretanien", "Mauritania"),
        ("MU", "Маврикій", "Mauritius", "Mauritius"), ("MW", "Малаві", "Malawi", "Malawi"),
        ("MZ", "Мозамбік", "Mosambik", "Mozambique"), ("NA", "Намібія", "Namibia", "Namibia"),
        ("NE", "Нігер", "Niger", "Niger"), ("NG", "Нігерія", "Nigeria", "Nigeria"),
        ("RW", "Руанда", "Ruanda", "Rwanda"), ("SC", "Сейшельські Острови", "Seychellen", "Seychelles"),
        ("SD", "Судан", "Sudan", "Sudan"), ("SL", "Сьєрра-Леоне", "Sierra Leone", "Sierra Leone"),
        ("SN", "Сенегал", "Senegal", "Senegal"), ("SO", "Сомалі", "Somalia", "Somalia"),
        ("SS", "Південний Судан", "Südsudan", "South Sudan"),
        ("ST", "Сан-Томе і Принсіпі", "São Tomé und Príncipe", "São Tomé and Príncipe"),
        ("SZ", "Есватіні", "Eswatini", "Eswatini"), ("TD", "Чад", "Tschad", "Chad"),
        ("TG", "Того", "Togo", "Togo"), ("TN", "Туніс", "Tunesien", "Tunisia"),
        ("TZ", "Танзанія", "Tansania", "Tanzania"), ("UG", "Уганда", "Uganda", "Uganda"),
        ("ZA", "Південно-Африканська Республіка", "Südafrika", "South Africa"),
        ("ZM", "Замбія", "Sambia", "Zambia"), ("ZW", "Зімбабве", "Simbabwe", "Zimbabwe"),
    ]),
    ("oceania", "Австралія й Океанія", "Australien und Ozeanien", "Australia & Oceania", [
        ("AU", "Австралія", "Australien", "Australia"), ("FJ", "Фіджі", "Fidschi", "Fiji"),
        ("NC", "Нова Каледонія", "Neukaledonien", "New Caledonia"),
        ("NZ", "Нова Зеландія", "Neuseeland", "New Zealand"),
        ("PF", "Французька Полінезія", "Französisch-Polynesien", "French Polynesia"),
        ("PG", "Папуа Нова Гвінея", "Papua-Neuguinea", "Papua New Guinea"),
        ("SB", "Соломонові Острови", "Salomonen", "Solomon Islands"), ("TO", "Тонга", "Tonga", "Tonga"),
        ("VU", "Вануату", "Vanuatu", "Vanuatu"), ("WS", "Самоа", "Samoa", "Samoa"),
    ]),
]

COUNTRIES = {c[0]: {"code": c[0], "uk": c[1], "de": c[2], "en": c[3], "region": r[0]}
             for r in REGIONS for c in r[4]}
REGION_CODES = {r[0]: [c[0] for c in r[4]] for r in REGIONS}


def name(code: str, lang: str = "uk") -> str:
    """«Німеччина (DE)»; невідомий код — як є."""
    c = COUNTRIES.get((code or "").upper())
    if code == "*":
        return {"uk": "Усі інші країни", "de": "Alle anderen Länder", "en": "All other countries"}.get(lang, "*")
    return f"{c[lang if lang in ('uk', 'de', 'en') else 'uk']} ({c['code']})" if c else (code or "")


def catalog() -> dict:
    """Для редактора в адмінці (json_script): регіони з країнами."""
    return {"regions": [{"key": r[0], "label": r[1], "countries": [[c[0], c[1], c[2], c[3]] for c in r[4]]}
                        for r in REGIONS]}
