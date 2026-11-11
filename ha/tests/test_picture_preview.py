"""picture.py and preview.py without Home Assistant.

    uv run --with pytest --with pillow pytest ha/tests/test_picture_preview.py
"""
from __future__ import annotations

import datetime as dt
import io
import sys
from math import gcd
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

HA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HA.parent / "custom_components"))

from th5_display import frame, picture, preview  # noqa: E402


def gradient(w: int = 800, h: int = 600) -> Image.Image:
    img = Image.new("RGB", (w, h))
    img.putdata([(x * 255 // (w - 1),) * 3 for _ in range(h) for x in range(w)])
    return img


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_load_flattens_alpha_and_applies_exif():
    rgba = Image.new("RGBA", (10, 20), (0, 0, 0, 0))
    assert picture.load(png(rgba)).getpixel((0, 0)) == (255, 255, 255)
    img = Image.new("RGB", (10, 20))
    exif = Image.Exif()
    exif[0x0112] = 6                                    # rotate 90 CW
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert picture.load(buf.getvalue()).size == (20, 10)


def test_shrink_keeps_aspect():
    assert picture.shrink(gradient(4000, 1000)).size == (1600, 400)
    assert picture.shrink(gradient(100, 50)).size == (100, 50)


@pytest.mark.parametrize("how", picture.FITS)
def test_fit_size(how):
    out = picture.fit(gradient(300, 300), how)
    assert out.size == (frame.W, frame.H) and out.mode == "L"


def test_contain_letterboxes_white_cover_fills():
    tall_black = Image.new("RGB", (100, 400), (0, 0, 0))
    contain = picture.fit(tall_black, "contain")
    assert contain.getpixel((0, 120)) == 255 and contain.getpixel((208, 120)) == 0
    cover = picture.fit(tall_black, "cover")
    assert cover.getpixel((0, 0)) == 0 and cover.getpixel((415, 239)) == 0


@pytest.mark.parametrize("how", picture.DITHERS)
def test_dither_only_four_levels(how):
    out = picture.render(gradient(), "cover", how)
    assert set(out.tobytes()) <= set(frame.LEVELS)
    # a smooth ramp uses every level, and its mean brightness is kept
    assert set(out.tobytes()) == set(frame.LEVELS)
    mean = sum(out.tobytes()) / (frame.W * frame.H)
    assert 110 < mean < 145


def test_bad_options_raise():
    with pytest.raises(ValueError):
        picture.fit(gradient(), "stretch")
    with pytest.raises(ValueError):
        picture.dither(Image.new("L", (4, 4)), "ordered")


@pytest.mark.parametrize(("top_bar", "size", "ratio"), [(False, (416, 240), (26, 15)),
                                                         (True, (416, 181), (416, 181))])
def test_picture_area_sizes(top_bar, size, ratio):
    w, h = picture.area(top_bar)
    assert (w, h) == size and (w // gcd(w, h), h // gcd(w, h)) == ratio


@pytest.mark.parametrize("how", picture.FITS)
def test_top_bar_layout_renders_below_the_band(how):
    # A 416:181 black image fills the area under the bar exactly with either fit;
    # rows 0..55 and 56 stay white (MCU band, gap), rows 57..58 are the rule.
    black = Image.new("RGB", (832, 362), (0, 0, 0))
    out = picture.render(black, how, "none", top_bar=True)
    assert out.size == (frame.W, frame.H)
    assert set(out.crop((0, 0, frame.W, frame.RULE_Y)).tobytes()) == {255}
    assert set(out.crop((0, frame.RULE_Y, frame.W, frame.H)).tobytes()) == {0}
    # the same image full screen: contain adds white bars above and below
    full = picture.render(black, "contain", "none")
    assert full.getpixel((0, 120)) == 0 and full.getpixel((208, 0)) == 255


def test_top_bar_contain_letterboxes_inside_the_area():
    tall = Image.new("RGB", (100, 400), (0, 0, 0))
    out = picture.render(tall, "contain", "none", top_bar=True)
    body = out.crop((0, picture.PICTURE_TOP, frame.W, frame.H))
    bbox = Image.eval(body, lambda v: 255 - v).getbbox()
    assert bbox[1] == 0 and bbox[3] == frame.H - picture.PICTURE_TOP   # full height of the area
    assert bbox[2] - bbox[0] == 45                                     # 181 * 100 / 400


def test_preview_band_only_on_band_frames():
    img = Image.new("L", (frame.W, frame.H), 170)
    payload = frame.encode_planes(img, 2)
    band = preview.Band(now=dt.datetime(2026, 11, 1, 14, 32, tzinfo=ZoneInfo("Europe/Oslo")),
                        temp=21.4, humidity=46)
    for flags, has_band in ((frame.FRAME_FLAG_BAND, True), (0, False)):
        slot = frame.build_slot(payload, 2, flags=flags, commit=False)
        out = Image.open(io.BytesIO(preview.render_png(slot, band)))
        assert out.size == (frame.W * preview.SCALE, frame.H * preview.SCALE)
        top = out.convert("L").crop((0, 0, out.width, frame.BAND_H * preview.SCALE))
        body = out.convert("L").crop((0, frame.BAND_H * preview.SCALE, out.width, out.height))
        assert set(body.tobytes()) == {170}
        assert (0 in top.tobytes()) is has_band
        assert set(top.tobytes()) <= set(frame.LEVELS)


def test_preview_clock_text_style_keeps_the_body_and_thresholds_the_band():
    payload = frame.encode_planes(Image.new("L", (frame.W, frame.H), 170), 2)
    slot = frame.build_slot(payload, 2, flags=frame.FRAME_FLAG_BAND, commit=False)
    now = dt.datetime(2026, 11, 1, 14, 32)
    images = [Image.open(io.BytesIO(preview.render_png(
        slot, preview.Band(now, temp=21.4, humidity=46, monochrome=mono)))).convert("L")
              for mono in (False, True)]
    width, height = images[0].size
    bands = [img.crop((0, 0, width, frame.BAND_H * preview.SCALE)) for img in images]
    assert set(bands[0].tobytes()) == set(frame.LEVELS)
    assert set(bands[1].tobytes()) == {0, 255}
    assert bands[1].tobytes() == bands[0].point(lambda v: 0 if v < 128 else 255).tobytes()
    for img in images:
        assert set(img.crop((0, frame.BAND_H * preview.SCALE, width, height)).tobytes()) == {170}


def test_band_texts():
    now = dt.datetime(2026, 11, 1, 14, 5)
    assert preview.band_texts(preview.Band(now, True, "C", 21.44, 46.0)) == (
        "14:05", "Sun 1 Nov", "21.4°C", "46.0%")
    assert preview.band_texts(preview.Band(now, False, "F"))[::2] == ("02:05", "--.-°F")
