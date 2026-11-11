/* Host test for src/flash.c against a modelled FLASH controller.
 *   cc -std=c11 -Wall -Wextra -Werror -DFLASH_HOST_TEST -I firmware/test -I firmware/src \
 *      firmware/test/test_flash.c firmware/src/flash.c -o <out> && <out>
 * (make -C firmware test-flash does the same.)
 *
 * Checks: range rejection before any register access, alignment handling,
 * lock/op/key invariants over every logged write, and the exact register
 * write sequence for one sector erase and one word program, compared with
 * stock 0x30D8/0x3194 and DDL Flash_SectorErase/Flash_Write32 (differences
 * are listed in firmware/notes-flash.md). */
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "flash.h"
#include "flash_host.h"
#include "frame.h"

/* ---- controller model ------------------------------------------------ */

#define FBASE 0x40020000u
#define R_TNVS 0x00u
#define R_TSERASE 0x0Cu
#define R_TMERASE 0x10u
#define R_TMRCV 0x1Cu
#define R_CR 0x20u
#define R_BYPASS 0x2Cu
#define R_SLOCK0 0x30u
#define R_SLOCK1 0x34u
#define CLKEN0 0x40002020u

uint8_t host_flash[HOST_FLASH_BYTES];
uint32_t host_regs[16];
uint32_t host_clken0;
host_wr_t host_log[4096];
unsigned host_nlog;
unsigned host_faults;
unsigned host_feeds;
bool host_masked;
static int key_state; /* 0 idle, 1 got 0x5A5A, 2 armed */
static unsigned outside_writes;
uint32_t SystemCoreClock = 4000000u;

static void log_wr(uint32_t a, uint32_t v, uint8_t w)
{
    if (host_nlog < sizeof host_log / sizeof host_log[0])
        host_log[host_nlog++] = (host_wr_t){ a, v, w, host_masked };
}

static bool unlocked(uint32_t a)
{
    uint32_t g = a >> 11;
    uint32_t r = host_regs[(R_SLOCK0 >> 2) + (g >> 5)];
    return (r >> (g & 31u)) & 1u;
}

static void flash_store(uint32_t a, uint32_t v, unsigned w)
{
    uint32_t op = host_regs[R_CR >> 2] & 3u;
    if (!flash_allowed(a, w))
        outside_writes++;
    if (!unlocked(a)) {
        host_faults++;
        return;
    }
    if (op == 2u) {
        memset(&host_flash[a & ~511u], 0xFF, 512u);
    } else if (op == 1u) {
        for (unsigned i = 0; i < w; i++)
            host_flash[a + i] &= (uint8_t)(v >> (8u * i));
    } else {
        host_faults++;
    }
}

uint32_t io_rd32(uintptr_t a)
{
    if (a < HOST_FLASH_BYTES) {
        uint32_t v;
        memcpy(&v, &host_flash[a], 4);
        return v;
    }
    if (a == CLKEN0)
        return host_clken0;
    if (a == FBASE + R_CR)
        return host_regs[R_CR >> 2] & ~(1u << 4); /* BUSY = 0: ops finish at once */
    if (a >= FBASE && a < FBASE + 0x40u)
        return host_regs[(a - FBASE) >> 2];
    host_faults++;
    return 0;
}

uint8_t io_rd8(uintptr_t a)
{
    if (a < HOST_FLASH_BYTES)
        return host_flash[a];
    host_faults++;
    return 0;
}

void io_wr32(uintptr_t a, uint32_t v)
{
    log_wr((uint32_t)a, v, 4);
    if (a < HOST_FLASH_BYTES) {
        flash_store((uint32_t)a, v, 4);
    } else if (a == CLKEN0) {
        host_clken0 = v;
    } else if (a == FBASE + R_BYPASS) {
        key_state = (v == 0x5A5Au) ? 1 : (key_state == 1 && v == 0xA5A5u) ? 2 : 0;
    } else if (a >= FBASE && a < FBASE + 0x38u) {
        if (key_state == 2)
            host_regs[(a - FBASE) >> 2] = v;
        else
            host_faults++;
        key_state = 0;
    } else {
        host_faults++;
    }
}

