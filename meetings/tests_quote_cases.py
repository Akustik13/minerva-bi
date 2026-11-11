"""Спільні випадки «де цитата в розшифровці» — однаково для Minerva (meetings/quotes.py, tests_quotes.py) і RAG-ПК
(bench/moments.py: normalize + place). Лише дані, без Django й без імпортів: RAG-ПК читає файл за шляхом.

LINES — рядки розшифровки {n: текст}; кожен випадок: (назва, цитата, line, очікуваний номер рядка або None).
"""
LINES = {
    1: "Guten Tag, sind alle einverstanden, dass wir aufzeichnen?",
    2: "Ja, das passt für uns.",
    3: "Wir liefern die Antennen bis Freitag,",
    4: "spätestens am 14. November an das Lager in Köln.",
    5: "Der Preis bleibt bei 12,50 Euro pro Stück für 500 Stück.",
    6: "",
    7: "Ми надішлемо креслення до п’ятниці — обіцяю.",
    8: "The shipment might be delayed because of customs in Basel.",
    9: "Okay — let’s “confirm” the order next week.",
}

CASES = [
    ("exact line", "liefern die Antennen bis Freitag", 3, 3),
    ("case and spaces", "DER PREIS  bleibt bei 12,50 euro", 5, 5),
    ("neighbour below", "spätestens am 14. November", 3, 4),
    ("neighbour above", "Wir liefern die Antennen", 4, 3),
    ("joined window anchors at line", "bis Freitag, spätestens am 14. November", 3, 3),
    ("joined window from the next line", "bis Freitag, spätestens am 14. November", 4, 4),
    ("any line far away", "might be delayed because of customs", 2, 8),
    ("typographic quotes and dashes", 'let\'s "confirm" the order', 9, 9),
    ("apostrophe and dash in Ukrainian", "креслення до п'ятниці - обіцяю", 7, 7),
    ("punctuation at the ends", "…Ja, das passt für uns!", 2, 2),
    ("too short", "Ja, das", 2, None),
    ("not in the transcript", "Wir stornieren den ganzen Auftrag", 3, None),
    ("empty line number", "Der Preis bleibt bei 12,50 Euro", 6, 5),
    ("unknown line number", "sind alle einverstanden, dass wir aufzeichnen", 99, 1),
]
