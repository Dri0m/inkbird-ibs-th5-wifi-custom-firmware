# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""Build the host renderer, run it and turn the raw planes into PNGs
(landscape 416x240, x2).  Panel mapping: landscape x = row, y = byte*8+bit
(MSB first); each row is the hi plane then the lo plane, pixel = hi:lo,
11 white, 10 light grey, 01 dark grey, 00 black.

    uv run firmware/tools/preview_render.py [OUTDIR]
"""
import subprocess
import sys
import tempfile
from pathlib import Path
from PIL import Image

FW = Path(__file__).resolve().parent.parent
SCALE = 2


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "render-preview")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        exe = Path(t) / "render_host"
        subprocess.run(["clang", "-std=c11", "-O1", "-Wall", "-Wextra", f"-I{FW/'src'}",
                        str(FW / "test/render_host.c"), str(FW / "src/render.c"),
                        str(FW / "src/fonts_gen.c"), "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(out)], check=True)
    for raw in sorted(out.glob("*.raw")):
        data = raw.read_bytes()
        rb = 7 if raw.name.startswith("band") else 30
        img = Image.new("L", (416, rb * 8), 255)
        px = img.load()
        for x in range(416):
            for k in range(rb):
                h, l = data[x * 2 * rb + k], data[x * 2 * rb + rb + k]
                for bit in range(8):
                    code = 2 * ((h >> (7 - bit)) & 1) + ((l >> (7 - bit)) & 1)
                    px[x, k * 8 + bit] = code * 85
        img = img.resize((416 * SCALE, rb * 8 * SCALE), Image.NEAREST)
        img.save(raw.with_suffix(".png"))
        print(raw.with_suffix(".png"))


if __name__ == "__main__":
    main()
