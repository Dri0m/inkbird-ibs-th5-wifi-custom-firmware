"""Forecast screen (layout D of design/mockups) rendered to a 416x240 'L' image.

Rows y 0..55 stay pure white: the MCU draws its live band (clock, date,
indoor T/RH, battery) there. Everything below is drawn once per sync with
only the four levels 0/85/170/255. Shapes (icons, curve, fill, bars, rules)
are drawn SS x supersampled and box-filtered down, text at 1x with FreeType's
anti-aliasing (see Canvas); the grey edges are snapped to those levels at the
end. The image is also built to read as a plain threshold of
plane0 (grey >= 170 -> white), which is what the MCU shows when it is too
cold for greys: the light-grey fill and cloud bodies drop out, and the curve,
outlines, text and dark rain bars stay black.

Port of design/mockups/render_forecast.py (variant D_band48_icons). Pure
Python: stdlib + Pillow, fonts bundled in ./fonts (DejaVu Sans, Bitstream
Vera licence).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .frame import BAND_H, H, LEVELS, RULE_Y, W

K, D, L, WH = LEVELS                     # black, dark, light, white
FONT_DIR = Path(__file__).resolve().parent / "fonts"

HOURS = 48                               # chart span
ICONS_EVERY = 4                          # hours per icon
ICON_SIZE = 20
LABEL_EVERY = 6                          # hour labels
MARGIN = 4                               # left/right; the bezel needs >= 2
BOTTOM = H - 3                           # text baseline; keeps 2 px clear below

DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
COND_TEXT = {
    "sunny": "Sunny", "clear-night": "Clear", "partlycloudy": "Partly cloudy",
    "cloudy": "Cloudy", "fog": "Fog", "rainy": "Rain", "pouring": "Heavy rain",
    "snowy": "Snow", "snowy-rainy": "Sleet", "hail": "Hail",
    "lightning": "Thunder", "lightning-rainy": "Thunderstorm",
    "windy": "Windy", "windy-variant": "Windy", "exceptional": "Warning",
}
WET = ("rainy", "pouring", "lightning-rainy", "snowy", "snowy-rainy", "hail")


# --------------------------------------------------------------------------- model


@dataclass
class HourPoint:
    time: dt.datetime                    # aware, start of the hour
    temp: float                          # degC
    precip_mm: float = 0.0               # mm within this hour
    precip_prob: float | None = None     # %, optional (Met.no has none)
    condition: str = "cloudy"            # HA condition name
    is_day: bool | None = None           # None: decided from `sun`


@dataclass
class Now:
    outdoor_temp: float | None           # degC
    feels_like: float | None = None      # degC
    condition: str = "cloudy"
    precip_rate: float | None = None     # mm/h
    is_day: bool | None = None


@dataclass
class Forecast:
    location: str
    now: Now
    hourly: list[HourPoint]
    # (sunrise, sunset) per day as aware datetimes; optional
    sun: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


_fcache: dict = {}


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _fcache:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        _fcache[key] = ImageFont.truetype(str(FONT_DIR / name), size)
    return _fcache[key]


def text_w(s: str, f) -> int:
    l, _, r, _ = f.getbbox(s)
    return r - l


def fit_text(s: str, f, width: float) -> str:
    """s, or its longest prefix plus an ellipsis that fits `width` px; "" if
    not even the ellipsis fits."""
    if text_w(s, f) <= width:
        return s
    while s:
        s = s[:-1].rstrip()
        if s and text_w(s + "…", f) <= width:
            return s + "…"
    return "…" if text_w("…", f) <= width else ""


def num(v: float, digits: int) -> str:
    """Fixed-point text without a negative zero ("-0.0" -> "0.0")."""
    s = f"{v:.{digits}f}"
    return s[1:] if s.startswith("-") and float(s) == 0 else s


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


_Q4 = [min(LEVELS, key=lambda q: abs(q - v)) for v in range(256)]


def quant4(img: Image.Image) -> Image.Image:
    """Snap every pixel to the nearest of the four levels."""
    return img.point(_Q4)


def is_night(t: dt.datetime, is_day: bool | None, sun) -> bool:
    if is_day is not None:
        return not is_day
    for rise, sset in sun:
        if rise.date() == t.date():
            return not (rise <= t < sset)
    return not (7 <= t.hour < 17)        # no sun data: rough guess


SS = 4                                   # supersampling factor for shapes


class Canvas:
    """ImageDraw-like drawing onto a 1x 'L' image, with anti-aliased shapes.

    Shape calls take the same 1x coordinates as ImageDraw and are drawn SS x
    larger into a layer (premultiplied grey + coverage mask) that is
    box-filtered down and composited onto the image before the next text call
    and in finish(), so the drawing order (halos under the curve, labels over
    it) is kept. Integer axis-aligned lines and rectangles cover whole SS
    blocks, so rules and ticks stay solid. Text is drawn at 1x, hinted and
    anti-aliased by FreeType, which keeps small numbers sharper than
    downsampled large text."""

    def __init__(self, img: Image.Image, ss: int = SS):
        self.img, self.k = img, ss
        self.dr = ImageDraw.Draw(img)
        self.dr.fontmode = "L"              # anti-aliased, snapped to 4 levels later
        self._layer: tuple | None = None
        self._dirty = False

    # -- layer
    def _draws(self):
        if self._layer is None:
            size = (self.img.width * self.k, self.img.height * self.k)
            col, mask = Image.new("L", size, 0), Image.new("L", size, 0)
            self._layer = (col, mask, ImageDraw.Draw(col), ImageDraw.Draw(mask))
        self._dirty = True
        return self._layer[2], self._layer[3]

    def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        col, mask = self._layer[0], self._layer[1]
        bb = mask.getbbox()
        if bb is None:
            return
        k = self.k
        x0, y0 = bb[0] // k, bb[1] // k
        x1, y1 = -(-bb[2] // k), -(-bb[3] // k)
        ss = (x0 * k, y0 * k, x1 * k, y1 * k)
        c, m = col.crop(ss).reduce(k), mask.crop(ss).reduce(k)
        under = self.img.crop((x0, y0, x1, y1))
        # img * (1 - coverage) + premultiplied colour
        self.img.paste(ImageChops.add(ImageChops.multiply(under, ImageChops.invert(m)), c), (x0, y0))
        col.paste(0, ss)
        mask.paste(0, ss)

    finish = flush

    # -- coordinates
    def _p(self, v: float) -> float:
        """1x pixel-centre coordinate -> SS pixel-centre coordinate."""
        return (v + 0.5) * self.k - 0.5

    def _pts(self, xy) -> list[tuple[float, float]]:
        if xy and not isinstance(xy[0], (tuple, list)):
            xy = list(zip(xy[0::2], xy[1::2]))
        return [(self._p(x), self._p(y)) for x, y in xy]

    def _box(self, box) -> tuple[float, float, float, float]:
        """Inclusive 1x box (as ImageDraw fills it) -> inclusive SS box."""
        k = self.k
        x0, y0, x1, y1 = box
        return x0 * k, y0 * k, x1 * k + k - 1, y1 * k + k - 1

    # -- shapes
    def _paint(self, method: str, args, fill, **kw) -> None:
        dc, dm = self._draws()
        getattr(dc, method)(*args, fill=fill, **kw)
        getattr(dm, method)(*args, fill=255, **kw)

    def rectangle(self, box, fill) -> None:
        self._paint("rectangle", (self._box(box),), fill)

    def line(self, xy, fill, width: int = 1, joint=None) -> None:
        pts = self._pts(xy)
        if len(pts) == 2:
            (ax, ay), (bx, by) = xy if isinstance(xy[0], (tuple, list)) else (xy[:2], xy[2:])
            straight = ax == bx or ay == by
            if straight and all(float(v).is_integer() for v in (ax, ay, bx, by)):
                # as ImageDraw: width w at x covers x - (w-1)//2 .. + w-1
                o = (width - 1) // 2
                if ax == bx:
                    box = (ax - o, min(ay, by), ax - o + width - 1, max(ay, by))
                else:
                    box = (min(ax, bx), ay - o, max(ax, bx), ay - o + width - 1)
                self.rectangle(box, fill)
                return
        self._paint("line", (pts,), fill, width=round(width * self.k), joint=joint)

    def polygon(self, xy, fill) -> None:
        pts = self._pts(xy)
        # ImageDraw fills a polygon including its edge pixels, i.e. half a pixel
        # past the outline: a 1x-wide stroke along the edges adds that back.
        self._paint("polygon", (pts,), fill)
        self._paint("line", (pts + pts[:1],), fill, width=self.k, joint="curve")

    def ellipse(self, box, fill, outline=None, width: int = 1) -> None:
        b = self._box(box)
        self._paint("ellipse", (b,), fill)
        if outline is not None:
            self.arc(box, 0, 360, outline, width)

    def rounded_rectangle(self, box, radius: float, fill) -> None:
        self._paint("rounded_rectangle", (self._box(box),), fill, radius=radius * self.k)

    def arc(self, box, start: float, end: float, fill, width: int = 1) -> None:
        dc, dm = self._draws()
        b, w = self._box(box), round(width * self.k)
        dc.arc(b, start, end, fill=fill, width=w)
        dm.arc(b, start, end, fill=255, width=w)

    # -- text, at 1x over everything drawn so far
    def text(self, xy, s, font, fill=K, anchor=None, **kw) -> None:
        self.flush()
        self.dr.text(xy, s, font=font, fill=fill, anchor=anchor, **kw)


def halo_text(dr, xy, s, f, anchor="mm", fill=K, halo=WH, sw=2):
    dr.text(xy, s, font=f, fill=fill, anchor=anchor, stroke_width=sw, stroke_fill=halo)


# --------------------------------------------------------------------------- icons


def _cloud(dr, cx, cy, s, lw):
    """Cloud centred at (cx, cy), about s px wide: black outline, light body."""
    blobs = [(-0.22, 0.08, 0.20), (0.02, -0.08, 0.27), (0.25, 0.08, 0.19)]
    base = (cx - 0.42 * s, cy + 0.02 * s, cx + 0.44 * s, cy + 0.28 * s)
    for pad, col in ((lw, K), (0, L)):
        for bx, by, r in blobs:
            R = r * s + pad
            dr.ellipse((cx + bx * s - R, cy + by * s - R, cx + bx * s + R, cy + by * s + R), fill=col)
        dr.rounded_rectangle((base[0] - pad, base[1] - pad, base[2] + pad, base[3] + pad),
                             radius=0.13 * s + pad, fill=col)


def _sun(dr, cx, cy, s, lw):
    r = 0.2 * s
    for i in range(8):
        a = i * math.pi / 4
        dr.line((cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45,
                 cx + math.cos(a) * r * 2.15, cy + math.sin(a) * r * 2.15), fill=K, width=lw)
    dr.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WH, outline=K, width=lw)


def _moon(dr, cx, cy, s, lit="left"):
    """Crescent; `lit` is the side that stays black."""
    r = 0.24 * s
    dr.ellipse((cx - r, cy - r, cx + r, cy + r), fill=K)
    o = 0.32 * r * (1 if lit == "left" else -1)
    r2 = r * 0.95
    dr.ellipse((cx - r2 + o, cy - r2 - abs(o) * 0.8, cx + r2 + o, cy + r2 - abs(o) * 0.8), fill=WH)


def icon(dr, cond: str, cx, cy, s, night=False):
    """Weather icon for an HA condition name, roughly s x s px."""
    lw = max(1, round(s / 11))
    c = (cond or "cloudy").replace("_", "-")
    if c in ("sunny", "clear-night"):
        if night or c == "clear-night":
            _moon(dr, cx, cy, s * 1.5)
        else:
            _sun(dr, cx, cy, s * 1.05, lw)
        return
    if c == "partlycloudy":
        if night:
            _moon(dr, cx + 0.18 * s, cy - 0.16 * s, s * 1.45, lit="right")
        else:
            _sun(dr, cx + 0.16 * s, cy - 0.17 * s, s * 0.85, lw)
        _cloud(dr, cx - 0.04 * s, cy + 0.08 * s, s * 0.85, lw)
        return
    if c == "fog":
        for i, (a, b) in enumerate(((-0.42, 0.30), (-0.30, 0.42), (-0.42, 0.42))):
            y = cy - 0.22 * s + i * 0.22 * s
            dr.line((cx + a * s, y, cx + b * s, y), fill=K, width=lw + 1)
        return
    if c in ("windy", "windy-variant"):
        for i, (a, b) in enumerate(((-0.42, 0.25), (-0.42, 0.40), (-0.42, 0.15))):
            y = cy - 0.22 * s + i * 0.22 * s
            dr.line((cx + a * s, y, cx + b * s, y), fill=K, width=lw)
            if i < 2:                       # curl at the end of the upper gusts
                dr.arc((cx + b * s - 3, y - 5, cx + b * s + 3, y + 1), 270, 90, fill=K, width=lw)
        return
    wet = c in WET
    cyc = cy - (0.16 * s if wet or c == "lightning" else 0)
    _cloud(dr, cx, cyc, s * (0.9 if wet else 1), lw)
    if c in ("rainy", "pouring", "lightning-rainy", "snowy-rainy"):
        n = 4 if c == "pouring" else 3
        for i in range(n):
            x = cx + (i - (n - 1) / 2) * 0.24 * s
            y0 = cyc + 0.38 * s
            dr.line((x, y0, x - 0.1 * s, y0 + 0.28 * s), fill=K, width=lw + 1)
    if c in ("snowy", "snowy-rainy", "hail"):
        for i in range(3):
            x = cx + (i - 1) * 0.24 * s
            y0 = cyc + 0.44 * s
            r = max(1.2, 0.05 * s)
            dr.ellipse((x - r, y0 - r, x + r, y0 + r), fill=K)
    if c in ("lightning-rainy", "lightning"):
        dr.polygon([(cx + 0.02 * s, cyc + 0.2 * s), (cx - 0.08 * s, cyc + 0.42 * s),
                    (cx + 0.02 * s, cyc + 0.42 * s), (cx - 0.04 * s, cyc + 0.6 * s),
                    (cx + 0.12 * s, cyc + 0.36 * s), (cx + 0.03 * s, cyc + 0.36 * s)], fill=K)


# --------------------------------------------------------------------------- chart maths


def monotone(xs, ys, samples_per_seg=12):
    """Fritsch-Carlson monotone cubic: no overshoot past hourly extrema."""
    n = len(xs)
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [d[0]] + [0.0] * (n - 2) + [d[-1]]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        h = a * a + b * b
        if h > 9:
            tau = 3 / math.sqrt(h)
            m[i], m[i + 1] = tau * a * d[i], tau * b * d[i]
    pts = []
    for i in range(n - 1):
        hx = xs[i + 1] - xs[i]
        for k in range(samples_per_seg):
            t = k / samples_per_seg
            h00, h10 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t
            h01, h11 = -2 * t**3 + 3 * t**2, t**3 - t**2
            pts.append((xs[i] + t * hx, h00 * ys[i] + h10 * hx * m[i] + h01 * ys[i + 1] + h11 * hx * m[i + 1]))
    pts.append((xs[-1], ys[-1]))
    return pts


def extrema(vals, min_prom):
    """(index, 'max'|'min') of turning points whose rise or fall to the
    neighbouring turning point (or end) is at least min_prom."""
    turning = []
    for i in range(1, len(vals) - 1):
        a, b, c = vals[i - 1], vals[i], vals[i + 1]
        if (b >= a and b > c) or (b > a and b >= c):
            turning.append((i, "max"))
        elif (b <= a and b < c) or (b < a and b <= c):
            turning.append((i, "min"))
    out = []
    for j, (i, kind) in enumerate(turning):
        pv = vals[turning[j - 1][0]] if j > 0 else vals[0]
        nv = vals[turning[j + 1][0]] if j + 1 < len(turning) else vals[-1]
        if abs(vals[i] - pv) >= min_prom or abs(vals[i] - nv) >= min_prom:
            out.append((i, kind))
    return out


def block_condition(hs: list[HourPoint], a: int, b: int) -> HourPoint:
    """Representative hour for an icon block [a, b): the wettest hour if any
    rain is forecast in it, else the middle hour."""
    blk = hs[a:b]
    wet = [h for h in blk if h.condition in WET or h.precip_mm > 0.1]
    if wet:
        return max(wet, key=lambda h: h.precip_mm)
    return blk[len(blk) // 2]


# --------------------------------------------------------------------------- sections


def outdoor_header(dr, fc: Forecast, now: dt.datetime, box, conv, unit_deg):
    x0, y0, x1, y1 = box
    n = fc.now
    cy = (y0 + y1) / 2
    s = (y1 - y0) - 2
    icon(dr, n.condition, x0 + s / 2 + 1, cy, s, is_night(now, n.is_day, fc.sun))
    x = x0 + s + 6
    f = font(24)
    v = "--" if n.outdoor_temp is None else f"{num(conv(n.outdoor_temp), 1)}{unit_deg}"
    dr.text((x, cy + 9), v, font=f, fill=K, anchor="ls")
    x += text_w(v, f) + 8
    fs = font(13)
    cond = COND_TEXT.get(n.condition, (n.condition or "").replace("-", " ").capitalize())
    dr.text((x, cy - 2), cond, font=fs, fill=K, anchor="ls")
    feels = None if n.feels_like is None else f"feels {num(conv(n.feels_like), 0)}°"
    if feels:
        dr.text((x, cy + 12), feels, font=fs, fill=K, anchor="ls")
    # right column: location and precip rate, each only as wide as the free
    # space beside the text on its row (location truncated, rate dropped)
    xr = x1 - 2
    loc = fit_text(fc.location, fs, xr - (x + text_w(cond, fs) + 8))
    if loc:
        dr.text((xr, cy - 2), loc, font=fs, fill=K, anchor="rs")
    if n.precip_rate is not None:
        rate = f"{n.precip_rate:.1f} mm/h"
        if text_w(rate, fs) <= xr - (x + (text_w(feels, fs) if feels else 0) + 8):
            dr.text((xr, cy + 12), rate, font=fs, fill=K, anchor="rs")


def chart(dr, fc: Forecast, hs: list[HourPoint], box, conv, fahrenheit: bool):
    """box = whole chart area incl. icon row and x axis; text baseline at y1."""
    x0, y0, x1, y1 = box
    n = len(hs) - 1
    # Place points by elapsed time, so a missing hour (null data, a non-hourly
    # tail) leaves a visible gap instead of squeezing the axis.
    # (timestamps: subtracting datetimes that share a ZoneInfo uses wall time,
    # which is wrong across the autumn fold)
    pos = [(h.time.timestamp() - hs[0].time.timestamp()) / 3600 for h in hs]
    pxh = (x1 - x0) / pos[-1]
    X = lambda i: x0 + pos[i] * pxh  # noqa: E731
    f_lab, f_day, f_val = font(12), font(13), font(13)

    axis_h = 16
    top = y0 + ICON_SIZE + 3
    py0, py1 = top + 14, y1 - axis_h       # plot area; 14 px headroom for max labels

    # temperature scale: curve in the upper part, rain bars in the bottom ~40 %
    temps = [conv(h.temp) for h in hs]
    tmin, tmax = min(temps), max(temps)
    span = max(tmax - tmin, 7.0 if fahrenheit else 4.0)
    lo, hi = tmin - span * 0.75, tmax + span * 0.05
    Y = lambda v: py1 - (v - lo) / (hi - lo) * (py1 - py0)  # noqa: E731
    mids = [i for i, h in enumerate(hs) if h.time.hour == 0 and i > 0]

    # filled area, then midnight lines over the fill and under the curve
    pts = monotone([X(i) for i in range(n + 1)], [Y(t) for t in temps])
    dr.polygon([(x0, py1)] + pts + [(x1, py1)], fill=L)
    for i in mids:
        x = round(X(i))
        dr.line((x, top + 2, x, py1), fill=K, width=1)

    # precipitation amount bars (dark grey, 1 px white gap) + total per spell
    placed: list[tuple[float, float, float]] = []   # (x, y, half width) of labels
    mm_max = max(2.0, max(h.precip_mm for h in hs))
    ph = (py1 - py0) * 0.42
    bar_h = lambda mm: max(3, round(mm / mm_max * ph))  # noqa: E731
    for i, h in enumerate(hs[:-1]):
        if h.precip_mm <= 0.04:
            continue
        bh = bar_h(h.precip_mm)
        bx0, bx1 = round(X(i)) + 1, round(X(i + 1)) - 1
        if bx1 - bx0 < 2:
            bx1 = bx0 + 2
        dr.rectangle((bx0 - 1, py1 - bh - 1, bx1 + 1, py1), fill=WH)
        dr.rectangle((bx0, py1 - bh, bx1, py1), fill=D)
    i = 0
    while i < n:
        if hs[i].precip_mm <= 0.04:
            i += 1
            continue
        a = i
        while i < n and hs[i].precip_mm > 0.04:
            i += 1
        tot = sum(h.precip_mm for h in hs[a:i])
        if tot < 0.15:                      # rounds to 0.1 or less: not worth a label
            continue
        lab = f"{tot:.1f} mm"
        peak = max(bar_h(h.precip_mm) for h in hs[a:i])
        ly = py1 - max(4, peak // 2) + 4
        lw = text_w(lab, f_lab)
        if X(i) + 4 + lw < x1:
            lx, anc, cx = X(i) + 4, "ls", X(i) + 4 + lw / 2
        else:
            lx = min(max(X(a) - 4, x0 + lw + 2), x1 - 2)    # a spell from x0 on: keep it on the canvas
            anc, cx = "rs", lx - lw / 2
        if any(abs(cx - px) < (lw / 2 + pw + 4) and abs(ly - py) < 14 for px, py, pw in placed):
            continue
        placed.append((cx, ly, lw / 2))
        halo_text(dr, (lx, ly), lab, f_lab, anchor=anc)

    # curve and baseline on top
    dr.line(pts, fill=K, width=3, joint="curve")
    dr.line((x0, py1, x1, py1), fill=K, width=1)

    # min / max labels, plus the value at the right end (as Klara does)
    def put(i, kind):
        s = f"{round(temps[i])}"
        w2 = text_w(s, f_val) / 2
        x = min(max(X(i), x0 + w2 + 2), x1 - w2 - 2)
        y = Y(temps[i]) - 7 if kind == "max" else Y(temps[i]) + 17
        if any(abs(x - px) < (w2 + pw + 6) and abs(y - py) < 14 for px, py, pw in placed):
            return
        placed.append((x, y, w2))
        halo_text(dr, (x, y), s, f_val, anchor="ms")

    for i, kind in extrema(temps, 2.0 if fahrenheit else 1.0):
        put(i, kind)
    put(n, "max" if temps[n] >= temps[n - 1] else "min")

    # x axis: midnight ticks + day names, hour labels every 6 h, minor ticks
    ay = y1 - axis_h
    day_spans = []
    for i in mids:
        x = round(X(i))
        lab = DOW[hs[i].time.weekday()]
        w = text_w(lab, f_day)
        if x + 3 + w <= x1 + 1:
            dr.text((x + 3, y1), lab, font=f_day, fill=K, anchor="ls")
            day_spans.append((x - 2, x + 3 + w + 3))
        else:                               # no room right of the line
            day_spans.append((x - 2, x + 2))
    for i, h in enumerate(hs):
        x = round(X(i))
        hr = h.time.hour
        if hr == 0:
            dr.line((x, ay, x, ay + 7), fill=K, width=2)
        elif hr % LABEL_EVERY == 0:
            dr.line((x, ay, x, ay + 4), fill=K, width=1)
            w = text_w(str(hr), f_lab)
            if x - w / 2 >= x0 and x + w / 2 <= x1 and \
                    not any(a < x + w / 2 and x - w / 2 < b for a, b in day_spans):
                dr.text((x, y1), f"{hr}", font=f_lab, fill=K, anchor="ms")
        else:
            dr.line((x, ay, x, ay + 1), fill=K, width=1)

    # icon row: one per 4 h block, centred on the block
    cy = y0 + ICON_SIZE / 2
    for a in range(0, n, ICONS_EVERY):
        cx = X(a) + pxh * ICONS_EVERY / 2
        if cx + ICON_SIZE / 2 > x1 + 2:
            break
        h = block_condition(hs, a, min(a + ICONS_EVERY, n))
        icon(dr, h.condition, cx, cy, ICON_SIZE, is_night(h.time, h.is_day, fc.sun))


# --------------------------------------------------------------------------- entry


def select_hours(fc: Forecast, now: dt.datetime, hours: int = HOURS,
                 tz: ZoneInfo | None = None) -> list[HourPoint]:
    """Points from the one in force at `now` (the last with time <= now, so a
    half-hour zone keeps its current hour; the first point if all are later),
    in `tz` (the location's ZoneInfo, default now's: a fixed offset gets
    midnights and DST wrong), at most `hours` hours after it. Duplicate
    timestamps and non-finite temperatures are dropped."""
    tz = tz or now.tzinfo
    seen, pts = set(), []
    for h in sorted(fc.hourly, key=lambda h: h.time.timestamp()):
        ts = h.time.timestamp()
        if ts in seen or not math.isfinite(h.temp):
            continue
        seen.add(ts)
        pts.append(h)
    t_now = now.timestamp()
    first = max((i for i, h in enumerate(pts) if h.time.timestamp() <= t_now), default=0)
    out = []
    for h in pts[first:]:
        if out and h.time.timestamp() - out[0].time.timestamp() > hours * 3600:
            break
        out.append(HourPoint(h.time.astimezone(tz), h.temp, h.precip_mm or 0.0, h.precip_prob,
                             h.condition, h.is_day))
    return out


def render_checked(fc: Forecast, units: str = "C", now: dt.datetime | None = None,
                   *, tz: ZoneInfo) -> tuple[Image.Image, str | None]:
    """Layout D, 416x240 'L', levels 0/85/170/255 only, rows 0..55 white.

    -> (image, problem). Bad forecast data (too few usable points, or numbers
    the chart maths cannot handle) gives the "No forecast data" frame and a
    problem text. `tz` is the location's ZoneInfo, `now` aware."""
    now = dt.datetime.now(tz) if now is None else now.astimezone(tz)
    fahrenheit = units == "F"
    conv = c_to_f if fahrenheit else (lambda c: c)

    def base():
        img = Image.new("L", (W, H), WH)
        dr = Canvas(img)
        dr.rectangle((0, RULE_Y, W - 1, RULE_Y + 1), fill=K)
        outdoor_header(dr, fc, now, (MARGIN, 62, W - MARGIN, 92), conv, "°")
        return img, dr

    img, dr = base()
    hs = select_hours(fc, now, tz=tz)
    problem = None
    if len(hs) < 3:
        problem = f"only {len(hs)} usable forecast points"
    else:
        try:
            chart(dr, fc, hs, (MARGIN, 96, W - MARGIN, BOTTOM), conv, fahrenheit)
        except (ArithmeticError, ValueError) as e:     # e.g. NaN precipitation
            problem = f"chart failed on the forecast data: {e!r}"
            img, dr = base()                # drop the half-drawn chart
    if problem:
        dr.text((W // 2, 170), "No forecast data", font=font(16), fill=K, anchor="mm")
    dr.finish()
    img = quant4(img)
    img.paste(WH, (0, 0, W, BAND_H))        # the band is the MCU's
    return img, problem


def render(fc: Forecast, units: str = "C", now: dt.datetime | None = None,
           *, tz: ZoneInfo) -> Image.Image:
    """render_checked without the problem text."""
    return render_checked(fc, units, now, tz=tz)[0]