void io_wr8(uintptr_t a, uint8_t v)
{
    log_wr((uint32_t)a, v, 1);
    if (a < HOST_FLASH_BYTES)
        flash_store((uint32_t)a, v, 1);
    else
        host_faults++;
}

uint32_t irq_save(void)
{
    uint32_t pm = host_masked;
    host_masked = true;
    return pm;
}

void irq_restore(uint32_t pm) { host_masked = pm != 0; }
void wdt_feed(void) { host_feeds++; }

/* ---- helpers ----------------------------------------------------------- */

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void reset_log(void) { host_nlog = 0; host_faults = 0; host_feeds = 0; }

/* Invariants over every logged write since reset_log(). */
static void check_invariants(void)
{
    for (unsigned i = 0; i < host_nlog; i++) {
        host_wr_t w = host_log[i];
        bool freg = w.addr >= FBASE && w.addr < FBASE + 0x100u;
        if (freg && w.addr != FBASE + R_BYPASS) {
            /* keyed, back to back, IRQs masked */
            CHECK(i >= 2 && host_log[i - 2].addr == FBASE + R_BYPASS && host_log[i - 2].val == 0x5A5Au);
            CHECK(i >= 1 && host_log[i - 1].addr == FBASE + R_BYPASS && host_log[i - 1].val == 0xA5A5u);
            CHECK(w.masked);
            CHECK(w.addr < FBASE + 0x38u); /* no 0x40/0x44 as stock writes */
            CHECK(w.addr != FBASE + R_TMERASE && w.addr != FBASE + R_TMRCV);
        }
        if (w.addr == FBASE + R_CR)
            CHECK((w.val & 3u) != 3u && ((w.val >> 2) & 3u) <= 1u); /* no array erase; WAIT 0/1 */
        if (w.addr == FBASE + R_SLOCK0)
            CHECK(w.val == 0u);
        if (w.addr == FBASE + R_SLOCK1)
            CHECK(w.val == 0u || (w.val & (w.val - 1u)) == 0u); /* one group at most */
        if (w.addr < HOST_FLASH_BYTES)
            CHECK(flash_allowed(w.addr, w.width));
    }
    /* left locked and in read mode */
    CHECK(host_regs[R_SLOCK0 >> 2] == 0u);
    CHECK(host_regs[R_SLOCK1 >> 2] == 0u);
    CHECK((host_regs[R_CR >> 2] & 3u) == 0u);
    CHECK(!host_masked);
    CHECK(host_faults == 0u);
    CHECK(outside_writes == 0u);
}

typedef struct { uint32_t addr, val; uint8_t width; } exp_t;

static void check_seq(const exp_t *e, unsigned n)
{
    CHECK(host_nlog == n);
    for (unsigned i = 0; i < n && i < host_nlog; i++) {
        if (host_log[i].addr != e[i].addr || host_log[i].val != e[i].val || host_log[i].width != e[i].width) {
            printf("  seq[%u]: got %08x=%08x/%u want %08x=%08x/%u\n", i, (unsigned)host_log[i].addr,
                   (unsigned)host_log[i].val, host_log[i].width, (unsigned)e[i].addr, (unsigned)e[i].val, e[i].width);
            fails++;
        }
    }
}

#define K1 { FBASE + R_BYPASS, 0x5A5Au, 4 }
#define K2 { FBASE + R_BYPASS, 0xA5A5u, 4 }

