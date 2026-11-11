"""Picture mode: any image -> a 416x240 frame image in the panel's four greys.

Two layouts: full screen (416x240, no band flag), or with the top bar, where
the MCU draws its live band in rows 0..55 (FRAME_FLAG_BAND, as in forecast
frames), a 2 px rule under it as in forecast mode, and the picture fills the
416x181 below that.

Steps: EXIF orientation, a downscaled copy kept as the source (so a restart
or a fit/dither change never needs the original), fit to the canvas, then
error diffusion to the levels of frame.LEVELS. The result goes through
frame.encode_planes like the forecast.

Pure Python: Pillow only, no Home Assistant imports.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

from .frame import H, LEVELS, RULE_Y, W

PICTURE_TOP = RULE_Y + 2        # under the top bar: band, white row 56, rule 57..58

SOURCE_MAX = 1600               # longest side of the stored source (design 6.2)
FITS = ("contain", "cover")
DITHERS = ("floyd_steinberg", "atkinson", "none")


def area(top_bar: bool = False) -> tuple[int, int]:
    """(width, height) the picture is fitted to: 416x240, or 416x181 under the bar."""
    return (W, H - PICTURE_TOP) if top_bar else (W, H)


def load(data: bytes) -> Image.Image:
    """Image bytes -> upright RGB image (EXIF rotation applied, alpha on white)."""
    img: Image.Image = Image.open(io.BytesIO(data))
    img.load()
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P", "PA"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def shrink(img: Image.Image, longest: int = SOURCE_MAX) -> Image.Image:
    """A copy no larger than `longest` px on its longest side."""
    img = img.copy()
    img.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return img


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def fit(img: Image.Image, how: str = "contain",
        size: tuple[int, int] = (W, H)) -> Image.Image:
    """Grey `size` (416x240): `contain` letterboxes on white, `cover` crops the centre."""
    if how not in FITS:
        raise ValueError(f"fit must be one of {FITS}")
    grey = img.convert("L")
    if how == "cover":
        return ImageOps.fit(grey, size, Image.Resampling.LANCZOS)
    w, h = size
    out = Image.new("L", size, 255)
    inner = ImageOps.contain(grey, size, Image.Resampling.LANCZOS)
    out.paste(inner, ((w - inner.width) // 2, (h - inner.height) // 2))
    return out


def _nearest(v: float) -> int:
    return min(LEVELS, key=lambda q: abs(q - v))


def _atkinson(img: Image.Image) -> Image.Image:
    """Atkinson diffusion: 6 neighbours x 1/8, so 1/4 of the error is dropped,
    which keeps highlights and shadows clean on a 4-level panel. About 0.3 s
    in pure Python for one frame; run it in an executor."""
    w, h = img.size
    px = [float(v) for v in img.tobytes()]
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            old = px[i]
            new = _nearest(old)
            px[i] = new
            e = (old - new) / 8
            if not e:
                continue
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and ny < h:
                    px[ny * w + nx] += e
    return Image.frombytes("L", (w, h), bytes(int(v) for v in px))


def _palette_image() -> Image.Image:
    pal = Image.new("P", (1, 1))
    flat: list[int] = []
    for v in LEVELS:
        flat += [v, v, v]
    pal.putpalette(flat + [0, 0, 0] * (256 - len(LEVELS)))
    return pal


def dither(img: Image.Image, how: str = "floyd_steinberg") -> Image.Image:
    """'L' image -> 'L' image holding only the four LEVELS."""
    if how not in DITHERS:
        raise ValueError(f"dither must be one of {DITHERS}")
    img = img.convert("L")
    if how == "atkinson":
        return _atkinson(img)
    q = img.convert("RGB").quantize(
        palette=_palette_image(),
        dither=Image.Dither.FLOYDSTEINBERG if how == "floyd_steinberg" else Image.Dither.NONE)
    return q.convert("L")


def render(source: Image.Image, how_fit: str = "contain",
           how_dither: str = "floyd_steinberg", top_bar: bool = False) -> Image.Image:
    """Source image -> the 416x240 frame image (4 levels). With `top_bar` the
    picture is fitted and dithered for the area under the bar (rows 59..239),
    rows 0..55 are left white for the MCU and rows 57..58 hold the rule."""
    body = dither(fit(source, how_fit, area(top_bar)), how_dither)
    if not top_bar:
        return body
    out = Image.new("L", (W, H), 255)
    out.paste(0, (0, RULE_Y, W, RULE_Y + 2))
    out.paste(body, (0, PICTURE_TOP))
    return out
