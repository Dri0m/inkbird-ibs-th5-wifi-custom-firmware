/* SWD-driven bench hooks, LINK_MBOX builds only (make LINK_MBOX=1).
 * The host writes a command word to `bench_cmd` (address from the ELF) and
 * polls until it reads 0; `bench_result` is 0 on success or the failing
 * step, `bench_val[]` holds measurements. Commands (notes-link.md):
 *
 *   0xF1A50001  slot B flash test at the current HCLK (4 MHz): erase the 49
 *               sectors, program a pattern over 25,088 B, read back
 *   0xF1A50011  the same at 24 MHz (clock_set_fast + flash_init), back to 4
 *   0xF1A50003  negative tests: erase/program outside the allowed ranges
 *               must fail; controller left in read mode, all locked
 *   0xF1A50005  erase slot B (cleanup)
 *   0xF1A50006  HCLK against the RTC crystal at 4 and 24 MHz; val[3]/[4] =
 *               the flash control word (flash_dbg_cr) at 24 MHz and after
 *
 * Refuses to touch slot B while it is the active frame. */
#ifdef LINK_MBOX
#include "bench.h"

#include "board.h"
#include "flash.h"
#include "frame.h"
#include "wdt.h"

volatile uint32_t bench_cmd;
volatile uint32_t bench_result;
volatile uint32_t bench_val[8];
uint32_t flash_dbg_cr(void);

static uint32_t cyc_since(uint32_t t0) { return (t0 - SYST_CVR) & 0x00FFFFFFu; }
static uint32_t us_of(uint32_t cyc) { return cyc / (SystemCoreClock / 1000000u); }

static uint8_t pat(uint32_t i) { return (uint8_t)(i * 7u + (i >> 9)); }

/* val: [0] erase us min, [1] erase us max, [2] program us total (25,088 B),
 * [4] HCLK, [5] sectors erased */
static uint32_t slot_b_test(void)
{
    if (frame_active() == (const frame_hdr_t *)FRAME_SLOT_B)
        return 100;
    if (!flash_init())
        return 1;
    uint32_t mn = 0xFFFFFFFFu, mx = 0, n = 0;
    for (uint32_t a = FRAME_SLOT_B; a < FRAME_SLOT_B + FRAME_SLOT_BYTES; a += FLASH_SECTOR_BYTES) {
        uint32_t t0 = SYST_CVR;
        if (!flash_erase_sector(a))
            return 2;
        uint32_t us = us_of(cyc_since(t0));
        if (us < mn) mn = us;
        if (us > mx) mx = us;
        n++;
    }
    bench_val[0] = mn;
    bench_val[1] = mx;
    bench_val[5] = n;
    static uint8_t buf[512];
    uint32_t total = 0;
    for (uint32_t off = 0; off < FRAME_SLOT_BYTES; off += sizeof buf) {
        for (uint32_t i = 0; i < sizeof buf; i++) buf[i] = pat(off + i);
        uint32_t t0 = SYST_CVR;
        if (!flash_program(FRAME_SLOT_B + off, buf, sizeof buf))
            return 3;
        total += cyc_since(t0);
        wdt_feed();
    }
    bench_val[2] = us_of(total);
    const uint8_t *f = (const uint8_t *)FRAME_SLOT_B;
    for (uint32_t i = 0; i < FRAME_SLOT_BYTES; i++)
        if (f[i] != pat(i))
            return 4;
    bench_val[4] = SystemCoreClock;
    return 0;
}

static uint32_t negative(void)
{
    static const uint8_t w[4] = {0, 0, 0, 0};
    if (!flash_init()) return 1;
    if (flash_erase_sector(0x0000u)) return 2;
    if (flash_erase_sector(0xFE00u)) return 3;
    if (flash_erase_sector(0x1C400u)) return 4;
    if (flash_program(0xFFFCu, w, 4)) return 5;
    if (flash_program(0x1C3FEu, w, 4)) return 6; /* runs past slot B */
    if (flash_erase_sector(0x16201u)) return 7;  /* unaligned */
    return 0;
}

static uint32_t erase_b(void)
{
    if (frame_active() == (const frame_hdr_t *)FRAME_SLOT_B)
        return 100;
    if (!flash_init()) return 1;
    for (uint32_t a = FRAME_SLOT_B; a < FRAME_SLOT_B + FRAME_SLOT_BYTES; a += FLASH_SECTOR_BYTES)
        if (!flash_erase_sector(a)) return 2;
    return 0;
}

/* SysTick cycles over one RTC second (crystal): HCLK in Hz. */
static uint32_t hclk_vs_rtc(void)
{
    uint32_t s0 = RTC_SEC;
    for (uint32_t i = 0; RTC_SEC == s0; i++) /* align to a second edge */
        if (i > 3000000u) return 0;
    uint32_t s1 = RTC_SEC, cyc = 0, last = SYST_CVR;
    while (RTC_SEC == s1) {
        uint32_t now = SYST_CVR;
        cyc += (last - now) & 0x00FFFFFFu;
        last = now;
        wdt_feed();
        if (cyc > 100000000u) return 0;
    }
    return cyc;
}

void bench_poll(void)
{
    uint32_t c = bench_cmd;
    if (!c)
        return;
    uint32_t r = 0xEEu;
    switch (c) {
    case 0xF1A50001u: r = slot_b_test(); break;
    case 0xF1A50011u:
        clock_set_fast(true);
        r = slot_b_test();
        clock_set_fast(false);
        (void)flash_init();
        break;
    case 0xF1A50003u: r = negative(); break;
    case 0xF1A50005u: r = erase_b(); break;
    case 0xF1A50006u:
        bench_val[0] = hclk_vs_rtc();
        clock_set_fast(true);
        bench_val[1] = hclk_vs_rtc();
        bench_val[3] = flash_dbg_cr(); /* WAIT = 1 at 24 MHz: 0x204 */
        clock_set_fast(false);
        bench_val[2] = hclk_vs_rtc();
        (void)flash_init();
        bench_val[4] = flash_dbg_cr(); /* back to 0x200 */
        r = bench_val[0] && bench_val[1] && bench_val[2] ? 0u : 1u;
        break;
    default: break;
    }
    bench_result = r;
    bench_cmd = 0;
}
#endif
