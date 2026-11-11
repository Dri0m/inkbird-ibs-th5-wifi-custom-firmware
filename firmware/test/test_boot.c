/* Host test of the bootloader's decisions and install (boot/boot.c with
 * BOOT_HOST): flash is an array, erase/program obey NOR rules and the
 * bootloader's allowed ranges, and a budget of operations simulates a power
 * cut part way through an install. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fwu.h"

typedef enum { BOOT_APP, BOOT_INSTALLED, BOOT_NO_APP } boot_result_t;
boot_result_t boot_run(void);

uint8_t host_flash[0x20000];
static int budget = -1; /* operations left before the "power cut"; -1 = none */
static int erases, programs;
static bool fail_done; /* the staging done word will not program */

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static bool spend(void)
{
    if (budget == 0)
        return false;
    if (budget > 0)
        budget--;
    return true;
}

static bool in_app(uint32_t a, uint32_t n) { return a >= FWU_APP_BASE && a + n <= FWU_APP_END; }

bool fl_erase(uint32_t a)
{
    if (a % 512u || !in_app(a, 512u)) {
        printf("FAIL erase outside the app region: %#x\n", a);
        fails++;
        return false;
    }
    if (!spend())
        return false;
    erases++;
    memset(host_flash + a, 0xFF, 512u);
    return true;
}

bool fl_program(uint32_t a, const uint8_t *src, uint32_t n)
{
    bool done_word = a == FWU_STAGE + 28u && n == 4u;
    if (!(in_app(a, n) || done_word) || (a | n) & 3u) {
        printf("FAIL program outside the allowed ranges: %#x+%u\n", a, n);
        fails++;
        return false;
    }
    if (!spend() || (done_word && fail_done))
        return false;
    programs++;
    for (uint32_t i = 0; i < n; i++) {
        if ((host_flash[a + i] & src[i]) != src[i])
            return false; /* 0 -> 1 needs an erase */
        host_flash[a + i] &= src[i];
    }
    return true;
}

void fl_feed(void) {}

static uint32_t crc32(const uint8_t *p, uint32_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    while (n--) {
        c ^= *p++;
        for (int k = 0; k < 8; k++)
            c = c >> 1 ^ (0xEDB88320u & -(c & 1u));
    }
    return ~c;
}

static void put32(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }
static uint32_t get32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }

/* An app image: vectors (SP, reset inside), info block, then a pattern. */
static void make_app(uint8_t *img, uint32_t len, uint8_t seed)
{
    for (uint32_t i = 0; i < len; i++)
        img[i] = (uint8_t)(i * 31u + seed);
    put32(img, 0x20004000u);
    put32(img + 4, FWU_APP_BASE + 0x101u);
    put32(img + FWU_INFO_OFF, FWU_APP_MAGIC);
    put32(img + FWU_INFO_OFF + 4, 0x00020000u + seed);
}

/* Stage img as the app's link code would after FW_END (commit = 0). */
static void stage(const uint8_t *img, uint32_t len, bool commit)
{
    memset(host_flash + FWU_STAGE, 0xFF, FWU_STAGE_END - FWU_STAGE);
    fwu_hdr_t h = { FWU_MAGIC, get32(img + FWU_INFO_OFF + 4), len, crc32(img, len),
                    FWU_APP_BASE, FWU_BOOT_VER, commit ? FWU_SET : FWU_ERASED, FWU_ERASED };
    memcpy(host_flash + FWU_STAGE, &h, sizeof h);
    memcpy(host_flash + FWU_STAGE + FWU_HDR_BYTES, img, len);
}

static fwu_hdr_t *hdr(void) { return (fwu_hdr_t *)(host_flash + FWU_STAGE); }
static bool app_is(const uint8_t *img, uint32_t len) { return !memcmp(host_flash + FWU_APP_BASE, img, len); }

static void fresh(const uint8_t *app, uint32_t len)
{
    memset(host_flash, 0xFF, sizeof host_flash);
    put32(host_flash + FWU_INFO_OFF, FWU_BOOT_MAGIC);
    if (app)
        memcpy(host_flash + FWU_APP_BASE, app, len);
    budget = -1;
    erases = programs = 0;
}

