"""Frame format and forecast renderer tests.

    uv run --with pytest --with pillow pytest ha/tests
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image
from zoneinfo import ZoneInfo

HA = Path(__file__).resolve().parent.parent
ROOT = HA.parent
sys.path.insert(0, str(HA.parent / "custom_components"))

from th5_display import forecast, frame, sources  # noqa: E402

FRAME_H = ROOT / "firmware" / "src" / "frame.h"
TZ = ZoneInfo("Europe/Oslo")
SAMPLE = HA / "tests" / "sample_forecast.json"


def four_bands() -> Image.Image:
    """x 0..103 white, 104..207 light, 208..311 dark, 312..415 black."""
    img = Image.new("L", (frame.W, frame.H), 255)
    for i, v in enumerate((255, 170, 85, 0)):
        img.paste(v, (i * 104, 0, (i + 1) * 104, frame.H))
    return img


# --------------------------------------------------------------------------- constants


def test_constants_match_frame_h():
    src = FRAME_H.read_text()
    defs = {m[1]: m[2] for m in re.finditer(r"#define\s+(FRAME_\w+)\s+(.+?)\s*(?:/\*.*)?$", src, re.M)}

    def val(name):
        return eval(re.sub(r"\b(0x[0-9a-fA-F]+|\d+)u\b", r"\1", defs[name]))  # drop C "u" suffixes

    for name in ("FRAME_MAGIC", "FRAME_VERSION", "FRAME_PLANE_BYTES", "FRAME_HDR_BYTES",
                 "FRAME_FLAG_BAND", "FRAME_COMMITTED", "FRAME_BAND_BYTES",
                 "FRAME_SLOT_A", "FRAME_SLOT_B", "FRAME_SLOT_BYTES"):
        assert getattr(frame, name) == val(name), name
    assert frame.FRAME_SLOT_A + frame.FRAME_SLOT_BYTES == frame.FRAME_SLOT_B
    assert frame.FRAME_HDR_BYTES + 2 * frame.FRAME_PLANE_BYTES <= frame.FRAME_SLOT_BYTES
    assert struct.pack("<I", frame.FRAME_MAGIC) == b"TH5F"


# --------------------------------------------------------------------------- planes


def test_four_band_vector():
    p = frame.encode_planes(four_bands(), 2)
    assert len(p) == 2 * frame.FRAME_PLANE_BYTES
    p0, p1 = p[:frame.FRAME_PLANE_BYTES], p[frame.FRAME_PLANE_BYTES:]
    for x in range(frame.W):
        lvl = 3 - x // 104                       # 3 white .. 0 black
        r0 = p0[x * 30:(x + 1) * 30]
        r1 = p1[x * 30:(x + 1) * 30]
        assert r0 == (b"\xff" if lvl >> 1 else b"\x00") * 30, x
        assert r1 == (b"\xff" if lvl & 1 else b"\x00") * 30, x
    # 1 bpp is plane0 alone
    assert frame.encode_planes(four_bands(), 1) == p0


def test_bit_order_single_pixel():
    img = Image.new("L", (frame.W, frame.H), 255)
    img.putpixel((5, 13), 0)                     # landscape x 5, y 13 = byte 1 bit 7-5
    img.putpixel((415, 239), 85)                 # last pixel, dark: p0 = 0, p1 = 1
    p = frame.encode_planes(img, 2)
    p0, p1 = p[:frame.FRAME_PLANE_BYTES], p[frame.FRAME_PLANE_BYTES:]
    assert p0[5 * 30 + 1] == 0xFF & ~(1 << 2)
    assert p1[5 * 30 + 1] == 0xFF & ~(1 << 2)
    assert p0[415 * 30 + 29] == 0xFE and p1[415 * 30 + 29] == 0xFF
    assert sum(b != 0xFF for b in p0) == 2 and sum(b != 0xFF for b in p1) == 1


def test_roundtrip():
    img = four_bands()
    for y in range(0, frame.H, 7):               # some structure along y too
        img.putpixel((y + 3, y), 0)
        img.putpixel((y + 150, y), 255)
    assert list(frame.decode_planes(frame.encode_planes(img, 2), 2).tobytes()) == list(img.tobytes())
    mono = frame.decode_planes(frame.encode_planes(img, 1), 1)
    assert list(mono.tobytes()) == [255 if v >= 170 else 0 for v in img.tobytes()]


def test_encode_snaps_to_nearest_level():
    img = Image.new("L", (frame.W, frame.H), 0)
    for i, v in enumerate((40, 50, 120, 200, 250)):
        img.paste(v, (i * 80, 0, (i + 1) * 80, frame.H))
    got = frame.decode_planes(frame.encode_planes(img, 2), 2)
    assert [got.getpixel((i * 80, 0)) for i in range(5)] == [0, 85, 85, 170, 255]


# --------------------------------------------------------------------------- slot


def test_header_layout():
    payload = frame.encode_planes(four_bands(), 2)
    s = frame.build_slot(payload, 2, flags=frame.FRAME_FLAG_BAND, seq=0x01020304, frame_id=0x1122334455667788)
    assert len(s) == 32 + 24960
    assert s[0:4] == b"TH5F"
    assert s[4] == 1 and s[5] == 2 and s[6] == 0 and s[7] == 0x01
    assert s[8:12] == bytes((4, 3, 2, 1))
    assert s[12:20] == bytes.fromhex("8877665544332211")      # id_lo then id_hi, LE
    assert s[20:24] == (24960).to_bytes(4, "little")
    assert s[24:28] == zlib.crc32(payload).to_bytes(4, "little")
    assert s[28:32] == b"\x00\x00\x00\x00"
    assert s[32:] == payload
    u = frame.build_slot(payload, 2, commit=False)
    assert u[28:32] == b"\xff\xff\xff\xff"


def test_crc_and_frame_id():
    payload = frame.encode_planes(four_bands(), 1)
    s = frame.build_slot(payload, 1)
    h, pl = frame.parse_slot(s)
    assert pl == payload and h.bpp == 1 and h.length == 12480 and h.committed
    assert h.crc32 == zlib.crc32(payload)
    assert h.frame_id == int.from_bytes(hashlib.sha256(b"\x01\x00" + payload).digest()[:8], "little")
    # trailing erased flash is fine
    frame.parse_slot(s + b"\xff" * (frame.FRAME_SLOT_BYTES - len(s)))


@pytest.mark.parametrize("off,val,msg", [(0, 0x00, "magic"), (4, 2, "version"), (5, 3, "bpp"),
                                         (6, 1, "encoding"), (20, 0, "length"), (24, 0x55, "CRC"),
                                         (100, 0x00, "CRC")])
def test_parse_rejects(off, val, msg):
    s = bytearray(frame.build_slot(frame.encode_planes(four_bands(), 2), 2))
    s[off] ^= val or 0xFF
    with pytest.raises(ValueError, match=msg):
        frame.parse_slot(bytes(s))


def test_parse_uncommitted():
    s = frame.build_slot(frame.encode_planes(four_bands(), 2), 2, commit=False)
    with pytest.raises(ValueError, match="committed"):
        frame.parse_slot(s)
    h, _ = frame.parse_slot(s, require_commit=False)
    assert not h.committed


# --------------------------------------------------------------------------- forecast


@pytest.fixture(scope="module")
def sample():
    d = json.loads(SAMPLE.read_text())
    return sources.from_sample(d), dt.datetime.fromisoformat(d["now"]["time"])


@pytest.mark.parametrize("units", ["C", "F"])
def test_forecast_levels_and_band(sample, units):
    fc, now = sample
    img = forecast.render(fc, units, now, tz=TZ)
    assert img.mode == "L" and img.size == (416, 240)
    assert set(img.tobytes()) <= set(frame.LEVELS)
    assert set(img.tobytes()) == set(frame.LEVELS)            # all four used
    band = img.crop((0, 0, 416, frame.BAND_H))
    assert set(band.tobytes()) == {255}
    # 2 px rule under the band belongs to the frame
    assert set(img.crop((0, 57, 416, 59)).tobytes()) == {0}
    # margins: 2 px left/right/bottom stay white
    for box in ((0, 59, 2, 240), (414, 59, 416, 240), (0, 238, 416, 240)):
        assert set(img.crop(box).tobytes()) == {255}, box
    # the encoded band bytes are all white in both planes
    p = frame.encode_planes(img, 2)
    for x in range(416):
        for pl in (0, 1):
            off = pl * frame.FRAME_PLANE_BYTES + x * 30
            assert p[off:off + frame.FRAME_BAND_BYTES] == b"\xff" * 7


def test_canvas_antialiases_shapes_keeps_rules_solid():
    from PIL import ImageDraw
    img = Image.new("L", (40, 40), 255)
    cv = forecast.Canvas(img)
    cv.line((3, 30, 36, 5), fill=forecast.K, width=3)                  # diagonal: grey edges
    cv.line((20, 2, 20, 37), fill=forecast.K, width=1)                 # rules: exact pixels
    cv.line((2, 35, 37, 35), fill=forecast.K, width=2)
    cv.rectangle((5, 5, 9, 9), fill=forecast.D)
    cv.finish()
    ref = Image.new("L", (40, 40), 255)
    dr = ImageDraw.Draw(ref)
    dr.line((20, 2, 20, 37), fill=0, width=1)
    dr.line((2, 35, 37, 35), fill=0, width=2)
    for box in ((20, 2, 21, 38), (2, 35, 38, 37)):
        assert img.crop(box).tobytes() == ref.crop(box).tobytes(), box
    assert set(img.crop((5, 5, 10, 10)).tobytes()) == {forecast.D}
    assert set(img.crop((4, 4, 11, 11)).tobytes()) == {forecast.D, 255}  # no fringe
    edge = set(forecast.quant4(img).crop((3, 5, 19, 31)).tobytes())
    assert {forecast.K, forecast.WH} < edge and edge & {forecast.D, forecast.L}


def test_fahrenheit_conversion(sample):
    fc, now = sample
    assert forecast.c_to_f(0) == 32 and forecast.c_to_f(100) == 212 and forecast.c_to_f(-40) == -40
    c = forecast.render(fc, "C", now, tz=TZ)
    f = forecast.render(fc, "F", now, tz=TZ)
    assert c.tobytes() != f.tobytes()
    # the header shows the converted outdoor value: 8.4 degC -> "47.1°"
    from PIL import ImageDraw
    img = Image.new("L", (416, 40), 255)
    forecast.outdoor_header(ImageDraw.Draw(img), fc, now, (4, 4, 412, 34), forecast.c_to_f, "°")
    ref = Image.new("L", (416, 40), 255)
    ImageDraw.Draw(ref).text((4 + 28 + 6, 19 + 9), "47.1°", font=forecast.font(24), fill=0, anchor="ls")
    w = forecast.text_w("47.1°", forecast.font(24))
    assert img.crop((38, 0, 38 + w, 40)).tobytes() == ref.crop((38, 0, 38 + w, 40)).tobytes()


def test_hours_start_at_current_hour(sample):
    fc, now = sample
    later = now + dt.timedelta(hours=5)
    hs = forecast.select_hours(fc, later, tz=TZ)
    assert hs[0].time == later.replace(minute=0, second=0, microsecond=0)
    assert len(hs) <= forecast.HOURS + 1


def test_open_meteo_adapter():
    d = {
        "timezone": "Europe/Oslo", "utc_offset_seconds": 3600,
        "current": {"time": "2026-11-01T08:00", "temperature_2m": 6.7, "apparent_temperature": 4.4,
                    "precipitation": 0.2, "weather_code": 61, "is_day": 1},
        "hourly": {"time": ["2026-11-01T08:00", "2026-11-01T09:00", "2026-11-01T10:00"],
                   "temperature_2m": [6.7, 7.0, 7.5], "apparent_temperature": [4.4, 5, 5.5],
                   "precipitation": [0.0, 1.5, 0.3], "precipitation_probability": [None, 80, None],
                   "weather_code": [0, 63, 2], "is_day": [0, 1, 1]},
        "daily": {"sunrise": ["2026-11-01T06:40"], "sunset": ["2026-11-01T16:32"]},
    }
    fc = sources.from_open_meteo(d)
    assert [h.precip_mm for h in fc.hourly] == [1.5, 0.3, 0.0]     # preceding-hour sums shifted
    assert [h.precip_prob for h in fc.hourly] == [80, None, None]
    assert [h.condition for h in fc.hourly] == ["clear-night", "rainy", "partlycloudy"]
    assert fc.hourly[0].time.utcoffset() == dt.timedelta(hours=1)
    assert fc.now.condition == "rainy" and fc.now.precip_rate == 0.2
    assert fc.sun[0][0].hour == 6


def test_ha_adapter_converts_units():
    resp = {"weather.home": {"forecast": [
        {"datetime": "2026-11-01T08:00:00+00:00", "temperature": 50, "precipitation": 0.1,
         "precipitation_probability": None, "condition": "rainy"},
        {"datetime": "2026-11-01T07:00:00+00:00", "temperature": 32, "precipitation": 0,
         "condition": "cloudy"}]}}
    attrs = {"temperature": 41, "apparent_temperature": 32, "temperature_unit": "°F",
             "precipitation_unit": "in", "friendly_name": "Home"}
    fc = sources.from_ha_forecast(resp, attrs, condition="cloudy")
    assert [round(h.temp, 3) for h in fc.hourly] == [0, 10]          # sorted, converted
    assert fc.hourly[1].precip_mm == pytest.approx(2.54)
    assert fc.now.outdoor_temp == pytest.approx(5) and fc.now.feels_like == pytest.approx(0)
    assert fc.location == "Home"


def test_open_meteo_dst_fold_and_axis():
    """Open-Meteo writes one fixed offset for the whole response, even across
    the autumn DST change; parsed hours must be 1 h apart in real time and the
    second midnight must fall on local midnight."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    from th5_display import forecast, sources
    tz = ZoneInfo("Europe/Oslo")
    t0 = dt.datetime(2026, 10, 24, 12)  # +02:00 at the start, change at 25 Oct 03:00
    times = [(t0 + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(50)]
    d = {"timezone": "Europe/Oslo", "utc_offset_seconds": 7200,
         "hourly": {"time": times, "temperature_2m": [5.0 + (i % 7) for i in range(50)]}}
    fc = sources.from_open_meteo(d)
    ts = [h.time.timestamp() for h in fc.hourly]
    assert all(b - a == 3600 for a, b in zip(ts, ts[1:]))
    now = dt.datetime(2026, 10, 24, 12, tzinfo=tz)
    hs = forecast.select_hours(fc, now, tz=tz)
    assert [h.time.isoformat() for h in hs if h.time.hour == 0] == \
        ["2026-10-25T00:00:00+02:00", "2026-10-26T00:00:00+01:00"]
    forecast.render(fc, "C", now=now, tz=tz)  # must not crash on the repeated 02:00


def test_open_meteo_short_and_null():
    from th5_display import sources
    fc = sources.from_open_meteo({"timezone": "UTC", "hourly": {
        "time": ["2026-11-01T00:00", "2026-11-01T01:00"], "temperature_2m": [1.0]}})
    assert len(fc.hourly) == 1
    fc = sources.from_open_meteo({"timezone": "UTC", "hourly": {
        "time": ["2026-11-01T00:00"], "temperature_2m": [None]}})
    assert fc.hourly == [] and fc.now.outdoor_temp is None


# --------------------------------------------------------------------------- review fixes


def mkfc(times, temps=None, **kw):
    temps = temps or [5.0 + (i % 5) for i in range(len(times))]
    return forecast.Forecast(
        "Oslo", forecast.Now(5.0), [forecast.HourPoint(t, v) for t, v in zip(times, temps)], **kw)


def hours(n, start=dt.datetime(2026, 11, 1, 8, tzinfo=TZ), step=1):
    return [start + dt.timedelta(hours=step * i) for i in range(n)]


def test_frame_id_covers_bpp_flags_and_is_nonzero():
    p = frame.encode_planes(four_bands(), 1)
    ids = {frame.frame_id_of(p, 1, 0), frame.frame_id_of(p, 2, 0), frame.frame_id_of(p, 1, 1)}
    assert len(ids) == 3
    a = frame.parse_slot(frame.build_slot(p, 1, flags=0))[0].frame_id
    b = frame.parse_slot(frame.build_slot(p, 1, flags=frame.FRAME_FLAG_BAND))[0].frame_id
    assert a != b
    with pytest.raises(ValueError, match="frame_id"):
        frame.build_slot(p, 1, frame_id=0)


def test_frame_id_zero_hash_maps_to_one(monkeypatch):
    class Z:
        def __init__(self, *a): pass
        def digest(self): return bytes(32)
    monkeypatch.setattr(frame.hashlib, "sha256", Z)
    assert frame.frame_id_of(b"x", 1) == 1


def test_duplicate_timestamps_do_not_crash():
    ts = hours(6)
    fc = mkfc([ts[0], ts[1], ts[1], ts[2], ts[3], ts[3], ts[4], ts[5]])
    hs = forecast.select_hours(fc, ts[0], tz=TZ)
    assert [h.time for h in hs] == ts
    img, problem = forecast.render_checked(fc, "C", ts[0], tz=TZ)
    assert problem is None and set(img.tobytes()) > {0}


def test_bad_data_falls_back_to_no_forecast_frame():
    ts = hours(2)
    for fc in (mkfc(ts), mkfc(hours(6), [float("nan")] * 6)):
        img, problem = forecast.render_checked(fc, "C", ts[0], tz=TZ)
        assert problem and "usable" in problem
        assert img.size == (416, 240) and set(img.crop((0, 0, 416, 56)).tobytes()) == {255}
    # same frame as the plain render, which does not raise either
    assert forecast.render(mkfc(ts), "C", ts[0], tz=TZ).tobytes() == img.tobytes()
    # a failure inside the chart is reported too
    fc = mkfc(hours(6))
    fc.hourly[2].precip_mm = float("nan")
    fc.hourly[3].precip_mm = float("inf")
    img, problem = forecast.render_checked(fc, "C", hours(1)[0], tz=TZ)
    assert img.size == (416, 240)


def test_tz_is_required():
    fc = mkfc(hours(6))
    now = fc.hourly[0].time
    with pytest.raises(TypeError):
        forecast.render(fc, "C", now)
    assert forecast.select_hours(fc, now)          # now's own ZoneInfo is the default


def test_half_hour_zone_keeps_current_hour():
    kolkata = ZoneInfo("Asia/Kolkata")
    start = dt.datetime(2026, 11, 1, 6, 30, tzinfo=kolkata)      # points on :30 local
    fc = mkfc(hours(10, start))
    now = dt.datetime(2026, 11, 1, 9, 45, tzinfo=kolkata)
    hs = forecast.select_hours(fc, now, tz=kolkata)
    assert hs[0].time == dt.datetime(2026, 11, 1, 9, 30, tzinfo=kolkata)
    # all points in the future: start from the first
    early = dt.datetime(2026, 11, 1, 1, 0, tzinfo=kolkata)
    assert forecast.select_hours(fc, early, tz=kolkata)[0].time == start


def header(fc, width=408):
    img = Image.new("L", (416, 40), 255)
    forecast.outdoor_header(forecast.ImageDraw.Draw(img), fc, dt.datetime(2026, 11, 1, 8, tzinfo=TZ),
                            (4, 4, 4 + width, 34), lambda c: c, "°")
    return img


def test_header_negative_zero():
    assert forecast.num(-0.04, 1) == "0.0" and forecast.num(-0.4, 0) == "0"
    assert forecast.num(-0.6, 0) == "-1" and forecast.num(-1.26, 1) == "-1.3"
    fc = mkfc(hours(3))
    fc.now = forecast.Now(-0.04, feels_like=-0.3)
    a = header(fc)
    fc.now = forecast.Now(0.0, feels_like=0.0)
    assert a.tobytes() == header(fc).tobytes()


def test_header_long_location_is_truncated_and_clear_of_condition():
    fc = mkfc(hours(3))
    fc.location = "Oslo-Nordre Aker og Sagene, Norway"
    fc.now = forecast.Now(8.4, feels_like=5.0, condition="partlycloudy", precip_rate=0.3)
    img = header(fc)
    fs = forecast.font(13)
    # the location's rightmost glyph column is right-aligned at x1 - 2 = 410; nothing
    # of it may reach into the condition text, which ends at x + its width
    x = 4 + 28 + 6 + forecast.text_w("8.4°", forecast.font(24)) + 8
    cond_end = x + forecast.text_w("Partly cloudy", fs)
    # truncated: the fitted text is shorter than the full one and ends in an ellipsis
    fit = forecast.fit_text(fc.location, fs, 406 - (cond_end + 8))
    assert fit.endswith("…") and forecast.text_w(fit, fs) <= 406 - (cond_end + 8) < forecast.text_w(fc.location, fs)
    # dark pixels in the top text row exist right of cond_end + 8 only
    row = img.crop((0, 12, 416, 24))
    assert min(row.crop((int(cond_end) + 2, 0, int(cond_end) + 8, 12)).tobytes()) == 255


def test_fit_text():
    f = forecast.font(13)
    assert forecast.fit_text("Oslo", f, 100) == "Oslo"
    assert forecast.fit_text("Oslo", f, 0) == ""
    w = forecast.text_w("Osl…", f)
    assert forecast.fit_text("Osloxxxxxx", f, w) == "Osl…"
    assert forecast.fit_text("Osloxxxxxx", f, forecast.text_w("…", f)) == "…"


def test_header_drops_rate_when_it_does_not_fit():
    fc = mkfc(hours(3))
    fc.now = forecast.Now(8.4, feels_like=5.0, precip_rate=0.3)
    assert header(fc, 408).crop((300, 22, 416, 40)).getextrema()[0] < 255          # rate drawn
    assert header(fc, 150).crop((165, 22, 416, 40)).getextrema()[0] == 255         # dropped
    # at 150 px wide the rate cannot sit beside "feels 5°"
    x = 4 + 28 + 6 + forecast.text_w("8.4°", forecast.font(24)) + 8
    fs = forecast.font(13)
    assert forecast.text_w("0.3 mm/h", fs) > 4 + 150 - 2 - (x + forecast.text_w("feels 5°", fs) + 8)


def test_rain_label_stays_on_canvas_when_spell_starts_at_left_edge(monkeypatch):
    start = dt.datetime(2026, 11, 1, 8, tzinfo=TZ)
    fc = mkfc(hours(49, start), [4.0 + (i % 3) for i in range(49)])
    for h in fc.hourly:
        h.precip_mm = 0.5                 # wet for the whole span: no room on either side
    labels = []
    monkeypatch.setattr(forecast, "halo_text", lambda dr, xy, s, f, anchor="mm", **kw:
                        labels.append((xy[0], s, forecast.text_w(s, f), anchor)))
    img = Image.new("L", (416, 240), 255)
    hs = forecast.select_hours(fc, start, tz=TZ)
    forecast.chart(forecast.ImageDraw.Draw(img), fc, hs, (4, 96, 412, 237), lambda c: c, False)
    rain = [l for l in labels if l[1].endswith(" mm")]
    assert rain
    for x, _, w, anc in rain:
        left = x - w if anc == "rs" else x
        assert left >= 4 and left + w <= 412
