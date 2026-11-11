# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10.1"]
# ///
"""Dev CLI: render the forecast frame (layout D) the way the HA integration does.

    uv run ha/tools/th5_render.py [--json FILE | --fetch] [--units C|F] [--out DIR] [--bpp 2]

--json takes an Open-Meteo response or the mockup sample format (default:
ha/tests/sample_forecast.json, rendered at its own "now"). --fetch gets
Open-Meteo (icon_seamless) for Oslo and saves the response as openmeteo.json.

Writes into DIR (default ha/out), per unit:
    frame-<u>.slot          32-byte header + payload, flags = BAND, committed;
                            flash at FRAME_SLOT_A (0x10000)
    preview-<u>.png         decoded from the slot's planes, band area shown
                            as a light placeholder
    preview-<u>_3x.png      3x nearest-neighbour upscale of the above
    preview-<u>-1bit_3x.png plane0 only: what the MCU shows when too cold for greys
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

HA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HA.parent / "custom_components"))

from th5_display import frame, forecast, sources  # noqa: E402

SAMPLE = HA / "tests" / "sample_forecast.json"
TZ = ZoneInfo(sources.OSLO[2])


def load(a) -> tuple[forecast.Forecast, dt.datetime, ZoneInfo]:
    if a.fetch:
        url = sources.open_meteo_url()
        print(f"GET {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "th5_display-dev"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        Path(a.out, "openmeteo.json").write_text(json.dumps(d, indent=1))
        return sources.from_open_meteo(d), dt.datetime.now(TZ), TZ
    d = json.loads(Path(a.json).read_text())
    tz = ZoneInfo(d["timezone"]) if d.get("timezone") else TZ
    if isinstance(d.get("hourly"), dict):           # Open-Meteo response
        fc = sources.from_open_meteo(d)
        now = fc.hourly[0].time if fc.hourly else dt.datetime.now(TZ)
    else:                                           # mockup sample
        fc = sources.from_sample(d)
        now = dt.datetime.fromisoformat(d["now"]["time"])
    if a.now:
        now = dt.datetime.fromisoformat(a.now)
    return fc, now, tz


def band_placeholder(img: Image.Image) -> Image.Image:
    """RGB copy with the MCU band area tinted and outlined, for humans only."""
    rgb = img.convert("RGB")
    dr = ImageDraw.Draw(rgb)
    dr.rectangle((0, 0, frame.W - 1, frame.BAND_H - 1), fill=(236, 240, 248), outline=(120, 140, 200))
    dr.text((frame.W // 2, frame.BAND_H // 2), "MCU band: clock, date, indoor T/RH, battery",
            fill=(120, 140, 200), anchor="mm")
    return rgb


def up3(img: Image.Image) -> Image.Image:
    return img.resize((img.width * 3, img.height * 3), Image.Resampling.NEAREST)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--json", default=str(SAMPLE))
    src.add_argument("--fetch", action="store_true")
    ap.add_argument("--units", choices=("C", "F"), action="append", help="default: both")
    ap.add_argument("--out", default=str(HA / "out"))
    ap.add_argument("--bpp", type=int, choices=(1, 2), default=2)
    ap.add_argument("--seq", type=int, default=1)
    ap.add_argument("--now", help="ISO time to render at (default: data's or wall clock)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    fc, now, tz = load(a)
    print(f"{fc.location}: {len(fc.hourly)} hours, now {now:%Y-%m-%d %H:%M %Z}")
    for u in a.units or ("C", "F"):
        img = forecast.render(fc, u, now, tz=tz)
        payload = frame.encode_planes(img, a.bpp)
        slot = frame.build_slot(payload, a.bpp, flags=frame.FRAME_FLAG_BAND, seq=a.seq)
        hdr, pl = frame.parse_slot(slot)              # self-check
        (out / f"frame-{u}.slot").write_bytes(slot)
        shown = frame.decode_planes(pl, a.bpp)        # exactly what is in the frame
        prev = band_placeholder(shown)
        prev.save(out / f"preview-{u}.png")
        up3(prev).save(out / f"preview-{u}_3x.png")
        mono = frame.decode_planes(pl[:frame.FRAME_PLANE_BYTES], 1)
        up3(band_placeholder(mono)).save(out / f"preview-{u}-1bit_3x.png")
        print(f"  {u}: {len(slot)} B slot, id {hdr.frame_id:016x}, crc {hdr.crc32:08x} -> {out}/frame-{u}.slot")


if __name__ == "__main__":
    main()
