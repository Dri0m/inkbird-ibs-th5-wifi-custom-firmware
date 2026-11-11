/* Real-time clock on XTL. Register facts and sources: firmware/notes-rtc-power.md. */
#include "rtc.h"

#include "board.h"
#include "power.h"

/* SYSCTRL0 enables (vendor header stc_sysctrl_sysctrl0_field_t) */
#define SC0_XTL_EN        (1u << 3)

/* XTL_CR (stc_sysctrl_xtl_cr_field_t): DRIVER[1:0], AMP_SEL[3:2], STARTUP[5:4], STABLE bit 6 */
#define XTL_CR_CFG_MASK   0x3Fu
#define XTL_CR_RECOMMENDED 0x3Au /* [3:0]=0b1010 (datasheet 7.3.x "推荐值"), STARTUP=0b11 (16384 cycles) */
#define XTL_CR_STRONG     0x3Fu  /* max amplitude and drive (DDL Sysctrl_ClkInit uses Amp3/Driver3) */
#define XTL_CR_STABLE     (1u << 6)
#define XTL_WAIT_MS       3000u  /* datasheet Tstart typ 2000 ms */
#define XTL_RETRY_MS      2000u

/* PC14/PC15 = XTL pins, must be analog (DDL Sysctrl_ClkSourceEnable XTL case:
 * PCADS |= 0xC000; stock does the same at 0x622C). Port C block is +0x80. */
#define PORT_C_OFS        0x0080u
#define PC14_PC15         0xC000u

/* RTC_CR0 (stc_rtc_cr0_field_t) */
#define CR0_PRDS_MASK     0x7u
#define CR0_PRDS_1MIN     3u          /* DDL en_rtc_prds_t Rtc1Min */
#define CR0_AMPM          (1u << 3)   /* 1 = 24 h (DDL RtcPm "24小时制") */
#define CR0_HZ1OE         (1u << 5)
#define CR0_START         (1u << 7)
#define CR0_PRDSEL        (1u << 14)  /* 0 = use PRDS */

/* RTC_CR1 (stc_rtc_cr1_field_t). Flags clear by writing 0 (DDL
 * Rtc_ClearPrdfItStatus); write 1 back to leave them alone. */
#define CR1_WAIT          (1u << 0)
#define CR1_WAITF         (1u << 1)
#define CR1_PRDF          (1u << 3)
#define CR1_ALMF          (1u << 4)
#define CR1_IE            (1u << 6)   /* RTC interrupt enable (header calls it ALMIE; stock sets it for the period irq) */
#define CR1_ALMEN         (1u << 7)
#define CR1_CKSEL_SHIFT   8u
#define CR1_CKSEL_MASK    (7u << CR1_CKSEL_SHIFT)
#define CKSEL_XTL         0u          /* DDL RtcClkXtl */
#define CKSEL_RCL         2u          /* DDL RtcClkRcl */

#define HOUR12_PM         0x20u       /* 12 h mode: bit 5 = PM (DDL masks HOUR & 0x1F in 12 h mode) */

static bool s_running_before;

static uint8_t bcd2bin(uint32_t v) { return (uint8_t)(((v >> 4) & 0xFu) * 10u + (v & 0xFu)); }
static uint32_t bin2bcd(uint8_t v) { return ((uint32_t)(v / 10u) << 4) | (v % 10u); }

/* Wait for WAITF to reach `want`, at most about 2 ms. WAITF follows WAIT
 * after a couple of RTC clock edges (61 us each at 32.768 kHz). */
static bool wait_waitf(bool want)
{
    for (uint32_t i = 0; i < 200u; i++) {
        if (((RTC_CR1 & CR1_WAITF) != 0u) == want)
            return true;
        delay_us(10);
    }
    return false;
}

/* WAIT freezes the counter-to-register transfer so all fields form one
 * snapshot and become writable (DDL Rtc_ReadDateTime / Rtc_SetTime). */
static void lock_regs(void)
{
    if (!(RTC_CR0 & CR0_START))
        return;
    RTC_CR1 = RTC_CR1 | CR1_WAIT | CR1_PRDF | CR1_ALMF;
    (void)wait_waitf(true);
}

static void unlock_regs(void)
{
    RTC_CR1 = (RTC_CR1 & ~CR1_WAIT) | CR1_PRDF | CR1_ALMF;
    if (RTC_CR0 & CR0_START)
        (void)wait_waitf(false);
}

static void read_regs(rtc_time_t *t)
{
    uint32_t hour = RTC_HOUR;
    t->sec = bcd2bin(RTC_SEC & 0x7Fu);
    t->min = bcd2bin(RTC_MIN & 0x7Fu);
    if (RTC_CR0 & CR0_AMPM) {
        t->hour = bcd2bin(hour & 0x3Fu);
    } else {
        uint8_t h = (uint8_t)(bcd2bin(hour & 0x1Fu) % 12u);
        t->hour = (uint8_t)(h + ((hour & HOUR12_PM) ? 12u : 0u));
    }
    t->wday = (uint8_t)(RTC_WEEK & 0x7u);
    t->day = bcd2bin(RTC_DAY & 0x3Fu);
    t->month = bcd2bin(RTC_MON & 0x1Fu);
    t->year = bcd2bin(RTC_YEAR & 0xFFu);
}

