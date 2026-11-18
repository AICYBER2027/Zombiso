"""Original VicePulse motion-graphics assets, drawn with Pillow and animated by FFmpeg at render time.

Everything here is generated from shapes, gradients and two open-licence fonts (Anton, Archivo Black;
SIL Open Font License, in assets/fonts). No third-party artwork or logos. A creator's own logo can be
supplied as a file (brand "logo").

All sizes are given on the 1080x1920 design canvas and scaled to the real output size.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import PACKAGE_ROOT

FONT_DIR = PACKAGE_ROOT / "assets" / "fonts"
TITLE_FONT = "Anton"                  # family names as libass sees them (captions use fontsdir=FONT_DIR)
CAPTION_FONT = "Archivo Black"
_FILES = {"Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}

DEFAULT_THEME = {"primary": "#FF2E88", "secondary": "#FF9A1F", "accent": "#FFD600", "dark": "#0E0B16",
                 "light": "#FFFFFF"}


def theme(brand: dict | None) -> dict:
    """Colours for a brand: channels.json brand.theme overrides the VicePulse sunset defaults."""
    brand = brand or {}
    t = {**DEFAULT_THEME, **(brand.get("theme") or {})}
    if brand.get("colour") and "accent" not in (brand.get("theme") or {}):
        t["accent"] = brand["colour"]
    return t


def font(family: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / _FILES[family]
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)


def monogram(name: str) -> str:
    """'VicePulse EN' -> 'VP' (capitals of the first word), 'Test Streamer' -> 'TS'."""
    words = name.replace("-", " ").split() or ["VP"]
    caps = [c for c in words[0] if c.isupper()]
    if len(caps) >= 2:
        return "".join(caps[:2])
    return "".join(w[0] for w in words[:2]).upper()


def rgb(hex_colour: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def _gradient(w: int, h: int, c0: str, c1: str, *, angle: float = 0.0) -> Image.Image:
    """Linear gradient c0 -> c1 (angle in degrees, 0 = left to right)."""
    a, b = rgb(c0), rgb(c1)
    small = Image.new("RGBA", (256, 1))
    for x in range(256):
        f = x / 255
        small.putpixel((x, 0), tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(4)))
    diag = int(math.hypot(w, h)) + 2
    g = small.resize((diag, diag), Image.BILINEAR).rotate(-angle, resample=Image.BILINEAR)
    return g.crop(((diag - w) // 2, (diag - h) // 2, (diag - w) // 2 + w, (diag - h) // 2 + h))


class Assets:
    """Generates (and caches) the PNGs for one output size and brand."""

    def __init__(self, folder: Path, size: tuple[int, int], brand: dict | None = None):
        self.W, self.H = size
        self.k = self.W / 1080
        self.brand = brand or {}
        self.t = theme(brand)
        key = hashlib.sha1(json.dumps([size, self.t, self.brand.get("name"), self.brand.get("handle"),
                                       self.brand.get("logo")], sort_keys=True).encode()).hexdigest()[:10]
        self.dir = Path(folder) / f"gfx_{key}"
        self.dir.mkdir(parents=True, exist_ok=True)

    def px(self, v: float) -> int:
        return max(1, round(v * self.k))

    def _save(self, img: Image.Image, name: str) -> str:
        path = self.dir / name
        img.save(path)
        return str(path)

    # -------------------------------------------------------------- frames

    def frame(self, w: int, h: int, *, name: str, radius: int = 26, stroke: int = 9, brackets: bool = True,
              tag: str | None = None, glow: bool = True, round_: bool = False, frames: int = 1) -> tuple[str, int]:
        """Branded border around a w x h box (output pixels). Returns (png, padding): place it at (x-pad, y-pad).

        frames > 1: an animated border - the gradient turns once around the frame over that many images,
        saved as name_00.png, name_01.png ... (returned as name_%02d.png, played as a looping sequence)."""
        if frames > 1:
            for i in range(frames):
                self._frame_one(w, h, name=f"{name}_{i:02d}", radius=radius, stroke=stroke, brackets=brackets,
                                tag=tag, glow=glow, round_=round_, angle=35 + 360 * i / frames)
            return str(self.dir / f"{name}_%02d.png"), self.px(34)
        return self._frame_one(w, h, name=name, radius=radius, stroke=stroke, brackets=brackets, tag=tag, glow=glow,
                               round_=round_, angle=35)

    def _frame_one(self, w: int, h: int, *, name: str, radius: int, stroke: int, brackets: bool, tag: str | None,
                   glow: bool, round_: bool, angle: float) -> tuple[str, int]:
        t, pad = self.t, self.px(34)
        W, H = w + 2 * pad, h + 2 * pad
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        s = self.px(stroke)
        box = (pad - s // 2, pad - s // 2, pad + w + s // 2, pad + h + s // 2)
        if round_:
            md.ellipse(box, outline=255, width=s)
        else:
            md.rounded_rectangle(box, radius=self.px(radius), outline=255, width=s)
        grad = _gradient(W, H, t["primary"], t["secondary"], angle=angle)
        if glow:
            halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            halo.paste(grad, (0, 0), mask.filter(ImageFilter.MaxFilter(5)))
            img = Image.alpha_composite(img, halo.filter(ImageFilter.GaussianBlur(self.px(14))))
        ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ring.paste(grad, (0, 0), mask)
        img = Image.alpha_composite(img, ring)
        d = ImageDraw.Draw(img)
        if brackets and not round_:
            L, bw, off = self.px(54), self.px(7), self.px(14)
            for cx, cy, sx, sy in ((pad - off, pad - off, 1, 1), (pad + w + off, pad - off, -1, 1),
                                   (pad - off, pad + h + off, 1, -1), (pad + w + off, pad + h + off, -1, -1)):
                d.line([(cx, cy), (cx + sx * L, cy)], fill=rgb(t["light"]), width=bw)
                d.line([(cx, cy), (cx, cy + sy * L)], fill=rgb(t["light"]), width=bw)
        if tag:
            f = font(TITLE_FONT, self.px(40))
            tw = int(d.textlength(tag, font=f))
            th = self.px(58)
            x0, y0 = pad + self.px(26), pad + h - th // 2
            poly = [(x0 + self.px(14), y0), (x0 + tw + self.px(48), y0), (x0 + tw + self.px(34), y0 + th), (x0, y0 + th)]
            d.polygon([(x + self.px(6), y + self.px(6)) for x, y in poly], fill=(0, 0, 0, 150))
            d.polygon(poly, fill=rgb(t["accent"]))
            d.text((x0 + self.px(24), y0 + th / 2), tag, font=f, fill=rgb(t["dark"]), anchor="lm")
        return self._save(img, f"{name}.png"), pad

    def pulse(self, w: int, h: int, *, name: str, radius: int = 26, round_: bool = False) -> tuple[str, int]:
        """Bright flash of the border, overlaid for a moment on the payoff."""
        pad = self.px(34)
        W, H = w + 2 * pad, h + 2 * pad
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        s = self.px(16)
        box = (pad - s // 2, pad - s // 2, pad + w + s // 2, pad + h + s // 2)
        (md.ellipse if round_ else md.rounded_rectangle)(box, **({} if round_ else {"radius": self.px(radius)}),
                                                         outline=255, width=s)
        glow = Image.new("RGBA", (W, H), rgb(self.t["light"], 0))
        glow.putalpha(mask.filter(ImageFilter.GaussianBlur(self.px(10))))
        core = Image.new("RGBA", (W, H), rgb(self.t["light"], 0))
        core.putalpha(mask)
        return self._save(Image.alpha_composite(glow, core), f"{name}.png"), pad

    def circle_mask(self, d: int, name: str = "circle_mask") -> str:
        m = Image.new("L", (d, d), 0)
        ImageDraw.Draw(m).ellipse((0, 0, d - 1, d - 1), fill=255)
        return self._save(m, f"{name}.png")

    # -------------------------------------------------------------- titles and tags

    def title_bar(self, text: str, *, max_w: int = 860, name: str = "title") -> str:
        """Slanted gradient bar with the title in Anton; slides in at the start."""
        t = self.t
        text = text.upper()
        size = 64
        f = font(TITLE_FONT, self.px(size))
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        while probe.textlength(text, font=f) > self.px(max_w - 120) and size > 34:
            size -= 4
            f = font(TITLE_FONT, self.px(size))
        tw = int(probe.textlength(text, font=f))
        h, skew = self.px(100), self.px(26)
        w = tw + self.px(110)
        img = Image.new("RGBA", (w + self.px(20), h + self.px(16)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        poly = [(skew, 0), (w, 0), (w - skew, h), (0, h)]
        d.polygon([(x + self.px(10), y + self.px(10)) for x, y in poly], fill=(0, 0, 0, 140))
        bar = _gradient(w, h, t["primary"], t["secondary"])
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).polygon(poly, fill=255)
        img.paste(bar, (0, 0), m)
        d.polygon([(skew - self.px(6), 0), (skew + self.px(10), 0), (self.px(16), h), (0, h)], fill=rgb(t["accent"]))
        d.text((w / 2 + self.px(6), h / 2 + self.px(4)), text, font=f, fill=(0, 0, 0, 120), anchor="mm")
        d.text((w / 2, h / 2), text, font=f, fill=rgb(t["light"]), anchor="mm")
        return self._save(img, f"{name}.png")

    def sticker(self, text: str, *, name: str, size: int = 54, fill: str | None = None, ink: str | None = None) -> str:
        """Small slanted label ("MOMENT", "LIVE"): accent box, dark Anton text, hard shadow."""
        t = self.t
        f = font(TITLE_FONT, self.px(size))
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        tw = int(probe.textlength(text, font=f))
        h, skew = self.px(size * 1.45), self.px(size * 0.35)
        w = tw + self.px(size * 1.3)
        img = Image.new("RGBA", (w + self.px(14), h + self.px(14)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        poly = [(skew, 0), (w, 0), (w - skew, h), (0, h)]
        d.polygon([(x + self.px(8), y + self.px(8)) for x, y in poly], fill=rgb(t["dark"], 230))
        d.polygon(poly, fill=rgb(fill or t["accent"]))
        d.text((w / 2, h / 2), text, font=f, fill=rgb(ink or t["dark"]), anchor="mm")
        return self._save(img, f"{name}.png")

    def lower_third(self, title: str, subtitle: str = "", *, name: str = "lower_third") -> str:
        """Clean name bar: dark rounded panel, accent stripe, white Archivo title, light subtitle."""
        t = self.t
        f1, f2 = font(CAPTION_FONT, self.px(44)), font(CAPTION_FONT, self.px(28))
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        tw = max(probe.textlength(title, font=f1), probe.textlength(subtitle, font=f2) if subtitle else 0)
        w, h = int(tw) + self.px(90), self.px(128 if subtitle else 92)
        img = Image.new("RGBA", (w + self.px(20), h + self.px(20)), (0, 0, 0, 0))
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((self.px(8), self.px(10), w + self.px(8), h + self.px(10)),
                                                 radius=self.px(18), fill=(0, 0, 0, 120))
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(self.px(6))))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w, h), radius=self.px(18), fill=rgb(t["dark"], 225))
        d.rounded_rectangle((0, 0, self.px(14), h), radius=self.px(7), fill=rgb(t["accent"]))
        d.text((self.px(40), self.px(18)), title, font=f1, fill=rgb(t["light"]))
        if subtitle:
            d.text((self.px(40), self.px(78)), subtitle, font=f2, fill=rgb(t["light"], 190))
        return self._save(img, f"{name}.png")

    def badge(self, *, d: int = 104, name: str = "badge") -> str:
        """The creator's logo (brand "logo" file) in a round badge, or a VicePulse-style monogram."""
        t = self.t
        D = self.px(d)
        img = Image.new("RGBA", (D + self.px(12), D + self.px(12)), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse((self.px(6), self.px(8), D + self.px(6), D + self.px(8)), fill=(0, 0, 0, 120))
        disc = _gradient(D, D, t["primary"], t["secondary"], angle=45)
        m = Image.new("L", (D, D), 0)
        ImageDraw.Draw(m).ellipse((0, 0, D - 1, D - 1), fill=255)
        img.paste(disc, (0, 0), m)
        logo = self.brand.get("logo")
        if logo and Path(logo).is_file():
            inner = Image.open(logo).convert("RGBA")
            inner.thumbnail((int(D * 0.78), int(D * 0.78)))
            img.alpha_composite(inner, ((D - inner.width) // 2, (D - inner.height) // 2))
        else:
            letters = self.brand.get("monogram") or monogram(self.brand.get("name") or "VicePulse")
            f = font(TITLE_FONT, int(D * 0.46))
            dr.text((D / 2, D / 2), letters or "VP", font=f, fill=rgb(t["light"]), anchor="mm")
        dr.ellipse((0, 0, D - 1, D - 1), outline=rgb(t["light"], 220), width=self.px(4))
        return self._save(img, f"{name}.png")

    # -------------------------------------------------------------- backgrounds and bursts

    def pattern(self, *, period: int = 120, name: str = "pattern") -> tuple[str, int]:
        """Dark backdrop with a soft brand glow and thin diagonal stripes; wider than the screen so it can scroll."""
        t = self.t
        P = self.px(period)
        W, H = self.W + P, self.H
        img = _gradient(W, H, t["dark"], "#1B1030", angle=90)
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse((-self.px(300), -self.px(200), self.px(700), self.px(800)), fill=rgb(t["primary"], 90))
        gd.ellipse((W - self.px(700), H - self.px(900), W + self.px(300), H + self.px(100)), fill=rgb(t["secondary"], 70))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(self.px(160))))
        lines = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lines)
        for x in range(-H, W + H, P):
            ld.line([(x, H), (x + H, 0)], fill=(255, 255, 255, 14), width=self.px(10))
        img = Image.alpha_composite(img, lines)
        return self._save(img, f"{name}.png"), P

    def burst(self, *, d: int = 900, rays: int = 18, name: str = "burst") -> str:
        """Comic impact burst (rays + ring) in the brand colours, shown for a split second on the payoff."""
        t = self.t
        D = self.px(d)
        img = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        c = D / 2
        for i in range(rays):
            a0 = 2 * math.pi * i / rays
            a1 = a0 + math.pi / rays * 0.55
            col = rgb(t["accent"] if i % 2 else t["primary"], 210)
            dr.polygon([(c + math.cos(a0) * D * 0.16, c + math.sin(a0) * D * 0.16),
                        (c + math.cos(a0) * D * 0.5, c + math.sin(a0) * D * 0.5),
                        (c + math.cos(a1) * D * 0.5, c + math.sin(a1) * D * 0.5)], fill=col)
        img = img.filter(ImageFilter.GaussianBlur(self.px(2)))
        mask = Image.new("L", (D, D), 0)
        ImageDraw.Draw(mask).ellipse((D * 0.3, D * 0.3, D * 0.7, D * 0.7), fill=255)
        holes = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        img.paste(holes, (0, 0), mask)                       # keep the middle clear: the action stays visible
        return self._save(img, f"{name}.png")

    def light_leak(self, *, name: str = "leak") -> str:
        """Warm light leak washing in from one side (Cinematic transitions)."""
        t = self.t
        img = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((-self.W * 0.6, -self.H * 0.1, self.W * 0.7, self.H * 0.9), fill=rgb(t["secondary"], 150))
        d.ellipse((self.W * 0.1, self.H * 0.2, self.W * 1.2, self.H * 0.7), fill=rgb(t["primary"], 90))
        return self._save(img.filter(ImageFilter.GaussianBlur(self.px(180))), f"{name}.png")

    def rounded_mask(self, w: int, h: int, *, radius: int = 28, name: str = "rmask") -> str:
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=self.px(radius), fill=255)
        return self._save(m, f"{name}.png")
