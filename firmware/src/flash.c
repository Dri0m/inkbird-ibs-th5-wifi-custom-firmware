/* In-application flash erase/program, frame slots and settings sectors only.
 * Register facts and sources: firmware/notes-flash.md. Source key there:
 *   H    vendor header HC32L170FAUA.h (stc_flash_*_field_t, M0P_FLASH_TypeDef)
 *   DDL  XHSC HC32L17x DDL driver/src/flash.c, driver/inc/flash.h (BSD-3)
 *   FLM  pack HDSC.HC32L17X.1.0.1 Flash/FlashHC32L17X_128K.FLM (the SWD algorithm)
 *   O    stock dump evidence/dumps/hc32-stock-1.bin at the given address
 *
 * Safety: the only erase/program targets are the ranges in flash_allowed(),
 * checked before any register is touched. The controller op is only ever set
 * to read (0), program (1) or sector erase (2). Only the SLOCK1 bit covering
 * the target is unlocked, and SLOCK1 is written back to 0 (all locked) after
 * every operation, as stock 0x30A4 does. SLOCK0 (code, 0x00000..0x0FFFF) is
 * only ever written with 0. Nothing here touches SWD pins, SYSCTRL keys or
 * the factory info area. */
#include "flash.h"

#include <stddef.h>

#include "board.h"
#include "frame.h"

