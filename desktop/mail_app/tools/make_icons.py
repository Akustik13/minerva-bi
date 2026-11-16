"""Значки «Пошта Minerva» (стиль іконки Minerva: темний квадрат, бірюзова рамка, синя крапка) — конверт.

    python desktop/mail_app/tools/make_icons.py

  build/icon.ico            — програма й інсталятор (16…256)
  assets/icon.png           — 256 px (вікно, сповіщення)
  assets/tray.ico           — трей: звичайний
  assets/tray-unread.ico    — трей: є непрочитані (червона крапка)
  assets/tray-off.ico       — трей: немає зв'язку / треба увійти (сірий)
  assets/badge/<n>.ico      — цифра на значку панелі задач (1…9, 9plus)
Потрібен Pillow.
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = (14, 16, 24, 255)
ACCENT = (0, 212, 170, 255)
ACCENT2 = (79, 143, 255, 255)
GRAY = (130, 136, 150, 255)
RED = (229, 57, 53, 255)
SS = 4   # малюємо вчетверо більшим і зменшуємо — рівні краї


def _font(px):
    for path in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            pass
    return ImageFont.load_default()


def app_icon(size, accent=ACCENT, dot=ACCENT2, unread=False):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = s // 5
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG)
    w = max(SS, s // 28)
    d.rounded_rectangle([w // 2, w // 2, s - 1 - w // 2, s - 1 - w // 2], radius=r - w // 2, outline=accent, width=w)
    # конверт
    x0, y0, x1, y1 = s * 0.19, s * 0.29, s * 0.81, s * 0.71
    lw = max(SS, int(s * 0.055))
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(s * 0.05), outline=accent, width=lw)
    d.line([(x0 + lw * 0.6, y0 + lw * 0.6), ((x0 + x1) / 2, (y0 + y1) / 2 + s * 0.02), (x1 - lw * 0.6, y0 + lw * 0.6)],
           fill=accent, width=lw, joint="curve")
    if dot:
        rad = s * 0.075
        cx, cy = s * 0.86, s * 0.86
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=dot)
    if unread:
        rad = s * 0.17
        cx, cy = s * 0.80, s * 0.20
        d.ellipse([cx - rad - SS * 2, cy - rad - SS * 2, cx + rad + SS * 2, cy + rad + SS * 2], fill=BG)
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=RED)
    return img.resize((size, size), Image.LANCZOS)


def badge(text, size):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, s - 1, s - 1], fill=RED)
    font = _font(int(s * (0.72 if len(text) == 1 else 0.5)))
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((s - tw) / 2 - box[0], (s - th) / 2 - box[1]), text, font=font, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path, make, sizes):
    imgs = [make(sz) for sz in sizes]
    imgs[-1].save(path, format="ICO", sizes=[(sz, sz) for sz in sizes], append_images=imgs[:-1])


def main():
    os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "assets", "badge"), exist_ok=True)
    save_ico(os.path.join(ROOT, "build", "icon.ico"), app_icon, [16, 24, 32, 48, 64, 128, 256])
    app_icon(256).save(os.path.join(ROOT, "assets", "icon.png"))
    tray = [16, 20, 24, 32, 40, 48]
    save_ico(os.path.join(ROOT, "assets", "tray.ico"), lambda sz: app_icon(sz, dot=None), tray)
    save_ico(os.path.join(ROOT, "assets", "tray-unread.ico"), lambda sz: app_icon(sz, dot=None, unread=True), tray)
    save_ico(os.path.join(ROOT, "assets", "tray-off.ico"), lambda sz: app_icon(sz, accent=GRAY, dot=None), tray)
    for n in list(range(1, 10)) + ["9plus"]:
        text = "9+" if n == "9plus" else str(n)
        save_ico(os.path.join(ROOT, "assets", "badge", f"{n}.ico"), lambda sz, t=text: badge(t, sz), [16, 20, 24, 32])
    print("ok")


if __name__ == "__main__":
    main()
