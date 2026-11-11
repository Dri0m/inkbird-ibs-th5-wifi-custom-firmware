/* Bootloader, 0x0000..0x0FFF (design 4.7, src/fwu.h). Installed once over
 * SWD, never written by the app.
 *
 * Every reset: power hold and CBU off (as board_early_init), then install a
 * committed staged image if there is one, then start the app at 0x1000.
 *
 *   staged image committed, CRC good, done erased -> install, then done = 0
 *   done = 0 but the installed app is not sane    -> install again
 *   no sane app and nothing to install            -> three long beeps,
 *                                                    power hold off, stop
 *
 * Flash safety, as src/flash.c: sector erase only (op 2), never op 3; only
 * the 2 KB lock group being written is unlocked, and everything is relocked
 * after each operation. Erase is allowed in the app region only; program in
 * the app region plus the staging header's done word. The bootloader's own
 * sectors (lock bits 0 and 1 of SLOCK0) are never unlocked.
 *
 * RAM: the bootloader uses only the top 2 KB (boot.ld), which the app keeps
 * clear of .noinit (hc32l170.ld). No interrupts are enabled.
 *
 * BOOT_HOST: compiled on the host by test/test_boot.c, with flash memory and
 * the erase/program primitives supplied by the test. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "fwu.h"

#ifdef BOOT_HOST
extern uint8_t host_flash[0x20000];
#define MEM8(a)  (host_flash + (a))
#define MEM32(a) (*(const uint32_t *)(host_flash + (a)))
bool fl_erase(uint32_t addr);
bool fl_program(uint32_t addr, const uint8_t *src, uint32_t n);
void fl_feed(void);
#else
#include "board.h"
#define MEM8(a)  ((const uint8_t *)(uintptr_t)(a))
#define MEM32(a) (*(const volatile uint32_t *)(uintptr_t)(a))
static bool fl_erase(uint32_t addr);
static bool fl_program(uint32_t addr, const uint8_t *src, uint32_t n);
static void fl_feed(void);
#endif

#define SECTOR 512u

/* ---- checks (pure reads) -------------------------------------------- */

static uint32_t crc32(const uint8_t *p, uint32_t n)
{
    static const uint32_t t[16] = {
        0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C,
        0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C, 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C,
    };
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < n; i++) {
        crc ^= p[i];
        crc = (crc >> 4) ^ t[crc & 15u];
        crc = (crc >> 4) ^ t[crc & 15u];
        if ((i & 0xFFFu) == 0u)
            fl_feed();
    }
    return ~crc;
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* A vector table and info block that can be started at 0x1000: SP in RAM,
 * Thumb reset vector inside [0x1000, end), app magic at +0xC0. */
static bool image_sane(const uint8_t *img, uint32_t end)
{
    uint32_t sp = rd32(img), rv = rd32(img + 4);
    return sp > FWU_RAM_BASE && sp <= FWU_RAM_END && (sp & 3u) == 0u &&
           (rv & 1u) && (rv & ~1u) >= FWU_APP_BASE + FWU_INFO_OFF + 8u && (rv & ~1u) < end &&
           rd32(img + FWU_INFO_OFF) == FWU_APP_MAGIC;
}

static bool app_sane(void)
{
    return image_sane(MEM8(FWU_APP_BASE), FWU_APP_END);
}

/* Header fields this bootloader can install, before any CRC work. */
static bool staged_fields_ok(const fwu_hdr_t *h)
{
    return h->magic == FWU_MAGIC && h->commit == FWU_SET && h->load == FWU_APP_BASE &&
           h->min_boot_ver <= FWU_BOOT_VER && h->length >= FWU_MIN_IMAGE &&
           h->length <= FWU_MAX_IMAGE && (h->length & 3u) == 0u &&
           image_sane(MEM8(FWU_STAGE + FWU_HDR_BYTES), FWU_APP_BASE + h->length);
}

/* ---- install --------------------------------------------------------- */

static bool install(const fwu_hdr_t *h)
{
    const uint8_t *src = MEM8(FWU_STAGE + FWU_HDR_BYTES);
    for (uint32_t off = 0; off < h->length; off += SECTOR) {
        uint32_t n = h->length - off < SECTOR ? h->length - off : SECTOR;
        if (!fl_erase(FWU_APP_BASE + off) || !fl_program(FWU_APP_BASE + off, src + off, n))
            return false;
    }
    if (crc32(MEM8(FWU_APP_BASE), h->length) != h->crc32)
        return false;
    /* The app is verified: a done word that fails to program only means
     * the next reset installs (and verifies) it again. */
    static const uint8_t zero[4];
    (void)fl_program(FWU_STAGE + offsetof(fwu_hdr_t, done), zero, 4u);
    return true;
}

typedef enum { BOOT_APP, BOOT_INSTALLED, BOOT_NO_APP } boot_result_t;

