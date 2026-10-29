"""Сповіщення в Outlook (рушій Word): python manage.py test dashboard.tests_outlook --settings=tabele.settings_test"""
from django.test import SimpleTestCase

from dashboard.notifications import outlook_safe

SAMPLE = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="font-family:Arial,sans-serif;margin:0">'
    '<div style="max-width:580px;margin:0 auto;background:#fff">'
    '<table style="width:100%;border-collapse:collapse"><tr><td>'
    '<a href="https://x.example/p.jpg"><img src="https://x.example/p.jpg" width="60" height="60" class="mv-thumb"'
    ' style="object-fit:cover"></a>'
    '<img src="https://x.example/p.jpg" class="mv-big" style="display:none">'
    '</td></tr></table></div></body></html>')


class OutlookSafeTests(SimpleTestCase):
    def test_hover_image_hidden_from_outlook_and_fixed_width(self):
        html = outlook_safe(SAMPLE)
        # велике фото для наведення — лише не для Outlook; мініатюра з width/height лишається для всіх
        self.assertIn('<!--[if !mso]><!--><img src="https://x.example/p.jpg" class="mv-big" '
                      'style="mso-hide:all;display:none"><!--<![endif]-->', html)
        self.assertIn('width="60" height="60" class="mv-thumb"', html)
        self.assertIn('<!--[if mso]><table role="presentation" align="center" width="580"', html)
        self.assertIn('<!--[if mso]></td></tr></table><![endif]--></body>', html)
        self.assertIn('<table width="100%" style="width:100%', html)
        self.assertIn('<!--[if mso]><style>table,td{font-family:Arial,sans-serif}</style><![endif]--></head>', html)
        self.assertEqual(outlook_safe(html).count('<!--[if mso]><table'), 1)          # повторно — без змін
        self.assertEqual(outlook_safe(html), html)
        self.assertEqual(outlook_safe(""), "")
