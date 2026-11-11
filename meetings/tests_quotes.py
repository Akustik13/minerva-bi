"""Цитата в розшифровці — ті самі правила, що на RAG-ПК: python manage.py test meetings --settings=tabele.settings_test

Спільні випадки (tests_quote_cases.CASES) проганяються і через meetings/quotes.py, і через moments.py вікна запису
(meetings/rag_pc/bench/moments.py — лише стандартна бібліотека), тож обидва боки погоджуються щодо кожного пункту.
"""
import importlib.util
from pathlib import Path

from django.test import SimpleTestCase

from meetings import quotes
from meetings.tests_quote_cases import CASES, LINES

BENCH_MOMENTS = Path(__file__).resolve().parent / "rag_pc" / "bench" / "moments.py"


def _bench():
    if not BENCH_MOMENTS.exists():
        return None
    spec = importlib.util.spec_from_file_location("bench_moments_for_minerva_tests", BENCH_MOMENTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuoteCasesTests(SimpleTestCase):
    def test_minerva_cases(self):
        for name, quote, line, expected in CASES:
            with self.subTest(name):
                self.assertEqual(quotes.place(quote, line, LINES), expected)

    def test_find_on_segments(self):
        segments = [{"n": n, "start": n * 5.0, "end": n * 5.0 + 4, "ch": "L", "text": t} for n, t in LINES.items()]
        for name, quote, line, expected in CASES:
            with self.subTest(name):
                self.assertEqual(quotes.find(quote, line, segments), expected)
        self.assertIsNone(quotes.find("liefern die Antennen bis Freitag", "x", []))
        self.assertEqual(quotes.find("liefern die Antennen bis Freitag", "3", segments), 3)

    def test_same_answers_as_the_bench(self):
        bench = _bench()
        if bench is None:
            self.skipTest("bench/moments.py немає поруч")
        ordered = [(n, 0.0, "", t) for n, t in sorted(LINES.items()) if t.strip()]
        for name, quote, line, expected in CASES:
            with self.subTest(name):
                self.assertEqual(bench.place(quote, line, LINES, ordered), expected)
                self.assertEqual(bench.normalize(quote), quotes.normalize(quote))


class KeyTests(SimpleTestCase):
    def test_item_key_ignores_case_quotes_and_spacing(self):
        a = quotes.item_key("task", "Wir liefern  „bis Freitag“.")
        b = quotes.item_key("task", "wir liefern \"bis freitag\"")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 40)
        self.assertNotEqual(a, quotes.item_key("risk", "wir liefern \"bis freitag\""))

    def test_mark_key_by_tenth_of_second(self):
        self.assertEqual(quotes.mark_key(12.34), quotes.mark_key("12.3"))
        self.assertNotEqual(quotes.mark_key(12.3), quotes.mark_key(12.5))
        self.assertEqual(quotes.mark_key("bad"), quotes.mark_key(0))
