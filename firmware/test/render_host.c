/* Host build: render sample screens row by row and dump raw planes.
 *   clang -std=c11 -O1 -Isrc test/render_host.c src/render.c src/fonts_gen.c -o render_host
 *   ./render_host OUTDIR
 * Writes OUTDIR/<name>.raw : 416 rows of the hi plane then the lo plane,
 * 30 + 30 bytes (fallback) or 7 + 7 bytes (band).
 * tools/preview_render.py turns them into PNGs. */
#include <stdio.h>
#include <string.h>

#include "render.h"

static const struct {
  const char *name;
  screen_t s;
} cases[] = {
    {"normal", {214, 528, false, true, true, 14, 5, 6, 31, 10, 78, 0, LINK_OK, false, false}},
    {"fahrenheit", {-57, 999, true, true, true, 7, 41, 0, 1, 12, 100, 0, LINK_OK, true, false}},
    {"notset", {1999, 94, false, true, false, 0, 0, 0, 0, 0, 55, 1, LINK_WAIT, false, false}},
    {"linkerr", {0, 0, false, false, false, 0, 0, 0, 0, 0, 9, 2, LINK_ERR, false, false}},
    {"chargeme", {-123, 455, false, true, true, 23, 59, 3, 29, 2, 3, 3, LINK_ERR, true, false}},
    {"hot_f", {450, 999, true, true, true, 8, 8, 4, 8, 8, 100, 4, LINK_WAIT, false, false}},
    {"nopicture", {214, 528, false, true, true, 14, 5, 6, 31, 10, 78, 0, LINK_OK, false, false}},
    {"wifi", {214, 528, false, true, true, 14, 5, 6, 31, 10, 78, 5, LINK_ERR, false, false}},
    {"ha_unreach", {214, 528, false, true, true, 14, 5, 6, 31, 10, 78, 6, LINK_ERR, false, false}},
    {"ha_noanswer", {223, 442, false, true, false, 0, 0, 0, 0, 0, 80, 7, LINK_ERR, false, false}},
};

int main(int argc, char **argv) {
  const char *dir = argc > 1 ? argv[1] : ".";
  for (unsigned c = 0; c < sizeof cases / sizeof *cases; c++) {
    for (int mode = 0; mode < 2; mode++) {
      char path[512];
      snprintf(path, sizeof path, "%s/%s-%s.raw", dir, mode ? "fallback" : "band", cases[c].name);
      FILE *f = fopen(path, "wb");
      if (!f) { perror(path); return 1; }
      uint8_t hi[30], lo[30];
      for (uint16_t x = 0; x < 416; x++) {
        if (mode) render_fallback_row(&cases[c].s, x, hi, lo);
        else render_band_row(&cases[c].s, x, hi, lo);
        fwrite(hi, 1, mode ? 30 : 7, f);
        fwrite(lo, 1, mode ? 30 : 7, f);
      }
      fclose(f);
    }
  }
  /* consistency: band rows must equal the top 7 bytes of fallback rows,
   * also when two screens are rendered alternately (the band update renders
   * the old and the new screen row by row through the two-slot cache) */
  for (unsigned c = 0; c < sizeof cases / sizeof *cases; c++) {
    unsigned d = (c + 1) % (sizeof cases / sizeof *cases);
    for (uint16_t x = 0; x < 416; x++) {
      uint8_t ah[7], al[7], bh[30], bl[30], ch[7], cl[7], dh[7], dl[7];
      render_band_row(&cases[c].s, x, ah, al);
      render_fallback_row(&cases[c].s, x, bh, bl);
      if (memcmp(ah, bh, 7) || memcmp(al, bl, 7)) {
        fprintf(stderr, "band mismatch case %u x %u\n", c, x);
        return 2;
      }
      render_band_row(&cases[d].s, x, dh, dl);
      render_band_row(&cases[c].s, x, ch, cl);
      if (memcmp(ah, ch, 7) || memcmp(al, cl, 7)) {
        fprintf(stderr, "alternating render mismatch case %u x %u\n", c, x);
        return 3;
      }
      (void)dh; (void)dl;
    }
  }
  return 0;
}