/* Everything between the power hold and the jump; host-testable. */
boot_result_t boot_run(void)
{
    const fwu_hdr_t *h = (const fwu_hdr_t *)MEM8(FWU_STAGE);
    bool want = staged_fields_ok(h) && (h->done == FWU_ERASED || !app_sane());
    if (want && crc32(MEM8(FWU_STAGE + FWU_HDR_BYTES), h->length) == h->crc32) {
        /* One retry: an erase or program that does not verify is rare and
         * the image is still staged, so a second pass is cheap. */
        /* A failed install leaves the app region part erased: never start
         * it. done stays erased, so the next reset tries again. */
        if (!(install(h) || install(h)) || !app_sane())
            return BOOT_NO_APP;
        return BOOT_INSTALLED;
    }
    return app_sane() ? BOOT_APP : BOOT_NO_APP;
}

#ifndef BOOT_HOST

/* ---- info block and vectors ------------------------------------------ */

extern uint32_t _estack;
void Reset_Handler(void);
/* Drop the power hold (on battery that cuts power; after a power-on the
 * WDT is stopped, so spinning powered would drain the battery). On USB the
 * WDT, if running, resets; otherwise SWD recovers. */
static void Fault_Handler(void)
{
    GPIO_BCLR(PORT_A) = 1u << 8; /* PIN_PWR_HOLD low */
    for (;;)
        __asm volatile("wfi");
}

__attribute__((section(".vectors"), used))
void (*const vectors[48])(void) = {
    (void (*)(void))&_estack,
    Reset_Handler,
    Fault_Handler, /* NMI */
    Fault_Handler, /* HardFault */
};

__attribute__((section(".info"), used))
const fwu_info_t boot_info = { FWU_BOOT_MAGIC, FWU_BOOT_VER };

/* ---- flash driver (subset of src/flash.c, see there for sources) ----- */

#define KEY1 0x5A5Au
#define KEY2 0xA5A5u
#define CR_OP_MASK   0x3u
#define CR_WAIT_MASK (0x3u << 2)
#define CR_BUSY      (1u << 4)
#define OP_READ      0u
#define OP_PROGRAM   1u
#define OP_SECTOR_ER 2u
#define GROUP_SHIFT  11u  /* one SLOCK bit per 2 KB */
#define POLL_MAX     0x20000u

/* Only while the WDT runs: the feed key also starts a stopped WDT. */
static void fl_feed(void)
{
    if (!(WDT_CON & (1u << 4)))
        return;
    WDT_RST = 0x1Eu;
    WDT_RST = 0xE1u;
}

static bool prot_write(volatile uint32_t *reg, uint32_t val, uint32_t mask)
{
    for (unsigned tries = 0; tries < 3u; tries++) {
        FLASH_BYPASS = KEY1;
        FLASH_BYPASS = KEY2;
        *reg = val;
        if ((*reg & mask) == (val & mask))
            return true;
    }
    return false;
}

static bool wait_idle(void)
{
    for (uint32_t n = 0; n < POLL_MAX; n++)
        if (!(FLASH_CR & CR_BUSY))
            return true;
    return false;
}

static bool set_op(uint32_t op)
{
    if (op > OP_SECTOR_ER)
        return false;
    return prot_write(&FLASH_CR, (FLASH_CR & ~(CR_OP_MASK | CR_WAIT_MASK)) | op,
                      CR_OP_MASK | CR_WAIT_MASK);
}

/* Erase: app region only. Program: app region, or the staging done word. */
static bool allowed(uint32_t addr, uint32_t n, bool erase)
{
    if (n == 0u || addr < FWU_APP_BASE)
        return false;
    if (addr < FWU_APP_END)
        return n <= FWU_APP_END - addr;
    return !erase && addr == FWU_STAGE + offsetof(fwu_hdr_t, done) && n == 4u;
}

/* Unlock the one 2 KB group holding [addr, addr + n) (callers stay inside
 * a sector) and set op. */
static bool op_begin(uint32_t addr, uint32_t op)
{
    uint32_t bit = addr >> GROUP_SHIFT;
    if (bit < 2u || bit > 63u || !wait_idle())
        return false;
    bool lo = bit < 32u;
    if (!prot_write(lo ? &FLASH_SLOCK0 : &FLASH_SLOCK1, 1u << (bit & 31u), 0xFFFFFFFFu))
        return false;
    return set_op(op);
}

static bool op_end(void)
{
    bool ok = wait_idle();
    ok = set_op(OP_READ) && ok;
    ok = prot_write(&FLASH_SLOCK0, 0u, 0xFFFFFFFFu) && ok;
    ok = prot_write(&FLASH_SLOCK1, 0u, 0xFFFFFFFFu) && ok;
    return ok;
}

static bool fl_erase(uint32_t addr)
{
    if (addr % SECTOR || !allowed(addr, SECTOR, true))
        return false;
    fl_feed();
    bool ok = op_begin(addr, OP_SECTOR_ER);
    if (ok) {
        REG32(addr) = 0u; /* any write in the sector starts the erase */
        ok = wait_idle();
    }
    ok = op_end() && ok;
    for (uint32_t off = 0; ok && off < SECTOR; off += 4u)
        ok = MEM32(addr + off) == 0xFFFFFFFFu;
    return ok;
}

