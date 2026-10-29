"""Маленька позначка походження в куті етикетки DYMO (вертикально біля правого краю).

A — сформовано з шаблону з даними Minerva, B — готовий файл з бази, E — людина поправила тексти;
далі — дата формування («A 29.10.26»). Вмикається в «Налаштування продажів → 🏷️ Етикетки DYMO».
Лише формат DYMO Connect (DesktopLabel); об'єкт MinervaMark замінюється, тож файл з бази,
завантажений назад, не отримує другої позначки. Поправки у вікні перегляду ставлять E у браузері
(labels/_label_actions.html)."""
import re
from xml.sax.saxutils import escape

from django.utils import timezone

NAME = "MinervaMark"
CODES = {"auto": "A", "file": "B", "edited": "E"}
LEGEND = "A — з шаблону, B — файл з бази, E — поправлено вручну"
FONT_SIZE = 4            # pt: на папері ~1,4 мм, видно лише зблизька
STRIP = 0.06             # дюйма: ширина вільної смужки біля правого краю (усі макети закінчуються до x 3,363)
LENGTH = 0.4             # дюйма: довжина позначки вздовж краю, від низу

_OLD = re.compile(r"[ \t]*<TextObject>\s*<Name>" + NAME + r"</Name>.*?</TextObject>\s*", re.S)
_RECT = re.compile(r"<DYMORect>.*?<X>([\d.]+)</X>.*?<Y>([\d.]+)</Y>.*?<Width>([\d.]+)</Width>.*?<Height>([\d.]+)</Height>",
                   re.S)

_OBJECT = """        <TextObject>
          <Name>{name}</Name>
          <Brushes>
            <BackgroundBrush><SolidColorBrush><Color A="0" R="1" G="1" B="1"></Color></SolidColorBrush></BackgroundBrush>
            <BorderBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></BorderBrush>
            <StrokeBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></StrokeBrush>
            <FillBrush><SolidColorBrush><Color A="0" R="0" G="0" B="0"></Color></SolidColorBrush></FillBrush>
          </Brushes>
          <Rotation>Rotation90</Rotation>
          <OutlineThickness>1</OutlineThickness>
          <IsOutlined>False</IsOutlined>
          <BorderStyle>SolidLine</BorderStyle>
          <Margin><DYMOThickness Left="0" Top="0" Right="0" Bottom="0" /></Margin>
          <HorizontalAlignment>Left</HorizontalAlignment>
          <VerticalAlignment>Middle</VerticalAlignment>
          <FitMode>None</FitMode>
          <IsVertical>False</IsVertical>
          <FormattedText>
            <FitMode>None</FitMode>
            <HorizontalAlignment>Left</HorizontalAlignment>
            <VerticalAlignment>Middle</VerticalAlignment>
            <IsVertical>False</IsVertical>
            <LineTextSpan>
              <TextSpan>
                <Text>{text}</Text>
                <FontInfo>
                  <FontName>Arial</FontName>
                  <FontSize>{size}</FontSize>
                  <IsBold>False</IsBold>
                  <IsItalic>False</IsItalic>
                  <IsUnderline>False</IsUnderline>
                  <FontBrush><SolidColorBrush><Color A="1" R="0" G="0" B="0"></Color></SolidColorBrush></FontBrush>
                </FontInfo>
              </TextSpan>
            </LineTextSpan>
          </FormattedText>
          <ObjectLayout>
            <DYMOPoint><X>{x:.4f}</X><Y>{y:.4f}</Y></DYMOPoint>
            <Size><Width>{w:.4f}</Width><Height>{h:.4f}</Height></Size>
          </ObjectLayout>
        </TextObject>
"""


def enabled() -> bool:
    """«Позначка походження на етикетці» (SalesSettings.dymo_origin_mark), за замовчуванням — так."""
    try:
        from sales.models import SalesSettings
        return SalesSettings.get().dymo_origin_mark
    except Exception:
        return True


def text(origin, today=None) -> str:
    today = today or timezone.localdate()
    return f"{CODES.get(origin, 'A')} {today:%d.%m.%y}"


def stamp(xml, origin, today=None, on=None) -> str:
    """XML етикетки з позначкою origin ('auto' | 'file' | 'edited'); вимкнено — без позначки (стару прибирає)."""
    xml = _OLD.sub("", xml)
    if not (enabled() if on is None else on):
        return xml
    head, sep, tail = xml.rpartition("</LabelObjects>")
    rect = _RECT.search(xml)
    if not sep or not rect:          # старий формат DYMO Label 8 — як є
        return xml
    x, y, w, h = (float(v) for v in rect.groups())
    obj = _OBJECT.format(name=NAME, text=escape(text(origin, today)), size=FONT_SIZE,
                         x=x + w - STRIP, y=y + h - LENGTH, w=STRIP, h=LENGTH)
    return head + obj + "      " + sep + tail