#ifdef FLASH_HOST_TEST
#include "flash_host.h" /* firmware/test: logs MMIO, models the controller */
#define feed() wdt_feed() /* the host model counts feeds */
#else
static inline uint32_t io_rd32(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline void io_wr32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint8_t io_rd8(uintptr_t a) { return *(volatile uint8_t *)a; }
static inline void io_wr8(uintptr_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
static inline uint32_t irq_save(void)
{
    uint32_t pm;
    __asm volatile("mrs %0, primask\n cpsid i" : "=r"(pm)::"memory");
    return pm;
}
static inline void irq_restore(uint32_t pm)
{
    __asm volatile("msr primask, %0" ::"r"(pm) : "memory");
}
/* WDT feed key written here rather than calling wdt_feed(): everything that
 * runs while an erase or program is open must lie in flash.o, which the
 * linker script places below 32 KB (FLASH operation source defence), and
 * the build checks that flash.o calls nothing outside itself (Makefile,
 * flash-selfcontained). Same key as wdt.c (DDL Wdt_Feed: 0x1E, 0xE1). */
static inline void feed(void)
{
    io_wr32((uintptr_t)&WDT_RST, 0x1Eu);
    io_wr32((uintptr_t)&WDT_RST, 0xE1u);
}
#endif

/* Register addresses from regs.h (generated from H); only the address is
 * taken here, every access goes through io_rd32/io_wr32. */
#define A_TNVS     ((uintptr_t)&FLASH_TNVS)
#define A_TPGS     ((uintptr_t)&FLASH_TPGS)
#define A_TPROG    ((uintptr_t)&FLASH_TPROG)
#define A_TSERASE  ((uintptr_t)&FLASH_TSERASE)
#define A_TPRCV    ((uintptr_t)&FLASH_TPRCV)
#define A_TSRCV    ((uintptr_t)&FLASH_TSRCV)
#define A_CR       ((uintptr_t)&FLASH_CR)
#define A_BYPASS   ((uintptr_t)&FLASH_BYPASS)
#define A_SLOCK0   ((uintptr_t)&FLASH_SLOCK0)
#define A_SLOCK1   ((uintptr_t)&FLASH_SLOCK1)
#define A_CLKEN0   ((uintptr_t)&SYSCTRL_PERI_CLKEN0)

/* FLASH_CR (H stc_flash_cr_field_t): OP[1:0], WAIT[3:2], BUSY bit 4,
 * IE[6:5], DPSTB_EN bit 9. Only OP and WAIT are ever changed here
 * (read-modify-write, as stock 0x30D8/0x3194 and FLM EraseSector do:
 * (CR >> 2 << 2) + op). WAIT is the read wait-state count: DDL
 * (Flash_WaitCycle, en_flash_waitcycle_t) uses 0 for HCLK <= 24 MHz and 1
 * above; the DS IDD table likewise runs 24 MHz without and 32-48 MHz with
 * FlashWait = 1. The RCH "24 MHz" trim measures 24.17 MHz on this unit
 * (notes-link.md), just over that line, so link sessions run with WAIT = 1
 * (flash_set_wait); one extra cycle per flash fetch is harmless at 4 MHz. */
#define CR_OP_MASK   0x3u
#define CR_WAIT_SHIFT 2u
#define CR_WAIT_MASK (0x3u << CR_WAIT_SHIFT)
#define CR_BUSY      (1u << 4)
/* OP values (DDL en_flash_op_mode_t). Value 3 is the whole-array erase and
 * is deliberately not defined; cr_set_op() refuses anything above 2. */
#define OP_READ      0u
#define OP_PROGRAM   1u
#define OP_SECTOR_ER 2u

/* Write 0x5A5A then 0xA5A5 to BYPASS before every write to a protected
 * FLASH register (DDL FLASH_BYPASS(); O 0x30A4/0x30D8/0x315C; FLM). */
#define KEY1 0x5A5Au
#define KEY2 0xA5A5u

/* SLOCKn (DDL Flash_LockSet note): bit i of SLOCKn = sectors
 * [128n + 4i, 128n + 4i + 3], i.e. one bit per 2 KB; 1 = erase/program
 * allowed, 0 = locked. Unlock all = 0xFFFFFFFF, lock all = 0
 * (DDL Flash_UnlockAll/LockAll, O 0x315C/0x30A4, FLM Init/UnInit). */
#define LOCK_GROUP_SHIFT 11u  /* 2 KB */

/* Program/erase timings in HCLK cycles for 4 MHz (DDL pu32PcgTimer4M with
 * Flash_Init(u8FreqCfg = 1); FLM Init writes the same values). At 4 MHz:
 * Tnvs 8 us, Tpgs 5.75 us, Tprog 6.75 us, Tserase 4.5 ms, Tprcv 6 us,
 * Tsrcv 60 us. The two whole-array timings (TMERASE 0x222E0, TMRCV 0x3E8)
 * are left alone: they only time an operation this driver never issues.
 * At 24 MHz (link sessions, notes-link.md) every count is multiplied by 6,
 * exactly as DDL Flash_Init does for u8FreqCfg = 6 (FlashInit24M...). Any
 * other HCLK is refused. */
static const struct { uintptr_t reg; uint32_t val; } k_timing_4m[] = {
    { A_TNVS, 0x20u }, { A_TPGS, 0x17u }, { A_TPROG, 0x1Bu },
    { A_TSERASE, 0x4650u }, { A_TPRCV, 0x18u }, { A_TSRCV, 0xF0u },
};
#define HCLK_4M  4000000u
#define HCLK_24M 24000000u

/* BUSY poll bound. At 4 MHz one poll iteration is >= ~6 cycles (1.5 us), so
 * 0x20000 polls is >= ~200 ms, 40x the 4-5 ms page erase (DS) and far under
 * the 1.64 s worst-case WDT period; scaled with the clock factor. If the bus
 * stalls during the operation the poll simply resumes after it. */
#define POLL_MAX 0x20000u
#define FEED_EVERY 64u /* units between WDT feeds: 64 x ~50 us word program */

static bool s_ready;
static uint32_t s_hclk;   /* SystemCoreClock the timings were written for */
static uint32_t s_factor = 1u;

/* ---- range check (pure, no register access) ------------------------- */

static bool in_range(uint32_t addr, uint32_t len, uint32_t lo, uint32_t end)
{
    return addr >= lo && addr < end && len <= end - addr;
}

bool flash_allowed(uint32_t addr, uint32_t len)
{
    if (len == 0u)
        return false;
    /* Slots A and B as one range: together they are also the MCU firmware
     * staging area (fwu.h), whose writes cross the A/B boundary. */
    return in_range(addr, len, FRAME_SLOT_A, FRAME_SLOT_B + FRAME_SLOT_BYTES) ||
           in_range(addr, len, FLASH_SETTINGS_A, FLASH_SETTINGS_B + FLASH_SECTOR_BYTES);
}

_Static_assert(FRAME_SLOT_A + FRAME_SLOT_BYTES == FRAME_SLOT_B, "slots adjacent");
_Static_assert(FRAME_SLOT_A >= 0x10000u, "slots must stay above the 64 KB code region");
_Static_assert(FLASH_SETTINGS_B + FLASH_SECTOR_BYTES == 0x20000u, "128 KB part");
_Static_assert((FRAME_SLOT_A | FRAME_SLOT_B | FLASH_SETTINGS_A) % FLASH_SECTOR_BYTES == 0, "sector aligned");

/* ---- controller primitives ------------------------------------------- */

/* Key + write + read back with IRQs masked, so the two key writes and the
 * protected write are back to back (DDL does not mask; nothing in our ISRs
 * touches FLASH, but the window is a few cycles). Retried like stock's
 * OP loop (0x3108..0x3128) and DDL Flash_Init. */
static bool prot_write(uintptr_t reg, uint32_t val, uint32_t check_mask)
{
    for (unsigned tries = 0; tries < 3u; tries++) {
        uint32_t pm = irq_save();
        io_wr32(A_BYPASS, KEY1);
        io_wr32(A_BYPASS, KEY2);
        io_wr32(reg, val);
        uint32_t rb = io_rd32(reg);
        irq_restore(pm);
        if ((rb & check_mask) == (val & check_mask))
            return true;
    }
    return false;
}

/* FLASH_CR as read now (bench.c: WAIT and OP check). */
uint32_t flash_dbg_cr(void) { return io_rd32(A_CR); }

static bool wait_idle(void)
{
    uint32_t max = POLL_MAX * s_factor;
    for (uint32_t n = 0; n < max; n++)
        if (!(io_rd32(A_CR) & CR_BUSY))
            return true;
    return false;
}

static uint32_t s_wait; /* CR.WAIT to keep: 0 or 1 */

static bool cr_set_op(uint32_t op)
{
    if (op > OP_SECTOR_ER)
        return false;
    uint32_t v = (io_rd32(A_CR) & ~(CR_OP_MASK | CR_WAIT_MASK)) | s_wait << CR_WAIT_SHIFT | op;
    return prot_write(A_CR, v, CR_OP_MASK | CR_WAIT_MASK);
}

/* Unlock the one 2 KB group holding addr, set the op. Only SLOCK1 (upper
 * 64 KB) is ever unlocked; every allowed address is there. */
static bool op_begin(uint32_t addr, uint32_t op)
{
    uint32_t bit = addr >> LOCK_GROUP_SHIFT;
    if (bit < 32u || bit > 63u)
        return false;
    if (!wait_idle())
        return false;
    if (!prot_write(A_SLOCK1, 1u << (bit - 32u), 0xFFFFFFFFu))
        return false;
    return cr_set_op(op);
}

/* Always run after op_begin, also on failure: back to read mode, all locked
 * (stock 0x30A4 relocks; it leaves OP as it was, we return it to read). */
static bool op_end(void)
{
    bool ok = wait_idle();
    ok = cr_set_op(OP_READ) && ok;
    ok = prot_write(A_SLOCK1, 0u, 0xFFFFFFFFu) && ok;
    return ok;
}

static bool ready(void)
{
    return s_ready && SystemCoreClock == s_hclk;
}

/* ---- API ------------------------------------------------------------- */

bool flash_init(void)
{
    s_ready = false;
    if (SystemCoreClock != HCLK_4M && SystemCoreClock != HCLK_24M)
        return false;
    uint32_t factor = SystemCoreClock == HCLK_24M ? 6u : 1u; /* no libgcc division */

    /* Bus clock gate PERI_CLKEN0 bit 31 (H stc_sysctrl_peri_clken0_field_t
     * FLASH; board.h CLK_FLASH). FLM Init sets it before touching FLASH. */
    io_wr32(A_CLKEN0, io_rd32(A_CLKEN0) | CLK_FLASH);

    s_factor = factor;
    bool ok = wait_idle();
    for (size_t i = 0; ok && i < sizeof k_timing_4m / sizeof k_timing_4m[0]; i++)
        ok = prot_write(k_timing_4m[i].reg, k_timing_4m[i].val * factor, 0xFFFFFFFFu);
    ok = ok && cr_set_op(OP_READ);
    /* Lock everything (DDL Flash_LockAll, O 0x30A4, FLM UnInit). Locking
     * only removes write permission, so writing SLOCK0 = 0 is safe. */
    ok = ok && prot_write(A_SLOCK0, 0u, 0xFFFFFFFFu);
    ok = ok && prot_write(A_SLOCK1, 0u, 0xFFFFFFFFu);
    s_hclk = SystemCoreClock;
    s_ready = ok;
    return ok;
}

bool flash_set_wait(bool one)
{
    s_wait = one ? 1u : 0u;
    io_wr32(A_CLKEN0, io_rd32(A_CLKEN0) | CLK_FLASH);
    return wait_idle() && cr_set_op(OP_READ);
}

bool flash_erase_sector(uint32_t addr)
{
    if (addr % FLASH_SECTOR_BYTES || !flash_allowed(addr, FLASH_SECTOR_BYTES))
        return false;
    if (!ready())
        return false;

    feed();
    bool ok = op_begin(addr, OP_SECTOR_ER);
    if (ok) {
        /* Any write inside the sector starts the erase (DDL
         * Flash_SectorErase writes word 0; O 0x312C writes byte 0). */
        io_wr32(addr, 0u);
        ok = wait_idle();
    }
    ok = op_end() && ok;

    for (uint32_t off = 0; ok && off < FLASH_SECTOR_BYTES; off += 4u)
        ok = io_rd32(addr + off) == 0xFFFFFFFFu;
    return ok;
}

/* One unit (4 or 1 bytes) inside an open op. */
static bool prog_word(uint32_t a, uint32_t v)
{
    uint32_t cur = io_rd32(a);
    if (cur == v)
        return true;
    if ((cur & v) != v)
        return false; /* needs a 1 bit back: erase first */
    io_wr32(a, v);
    return wait_idle() && io_rd32(a) == v;
}

static bool prog_byte(uint32_t a, uint8_t v)
{
    uint8_t cur = io_rd8(a);
    if (cur == v)
        return true;
    if ((uint8_t)(cur & v) != v)
        return false;
    io_wr8(a, v);
    return wait_idle() && io_rd8(a) == v;
}

bool flash_program(uint32_t addr, const void *src, uint32_t len)
{
    if (!flash_allowed(addr, len) || src == NULL)
        return false;
    if (!ready())
        return false;

    const uint8_t *s = src;
    bool ok = true;
    while (ok && len) {
        /* One lock group (2 KB) per op_begin/op_end. */
        uint32_t group_end = (addr | ((1u << LOCK_GROUP_SHIFT) - 1u)) + 1u;
        uint32_t n = group_end - addr < len ? group_end - addr : len;
        uint32_t a = addr, left = n;
        unsigned since_feed = 0;

        feed();
        ok = op_begin(addr, OP_PROGRAM);
        while (ok && left) {
            if ((a & 3u) == 0 && left >= 4u) {
                uint32_t w = (uint32_t)s[0] | (uint32_t)s[1] << 8 | (uint32_t)s[2] << 16 |
                             (uint32_t)s[3] << 24; /* little-endian, any alignment */
                ok = prog_word(a, w);
                a += 4u; s += 4u; left -= 4u;
            } else {
                ok = prog_byte(a, *s);
                a += 1u; s += 1u; left -= 1u;
            }
            if (++since_feed >= FEED_EVERY) {
                feed();
                since_feed = 0;
            }
        }
        ok = op_end() && ok;
        addr += n;
        len -= n;
    }
    return ok;
}
