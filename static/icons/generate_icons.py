"""
Minerva BI — генератор іконок PWA.
Запуск: python static/icons/generate_icons.py
Потрібно: pip install Pillow
"""
from PIL import Image, ImageDraw, ImageFont
import os

SIZES = [72, 96, 128, 144, 152, 192, 384, 512]
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

BG_COLOR   = (14, 16, 24)     # #0e1018
ACCENT     = (0, 212, 170)    # #00d4aa
ACCENT2    = (79, 143, 255)   # #4f8fff


def create_icon(size: int) -> Image.Image:
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = size // 5
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG_COLOR)
    draw.rounded_rectangle(
        [2, 2, size - 3, size - 3],
        radius=radius - 2, outline=ACCENT, width=max(1, size // 48),
    )

    font_size = int(size * 0.52)
    font = None
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
    ]:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()

    text = "M"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x  = (size - tw) // 2 - bbox[0]
    y  = (size - th) // 2 - bbox[1]

    draw.text((x + max(1, size // 64), y + max(1, size // 64)), text, font=font, fill=(0, 0, 0, 120))
    draw.text((x, y), text, font=font, fill=ACCENT)

    dot = max(2, size // 20)
    draw.ellipse([size - dot * 3, size - dot * 3, size - dot, size - dot], fill=ACCENT2)

    return img


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for size in SIZES:
        img  = create_icon(size)
        path = os.path.join(OUTPUT_DIR, f'icon-{size}.png')
        img.save(path, 'PNG', optimize=True)
        print(f'OK icon-{size}.png saved')
    print(f'\nВсі іконки збережено в {OUTPUT_DIR}')


if __name__ == '__main__':
    main()