static void write_regs(const rtc_time_t *t)
{
    RTC_SEC = bin2bcd(t->sec);
    RTC_MIN = bin2bcd(t->min);
    RTC_HOUR = bin2bcd(t->hour); /* 24 h mode only: init forces AMPM = 1 */
    RTC_DAY = bin2bcd(t->day);
    RTC_MON = bin2bcd(t->month);
    RTC_YEAR = bin2bcd(t->year);
    RTC_WEEK = t->wday;
}

static bool xtl_wait(uint32_t ms)
{
    for (uint32_t i = 0; i < ms; i += 10u) {
        if (SYSCTRL_XTL_CR & XTL_CR_STABLE)
            return true;
        delay_ms(10); /* feeds the WDT */
    }
    return (SYSCTRL_XTL_CR & XTL_CR_STABLE) != 0u;
}

static void xtl_config(uint32_t cfg)
{
    /* Only touched while XTL_EN = 0. */
    SYSCTRL_XTL_CR = (SYSCTRL_XTL_CR & ~XTL_CR_CFG_MASK) | cfg;
}

static bool xtl_start(void)
{
    GPIO_ADS(PORT_C_OFS) |= PC14_PC15;

    if (SYSCTRL0 & SC0_XTL_EN) {
        /* Warm reset: already enabled (and the RTC may be counting on it).
         * Don't touch XTL_CR, just wait for STABLE. */
        if (xtl_wait(XTL_WAIT_MS))
            return true;
        power_sysctrl0_set(SC0_XTL_EN, false);
    }

    xtl_config(XTL_CR_RECOMMENDED);
    power_sysctrl0_set(SC0_XTL_EN, true);
    if (xtl_wait(XTL_WAIT_MS))
        return true;

    /* Retry once with full amplitude and drive. */
    power_sysctrl0_set(SC0_XTL_EN, false);
    xtl_config(XTL_CR_STRONG);
    power_sysctrl0_set(SC0_XTL_EN, true);
    if (xtl_wait(XTL_RETRY_MS))
        return true;

    power_sysctrl0_set(SC0_XTL_EN, false);
    return false;
}

static void rtc_restart(uint32_t cksel, const rtc_time_t *t)
{
    RTC_CR0 = RTC_CR0 & ~CR0_START;
    RTC_CR0 = CR0_AMPM | CR0_PRDS_1MIN; /* PRDSEL = 0, 1 Hz out off */
    RTC_CR1 = (cksel << CR1_CKSEL_SHIFT) | CR1_PRDF | CR1_ALMF; /* irq off for now, alarm off */
    RTC_COMPEN = 0u;
    write_regs(t);
    RTC_CR0 = RTC_CR0 | CR0_START;
}

bool rtc_init(void)
{
    SYSCTRL_PERI_CLKEN0 |= CLK_RTC | CLK_GPIO;

    bool xtl_ok = xtl_start();
    uint32_t cksel = CKSEL_XTL;
    if (!xtl_ok) {
        (void)power_rcl_enable();
        cksel = CKSEL_RCL;
    }

    uint32_t cr0 = RTC_CR0;
    uint32_t cur_cksel = (RTC_CR1 & CR1_CKSEL_MASK) >> CR1_CKSEL_SHIFT;
    s_running_before = (cr0 & CR0_START) != 0u;

    if (s_running_before) {
        if (cur_cksel != cksel || !(cr0 & CR0_AMPM)) {
            /* Keep the time across a clock source or 12/24 h change. */
            rtc_time_t t;
            rtc_get(&t);
            rtc_restart(cksel, &t);
        }
    } else {
        static const rtc_time_t epoch = { .year = 0, .month = 1, .day = 1, .wday = 6 /* 2000-01-01 was a Saturday */ };
        rtc_restart(cksel, &epoch);
    }

    /* Minute period interrupt; alarm off; clear stale flags (write 0). */
    RTC_CR0 = (RTC_CR0 & ~(CR0_PRDS_MASK | CR0_PRDSEL | CR0_HZ1OE)) | CR0_PRDS_1MIN;
    RTC_CR1 = (RTC_CR1 & ~(CR1_ALMEN | CR1_PRDF | CR1_ALMF | CR1_WAIT)) | CR1_IE;
    return xtl_ok;
}

bool rtc_running_before(void) { return s_running_before; }

void rtc_get(rtc_time_t *t)
{
    lock_regs();
    read_regs(t);
    unlock_regs();
}

void rtc_set(const rtc_time_t *t)
{
    if (RTC_CR0 & CR0_AMPM) {
        lock_regs();
        write_regs(t);
        unlock_regs();
    } else {
        uint32_t cksel = (RTC_CR1 & CR1_CKSEL_MASK) >> CR1_CKSEL_SHIFT;
        uint32_t ie = RTC_CR1 & CR1_IE;
        rtc_restart(cksel, t);
        RTC_CR1 = RTC_CR1 | ie | CR1_PRDF | CR1_ALMF;
    }
}

/* Any RTC interrupt is the minute tick. Clear both flags (write 0). */
void RTC_IRQHandler(void)
{
    RTC_CR1 = RTC_CR1 & ~(CR1_PRDF | CR1_ALMF);
    power_post_events(EV_MINUTE);
}