int main(void)
{
    static uint8_t old[32572], neu[40000];
    make_app(old, sizeof old, 1);
    make_app(neu, sizeof neu, 2);

    /* Plain boot: nothing staged. */
    fresh(old, sizeof old);
    CHECK(boot_run() == BOOT_APP && erases == 0 && programs == 0);

    /* No app at all. */
    fresh(NULL, 0);
    CHECK(boot_run() == BOOT_NO_APP);

    /* A committed image installs once, then boots normally. */
    fresh(old, sizeof old);
    stage(neu, sizeof neu, true);
    CHECK(boot_run() == BOOT_INSTALLED);
    CHECK(app_is(neu, sizeof neu) && hdr()->done == FWU_SET);
    CHECK(erases == (int)((sizeof neu + 511u) / 512u));
    erases = programs = 0;
    CHECK(boot_run() == BOOT_APP && erases == 0);

    /* Not committed (transfer cut before FW_END): nothing happens. */
    fresh(old, sizeof old);
    stage(neu, sizeof neu, false);
    CHECK(boot_run() == BOOT_APP && app_is(old, sizeof old) && erases == 0);

    /* CRC mismatch, bad magic, wrong load, newer bootloader needed, bad
     * vectors, no app magic, bad length: never installed. */
    for (int k = 0; k < 8; k++) {
        fresh(old, sizeof old);
        stage(neu, sizeof neu, true);
        uint8_t *img = host_flash + FWU_STAGE + FWU_HDR_BYTES;
        switch (k) {
        case 0: img[1000] ^= 1u; break;
        case 1: hdr()->magic = 0x12345678u; break;
        case 2: hdr()->load = 0u; break;
        case 3: hdr()->min_boot_ver = 2u; break;
        case 4: put32(img + 4, 0x200u | 1u); break;         /* reset in the bootloader */
        case 5: put32(img + FWU_INFO_OFF, 0u); break;
        case 6: hdr()->length = FWU_MAX_IMAGE + 4u; break;
        case 7: hdr()->length = 1002u; break;               /* not a multiple of 4 */
        }
        if (k == 4 || k == 5)
            hdr()->crc32 = crc32(img, sizeof neu);           /* consistent, just insane */
        CHECK(boot_run() == BOOT_APP);
        CHECK(app_is(old, sizeof old) && erases == 0);
    }

    /* Power cut at every point of an install: the next boot finishes it. */
    int total;
    fresh(old, sizeof old);
    stage(neu, sizeof neu, true);
    (void)boot_run();
    total = erases + programs;
    for (int cut = 0; cut < total; cut++) {
        fresh(old, sizeof old);
        stage(neu, sizeof neu, true);
        budget = cut;
        boot_result_t r = boot_run();
        /* Both tries die at the cut; a cut at the very last step (the done
         * word) leaves a verified app, which starts. */
        CHECK(r == (cut == total - 1 ? BOOT_INSTALLED : BOOT_NO_APP));
        CHECK(hdr()->done == FWU_ERASED);
        budget = -1;
        CHECK(boot_run() == BOOT_INSTALLED);
        CHECK(app_is(neu, sizeof neu) && hdr()->done == FWU_SET);
    }

    /* The done word fails to program (worn bit): the verified app still
     * starts, and the next reset reinstalls it. */
    fresh(old, sizeof old);
    stage(neu, sizeof neu, true);
    fail_done = true;
    CHECK(boot_run() == BOOT_INSTALLED && app_is(neu, sizeof neu));
    CHECK(hdr()->done == FWU_ERASED);
    fail_done = false;
    CHECK(boot_run() == BOOT_INSTALLED && hdr()->done == FWU_SET);

    /* done = 0 but the app was damaged later (vector sector erased):
     * installed again from the staged copy. */
    fresh(old, sizeof old);
    stage(neu, sizeof neu, true);
    CHECK(boot_run() == BOOT_INSTALLED);
    memset(host_flash + FWU_APP_BASE, 0xFF, 512u);
    CHECK(boot_run() == BOOT_INSTALLED && app_is(neu, sizeof neu));

    /* After an install the app writes a frame over the staging header:
     * plain boots from then on. */
    memset(host_flash + FWU_STAGE, 0xFF, 512u);
    erases = 0;
    CHECK(boot_run() == BOOT_APP && erases == 0);

    /* Largest image. */
    static uint8_t big[FWU_MAX_IMAGE];
    make_app(big, sizeof big, 3);
    fresh(old, sizeof old);
    stage(big, sizeof big, true);
    CHECK(boot_run() == BOOT_INSTALLED && app_is(big, sizeof big));

    if (fails) {
        printf("test_boot: %d failures\n", fails);
        return 1;
    }
    printf("test_boot: all passed (%d power-cut points)\n", total);
    return 0;
}
