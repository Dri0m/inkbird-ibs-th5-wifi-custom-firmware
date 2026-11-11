"""The preview image entity's picture: the stored frame plus an approximate band.

The frame is decoded from the slot that would be sent, so the body is exactly
what the panel gets. In forecast frames (FRAME_FLAG_BAND) rows 0..55 belong
to the MCU, which draws its live band there; here it is approximated after
firmware/src/render.c: clock at x 8 (32 px digits, 4 px down for the bezel),
the date under it, temperature at x 128 and humidity at x 262 with small
labels. Battery and Wi-Fi icons are left out.

Pure Python: Pillow only, no Home Assistant imports.
"""
from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .forecast import font, num
from .frame import BAND_H, FRAME_FLAG_BAND, decode_planes, parse_slot

SCALE = 2                       # nearest-neighbour upscale, crisp in a picture card

DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Band:
    """What the MCU would draw: local time and the indoor reading in display units."""
    now: dt.datetime
    clock_24h: bool = True
    units: str = "C"
    temp: float | None = None   # in `units`
    humidity: float | None = None
    monochrome: bool = False


def band_texts(b: Band) -> tuple[str, str, str, str]:
    """(clock, date, temperature, humidity) as render.c formats them."""
    h = b.now.hour if b.clock_24h else (b.now.hour % 12 or 12)
    clock = f"{h:02d}:{b.now.minute:02d}"
    date = f"{DOW[b.now.weekday()]} {b.now.day} {MON[b.now.month - 1]}"
    temp = ("--.-" if b.temp is None else num(b.temp, 1)) + "°" + b.units
    rh = ("--.-" if b.humidity is None else num(b.humidity, 1)) + "%"
    return clock, date, temp, rh


def draw_band(img: Image.Image, b: Band) -> None:
    """Draw the approximate band into rows 0..55 of a 416x240 'L' image."""
    dr = ImageDraw.Draw(img)
    dr.fontmode = "L"                   # approximate the MCU's antialiased four-grey text
    dr.rectangle((0, 0, img.width - 1, BAND_H - 1), fill=255)
    clock, date, temp, rh = band_texts(b)
    big, small = font(30), font(11, bold=False)
    for x, value, label in ((8, clock, date), (128, temp, "Temp"), (262, rh, "Humidity")):
        dr.text((x, 4), value, font=big, fill=0)
        dr.text((x, 41), label, font=small, fill=0)
    # The MCU uses two font planes; its monochrome path keeps the hi plane
    # (black/dark-grey become ink). Quantize only the live band, preserving
    # the frame body exactly as uploaded.
    band = img.crop((0, 0, img.width, BAND_H))
    if b.monochrome:
        band = band.point(lambda v: 0 if v < 128 else 255)
    else:
        band = band.point(lambda v: min((0, 85, 170, 255), key=lambda q: abs(q - v)))
    img.paste(band, (0, 0))


def render_png(slot: bytes, band: Band | None) -> bytes:
    """Slot image -> PNG of what the panel shows, upscaled SCALE x.

    `band` is drawn only into frames with the band flag (forecast layout)."""
    hdr, payload = parse_slot(slot, require_commit=False)
    img = decode_planes(payload, hdr.bpp)
    if band is not None and hdr.flags & FRAME_FLAG_BAND:
        draw_band(img, band)
    if SCALE != 1:
        img = img.resize((img.width * SCALE, img.height * SCALE), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