/* Words only (addr and n multiples of 4, inside one sector). src is copied
 * through RAM first: no flash data reads while the controller programs. */
static bool fl_program(uint32_t addr, const uint8_t *src, uint32_t n)
{
    static uint32_t buf[SECTOR / 4u];
    if ((addr | n) & 3u || n > SECTOR || (addr % SECTOR) + n > SECTOR || !allowed(addr, n, false))
        return false;
    for (uint32_t i = 0; i < n / 4u; i++)
        buf[i] = rd32(src + 4u * i);
    fl_feed();
    bool ok = op_begin(addr, OP_PROGRAM);
    for (uint32_t i = 0; ok && i < n / 4u; i++) {
        uint32_t a = addr + 4u * i;
        if (MEM32(a) == buf[i])
            continue;
        if ((MEM32(a) & buf[i]) != buf[i]) {
            ok = false; /* would need a 0 -> 1 bit */
            break;
        }
        REG32(a) = buf[i];
        ok = wait_idle() && MEM32(a) == buf[i];
    }
    ok = op_end() && ok;
    return ok;
}

/* ---- board ----------------------------------------------------------- */

#define TRIM_RCH_4M (*(volatile const uint16_t *)0x00100C08u)

static void systick_wait(uint32_t cycles)
{
    for (uint32_t used = 0, last = SYST_CVR; used < cycles;) {
        uint32_t cvr = SYST_CVR;
        used += (last - cvr) & 0x00FFFFFFu;
        last = cvr;
    }
}

static void out(uint32_t port, uint32_t pin, bool level)
{
    GPIO_SEL(port, pin) = 0;
    GPIO_ADS(port) &= ~(1u << pin);
    pin_write(port, pin, level);
    GPIO_DIR(port) &= ~(1u << pin);
}

/* Three 400 ms beeps at 2 kHz (no LED on this board). */
static void beep_error(void)
{
    out(PIN_BUZZER, false);
    for (unsigned b = 0; b < 3u; b++) {
        for (unsigned c = 0; c < 800u; c++) {
            pin_set(PIN_BUZZER);
            systick_wait(1000u);
            pin_clr(PIN_BUZZER);
            systick_wait(1000u);
        }
        fl_feed();
        systick_wait(1600000u); /* 400 ms */
        fl_feed();
    }
}

void Reset_Handler(void)
{
    /* As board_early_init: CBU off, then (2 ms after the pull-up) hold the
     * power only if the slide switch is on. */
    SYSCTRL_PERI_CLKEN0 |= CLK_GPIO;
    out(PIN_CBU_PWR, true);
    GPIO_SEL(PORT_A, 9) = 0;
    GPIO_ADS(PORT_A) &= ~(1u << 9);
    GPIO_DIR(PORT_A) |= 1u << 9;
    GPIO_PU(PORT_A) |= 1u << 9;
    SYST_RVR = 0x00FFFFFFu;
    SYST_CVR = 0;
    SYST_CSR = 5u;
    systick_wait(8000u);
    out(PIN_PWR_HOLD, pin_read(PIN_PWR_SW));

    /* WDT: survives resets but its bus clock gate does not. Feed only if it
     * runs: the feed key would start a stopped WDT. */
    SYSCTRL_PERI_CLKEN0 |= CLK_WDT | CLK_FLASH;
    fl_feed();

    /* 4 MHz RCH, trimmed (the flash timings below are for 4 MHz). */
    SYSCTRL_RCH_CR = (SYSCTRL_RCH_CR & ~0x7FFu) | (TRIM_RCH_4M & 0x7FFu);
    static const struct { volatile uint32_t *reg; uint32_t val; } timing[] = {
        { &FLASH_TNVS, 0x20u }, { &FLASH_TPGS, 0x17u }, { &FLASH_TPROG, 0x1Bu },
        { &FLASH_TSERASE, 0x4650u }, { &FLASH_TPRCV, 0x18u }, { &FLASH_TSRCV, 0xF0u },
    };
    bool ok = wait_idle();
    for (unsigned i = 0; ok && i < sizeof timing / sizeof timing[0]; i++)
        ok = prot_write(timing[i].reg, timing[i].val, 0xFFFFFFFFu);
    ok = ok && set_op(OP_READ);

    boot_result_t r = ok ? boot_run() : (app_sane() ? BOOT_APP : BOOT_NO_APP);
    if (r == BOOT_NO_APP) {
        beep_error();
        pin_clr(PIN_PWR_HOLD);
        for (;;)
            __asm volatile("wfi");
    }

    /* Start the app: SysTick off, its vector table, its stack. */
    SYST_CSR = 0;
    SCB_VTOR = FWU_APP_BASE;
    uint32_t sp = MEM32(FWU_APP_BASE), rv = MEM32(FWU_APP_BASE + 4u);
    __asm volatile("dsb\n isb\n msr msp, %0\n bx %1" ::"r"(sp), "r"(rv) : "memory");
    for (;;) {
    }
}

#endif