int main(void)
{
    /* Flash starts as a "used" image: not erased anywhere. */
    memset(host_flash, 0x00, sizeof host_flash);

    /* ---- range check ---- */
    CHECK(!flash_allowed(0x0u, 1));
    CHECK(!flash_allowed(0x0u, 512));
    CHECK(!flash_allowed(0xFFFFu, 1));
    CHECK(!flash_allowed(0xFFFFu, 2));             /* straddles code/slot A */
    CHECK(!flash_allowed(0xFE00u, 512));
    CHECK(flash_allowed(0x10000u, 1));
    CHECK(flash_allowed(0x10000u, FRAME_SLOT_BYTES));
    CHECK(flash_allowed(0x10000u, 2u * FRAME_SLOT_BYTES)); /* A + B: firmware staging */
    CHECK(!flash_allowed(0x10000u, 2u * FRAME_SLOT_BYTES + 1u));
    CHECK(flash_allowed(0x161FFu, 1));
    CHECK(flash_allowed(0x161FFu, 2));             /* crosses A -> B: one range */
    CHECK(flash_allowed(0x16200u, 1));             /* 0x161FF + 1 = slot B */
    CHECK(flash_allowed(0x16200u, FRAME_SLOT_BYTES));
    CHECK(flash_allowed(0x1C3FFu, 1));
    CHECK(!flash_allowed(0x1C3FFu, 2));
    CHECK(!flash_allowed(0x1C400u, 1));            /* spare, not writable */
    CHECK(!flash_allowed(0x1FBFFu, 1));
    CHECK(flash_allowed(0x1FC00u, 512));
    CHECK(flash_allowed(0x1FE00u, 512));
    CHECK(flash_allowed(0x1FC00u, 1024));
    CHECK(flash_allowed(0x1FFFFu, 1));
    CHECK(!flash_allowed(0x1FFFFu, 2));
    CHECK(!flash_allowed(0x20000u, 1));
    CHECK(!flash_allowed(0x00100C00u, 4));         /* factory info area */
    CHECK(!flash_allowed(0xFFFFFFFFu, 2));         /* wraparound */
    CHECK(!flash_allowed(0x10000u, 0xFFFFFFFFu));
    CHECK(!flash_allowed(0x10000u, 0));

    /* ---- not initialised yet: refused, no register touched ---- */
    reset_log();
    uint32_t word = 0x12345678u;
    CHECK(!flash_erase_sector(0x10000u));
    CHECK(!flash_program(0x10000u, &word, 4));
    CHECK(host_nlog == 0);

    /* ---- init ---- */
    host_regs[R_SLOCK0 >> 2] = 0xFFFFFFFFu; /* unknown reset state: unlocked */
    host_regs[R_SLOCK1 >> 2] = 0xFFFFFFFFu;
    host_regs[R_CR >> 2] = 0x1u;            /* stock leaves OP = program */
    reset_log();
    CHECK(flash_init());
    {
        const exp_t e[] = {
            { CLKEN0, 1u << 31, 4 },
            K1, K2, { FBASE + 0x00u, 0x20u, 4 },
            K1, K2, { FBASE + 0x04u, 0x17u, 4 },
            K1, K2, { FBASE + 0x08u, 0x1Bu, 4 },
            K1, K2, { FBASE + 0x0Cu, 0x4650u, 4 },
            K1, K2, { FBASE + 0x14u, 0x18u, 4 },
            K1, K2, { FBASE + 0x18u, 0xF0u, 4 },
            K1, K2, { FBASE + R_CR, 0x0u, 4 },
            K1, K2, { FBASE + R_SLOCK0, 0u, 4 },
            K1, K2, { FBASE + R_SLOCK1, 0u, 4 },
        };
        check_seq(e, sizeof e / sizeof e[0]);
    }
    check_invariants();

    /* ---- rejected addresses: false before any register access ---- */
    reset_log();
    CHECK(!flash_erase_sector(0x0u));
    CHECK(!flash_erase_sector(0xFE00u));
    CHECK(!flash_erase_sector(0x10004u));          /* not sector aligned */
    CHECK(!flash_erase_sector(0x1C400u));
    CHECK(!flash_erase_sector(0x1FA00u));
    CHECK(!flash_erase_sector(0x20000u));
    CHECK(!flash_program(0xFFFCu, &word, 4));
    CHECK(!flash_program(0x1C3FEu, &word, 4));     /* runs past slot B */
    CHECK(!flash_program(0x1FBFCu, &word, 4));
    CHECK(!flash_program(0x10000u, NULL, 4));
    CHECK(!flash_program(0x10000u, &word, 0));
    CHECK(host_nlog == 0);
    CHECK(host_flash[0x0] == 0x00 && host_flash[0xFE00] == 0x00);

    /* ---- one sector erase, exact sequence ----
     * 0x16200 = sector 177 = lock group 44 = SLOCK1 bit 12.
     * Stock 0x30D8: wait idle; unlock all (SLOCK0/1 and 0x40/0x44 = ~0);
     * OP = 2; strb 0 -> addr; wait; lock all. DDL: OP = 2 (caller); str 0;
     * wait. Ours: unlock one group; OP = 2; str 0; wait; OP = 0; lock. */
    reset_log();
    CHECK(flash_erase_sector(0x16200u));
    {
        const exp_t e[] = {
            K1, K2, { FBASE + R_SLOCK1, 1u << 12, 4 },
            K1, K2, { FBASE + R_CR, 0x2u, 4 },
            { 0x16200u, 0u, 4 },
            K1, K2, { FBASE + R_CR, 0x0u, 4 },
            K1, K2, { FBASE + R_SLOCK1, 0u, 4 },
        };
        check_seq(e, sizeof e / sizeof e[0]);
    }
    CHECK(host_feeds >= 1);
    check_invariants();
    {
        bool all_ff = true;
        for (uint32_t a = 0x16200u; a < 0x16400u; a++)
            all_ff = all_ff && host_flash[a] == 0xFF;
        CHECK(all_ff);
    }
    CHECK(host_flash[0x161FFu] == 0x00 && host_flash[0x16400u] == 0x00); /* neighbours */

    /* ---- one word program, exact sequence ----
     * Stock 0x3194 (byte): wait; OP = 1; unlock all; strb; wait; lock all.
     * DDL Flash_Write32: OP = 1 (caller); str; wait; read back. */
    reset_log();
    word = 0xA5C3F00Fu;
    CHECK(flash_program(0x16200u, &word, 4));
    {
        const exp_t e[] = {
            K1, K2, { FBASE + R_SLOCK1, 1u << 12, 4 },
            K1, K2, { FBASE + R_CR, 0x1u, 4 },
            { 0x16200u, 0xA5C3F00Fu, 4 },
            K1, K2, { FBASE + R_CR, 0x0u, 4 },
            K1, K2, { FBASE + R_SLOCK1, 0u, 4 },
        };
        check_seq(e, sizeof e / sizeof e[0]);
    }
    check_invariants();
    CHECK(memcmp(&host_flash[0x16200u], &word, 4) == 0);

    /* ---- same value again: no flash store at all ---- */
    reset_log();
    CHECK(flash_program(0x16200u, &word, 4));
    for (unsigned i = 0; i < host_nlog; i++)
        CHECK(host_log[i].addr >= FBASE);
    check_invariants();

    /* ---- 0 -> 1 needed: refused, nothing stored, relocked ---- */
    reset_log();
    uint32_t ones = 0xFFFFFFFFu;
    CHECK(!flash_program(0x16200u, &ones, 4));
    for (unsigned i = 0; i < host_nlog; i++)
        CHECK(host_log[i].addr >= FBASE);
    check_invariants();

    /* ---- commit word: 0xFFFFFFFF -> 0 after the header (frame.h) ---- */
    reset_log();
    CHECK(flash_erase_sector(0x10000u));
    frame_hdr_t h = { FRAME_MAGIC, FRAME_VERSION, 2, 0, FRAME_FLAG_BAND, 7, 1, 2,
                      2u * FRAME_PLANE_BYTES, 0xDEADBEEFu, 0xFFFFFFFFu };
    CHECK(flash_program(0x10000u, &h, sizeof h));
    uint32_t commit = FRAME_COMMITTED;
    CHECK(flash_program(0x10000u + offsetof(frame_hdr_t, commit), &commit, 4));
    CHECK(((const frame_hdr_t *)(const void *)&host_flash[0x10000u])->commit == 0u);
    CHECK(((const frame_hdr_t *)(const void *)&host_flash[0x10000u])->crc32 == 0xDEADBEEFu);
    check_invariants();

    /* ---- alignment: unaligned dst and src, head/tail bytes ---- */
    CHECK(flash_erase_sector(0x10200u));
    reset_log();
    uint8_t buf[16];
    for (unsigned i = 0; i < sizeof buf; i++)
        buf[i] = (uint8_t)(0x10u + i);
    CHECK(flash_program(0x10201u, buf + 1, 9)); /* 0x10201..0x10209 */
    {
        unsigned w1 = 0, w4 = 0;
        uint32_t addrs[8];
        unsigned na = 0;
        for (unsigned i = 0; i < host_nlog; i++) {
            if (host_log[i].addr >= HOST_FLASH_BYTES)
                continue;
            if (na < 8)
                addrs[na++] = host_log[i].addr;
            if (host_log[i].width == 1) w1++; else w4++;
        }
        CHECK(w1 == 5 && w4 == 1); /* bytes 1,2,3 + word 4..7 + bytes 8,9 */
        CHECK(na == 6 && addrs[0] == 0x10201u && addrs[3] == 0x10204u && addrs[4] == 0x10208u);
    }
    CHECK(memcmp(&host_flash[0x10201u], buf + 1, 9) == 0);
    CHECK(host_flash[0x10200u] == 0xFF && host_flash[0x1020Au] == 0xFF);
    check_invariants();

    /* ---- crossing a 2 KB lock group: two unlock/relock rounds ---- */
    CHECK(flash_erase_sector(0x10600u));
    CHECK(flash_erase_sector(0x10800u));
    reset_log();
    uint32_t two[2] = { 0x11111111u, 0x22222222u };
    CHECK(flash_program(0x107FCu, two, 8));
    {
        unsigned unl = 0;
        for (unsigned i = 0; i < host_nlog; i++)
            if (host_log[i].addr == FBASE + R_SLOCK1 && host_log[i].val)
                CHECK(host_log[i].val == (unl++ == 0 ? 1u << 0 : 1u << 1));
        CHECK(unl == 2);
    }
    CHECK(memcmp(&host_flash[0x107FCu], two, 8) == 0);
    check_invariants();

    /* ---- settings sectors ---- */
    reset_log();
    CHECK(flash_erase_sector(FLASH_SETTINGS_A));
    CHECK(flash_erase_sector(FLASH_SETTINGS_B));
    CHECK(flash_program(FLASH_SETTINGS_B + 0x1FCu, &word, 4)); /* last word of flash */
    CHECK(host_log[2].addr == FBASE + R_SLOCK1 && host_log[2].val == 1u << 31);
    check_invariants();

    /* ---- whole slot B: erase 49 sectors, program a pattern, read back ---- */
    static uint8_t pat[FRAME_SLOT_BYTES];
    for (unsigned i = 0; i < sizeof pat; i++)
        pat[i] = (uint8_t)(i * 7u + (i >> 9));
    reset_log();
    for (uint32_t a = FRAME_SLOT_B; a < FRAME_SLOT_B + FRAME_SLOT_BYTES; a += FLASH_SECTOR_BYTES)
        CHECK(flash_erase_sector(a));
    unsigned feeds_erase = host_feeds;
    CHECK(feeds_erase >= FRAME_SLOT_BYTES / FLASH_SECTOR_BYTES);
    host_nlog = 0; /* log is too small for the full program; invariants checked above */
    CHECK(flash_program(FRAME_SLOT_B, pat, sizeof pat));
    CHECK(memcmp(&host_flash[FRAME_SLOT_B], pat, sizeof pat) == 0);
    /* at least one feed per 64 words */
    CHECK(host_feeds - feeds_erase >= (FRAME_SLOT_BYTES / 4u) / 64u);
    CHECK(host_flash[FRAME_SLOT_B + FRAME_SLOT_BYTES] == 0x00); /* spare untouched */
    CHECK(host_faults == 0 && outside_writes == 0);

    /* ---- wrong HCLK: refused before any register access ---- */
    SystemCoreClock = 24000000u;
    reset_log();
    CHECK(!flash_erase_sector(0x10000u));
    CHECK(!flash_program(0x10000u, &word, 4));
    CHECK(host_nlog == 0);

    /* ---- 24 MHz (link sessions): WAIT = 1 first, timings x6 as DDL
     * Flash_Init(6); init and every op keep WAIT ---- */
    SystemCoreClock = 4000000u;
    reset_log();
    CHECK(flash_set_wait(true));
    {
        const exp_t e[] = { { CLKEN0, 1u << 31, 4 }, K1, K2, { FBASE + R_CR, 0x4u, 4 } };
        check_seq(e, sizeof e / sizeof e[0]);
    }
    SystemCoreClock = 24000000u;
    reset_log();
    CHECK(flash_init());
    {
        const exp_t e[] = {
            { CLKEN0, 1u << 31, 4 },
            K1, K2, { FBASE + 0x00u, 0x20u * 6u, 4 },
            K1, K2, { FBASE + 0x04u, 0x17u * 6u, 4 },
            K1, K2, { FBASE + 0x08u, 0x1Bu * 6u, 4 },
            K1, K2, { FBASE + 0x0Cu, 0x4650u * 6u, 4 },
            K1, K2, { FBASE + 0x14u, 0x18u * 6u, 4 },
            K1, K2, { FBASE + 0x18u, 0xF0u * 6u, 4 },
            K1, K2, { FBASE + R_CR, 0x4u, 4 },
            K1, K2, { FBASE + R_SLOCK0, 0u, 4 },
            K1, K2, { FBASE + R_SLOCK1, 0u, 4 },
        };
        check_seq(e, sizeof e / sizeof e[0]);
    }
    CHECK(flash_erase_sector(FLASH_SETTINGS_B));
    CHECK(flash_program(FLASH_SETTINGS_B, &word, 4));
    CHECK(memcmp(&host_flash[FLASH_SETTINGS_B], &word, 4) == 0);
    CHECK(host_regs[R_CR >> 2] == 0x4u);                  /* OP read, WAIT kept */
    check_invariants();

    /* ---- back at 4 MHz: WAIT = 0 again; ops refused until flash_init ---- */
    SystemCoreClock = 4000000u;
    CHECK(flash_set_wait(false));
    CHECK(host_regs[R_CR >> 2] == 0x0u);
    reset_log();
    CHECK(!flash_erase_sector(0x10000u));
    CHECK(host_nlog == 0);

    /* ---- an unsupported HCLK: init refuses before any register access ---- */
    SystemCoreClock = 8000000u;
    reset_log();
    CHECK(!flash_init());
    CHECK(host_nlog == 0);
    CHECK(!flash_erase_sector(0x10000u));
    SystemCoreClock = 4000000u;
    CHECK(flash_init());

    /* ---- code region never written by any of the above ---- */
    for (uint32_t a = 0; a < 0x10000u; a++)
        if (host_flash[a] != 0x00) {
            CHECK(host_flash[a] == 0x00);
            break;
        }

    if (fails)
        printf("%d failure(s)\n", fails);
    else
        printf("test_flash: all passed\n");
    return fails != 0;
}
