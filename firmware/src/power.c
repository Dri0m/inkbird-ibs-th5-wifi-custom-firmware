/* Wake events, deep sleep, WDT feed tick, power off.
 * Register facts and sources: firmware/notes-rtc-power.md. */
#include "power.h"

#include "board.h"
#include "wdt.h"

/* SYSCTRL0 (stc_sysctrl_sysctrl0_field_t) and its unlock key (DDL sysctrl.c
 * _SysctrlUnlock: 0x5A5A then 0xA5A5 to SYSCTRL2 before each SYSCTRL0/1 write) */
#define SC0_RCL_EN        (1u << 2)
#define SC_KEY1           0x5A5Au
#define SC_KEY2           0xA5A5u

/* RCL_CR (stc_sysctrl_rcl_cr_field_t): TRIM[9:0], STARTUP[11:10], STABLE bit 12 */
#define RCL_CR_TRIM_MASK  0x3FFu
#define RCL_CR_STARTUP_256 (3u << 10)   /* DDL SysctrlRclStableCycle256 */
#define RCL_CR_STABLE     (1u << 12)
/* Factory trim for RCL = 32.768 kHz (DDL sysctrl.c RCL_CR_TRIM_32768_VAL; stock SystemInit 0x6444) */
#define RCL_TRIM_32768    (*(volatile const uint16_t *)0x00100C22u)

/* GPIO CTRL0 (vendor header stc_gpio_ctrl0_field_t; not in regs.h).
 * IESEL = 1: port interrupts work in deep sleep (DDL GpioSfIrqDpslpMode). */
#define GPIO_CTRL0        REG32(0x40020F00u)
#define GPIO_CTRL0_IESEL  (1u << 0)

/* LPTIMER0_CR (stc_lptimer_cr_field_t; DDL lptim.c/lptim.h) */
#define LPT_TR            (1u << 0)
#define LPT_MD_RELOAD     (1u << 1)     /* mode 2: 16-bit auto-reload */
#define LPT_TCK_RCL       (3u << 4)     /* DDL LptimRcl; stock uses it (0x3B70) */
#define LPT_WT_FLAG       (1u << 7)     /* write-sync done; DDL waits for it before writing ARR */
#define LPT_IE            (1u << 10)
#define LPT_PRS_256       (7u << 11)    /* DDL LptimPrsDiv256 */
#define LPT_HZ            128u          /* 32768 / 256 */

/* WDT_CON WOV (stc_wdt_con_field_t): period = 1.6 ms << WOV (DDL wdt.h table) */
#define WDT_CON_WOV_MASK  0x0Fu

#define SCR_SLEEPDEEP     (1u << 2)     /* ARMv6-M SCB->SCR */

#define TICK_MAX_MS       13107u        /* design: feed wake every <= 20 s */
#define TICK_MIN_MS       20u

#ifndef POWER_BUSY_TIMEOUT_MS
#define POWER_BUSY_TIMEOUT_MS 5000u /* 2 feed ticks: 13-26 s at the 13.1 s tick */
#endif

#define PA_BUSY_BIT       (1u << 2)
#define PA_SW_BIT         (1u << 9)
#define PA_TOP_BIT        (1u << 10)
#define PB_CF_BIT         (1u << 4)

static volatile uint32_t s_events;
static volatile uint32_t s_ticks;
static uint32_t s_tick_ms = 1000u;
static bool s_deep_ok = true;

void power_allow_deep(bool on) { s_deep_ok = on; }

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

static inline void wfi(void)
{
    __asm volatile("dsb\n wfi\n isb" ::: "memory");
}

void power_sysctrl0_set(uint32_t bits, bool on)
{
    uint32_t pm = irq_save();
    uint32_t v = SYSCTRL0;
    v = on ? (v | bits) : (v & ~bits);
    SYSCTRL2 = SC_KEY1;
    SYSCTRL2 = SC_KEY2;
    SYSCTRL0 = v;
    irq_restore(pm);
}

bool power_rcl_enable(void)
{
    SYSCTRL_RCL_CR = (SYSCTRL_RCL_CR & ~(RCL_CR_TRIM_MASK | (3u << 10)))
                   | (RCL_TRIM_32768 & RCL_CR_TRIM_MASK) | RCL_CR_STARTUP_256;
    if (!(SYSCTRL0 & SC0_RCL_EN))
        power_sysctrl0_set(SC0_RCL_EN, true);
    for (uint32_t i = 0; i < 50u; i++) { /* 256 cycles = 8 ms */
        if (SYSCTRL_RCL_CR & RCL_CR_STABLE)
            return true;
        delay_ms(1);
    }
    return (SYSCTRL_RCL_CR & RCL_CR_STABLE) != 0u;
}

void power_post_events(uint32_t ev)
{
    uint32_t pm = irq_save();
    s_events |= ev;
    irq_restore(pm);
}

uint32_t power_take_events(void)
{
    uint32_t pm = irq_save();
    uint32_t ev = s_events;
    s_events = 0u;
    irq_restore(pm);
    return ev;
}

static bool lptim_sync(void)
{
    for (uint32_t i = 0; i < 1000u; i++) {
        if (LPTIMER0_CR & LPT_WT_FLAG)
            return true;
        delay_us(10);
    }
    return false;
}

/* Feed tick on LPTIM0/RCL. The period follows the WDT period actually in
 * effect (read back): a quarter of it, capped at TICK_MAX_MS. With WOV 0xF
 * (52.4 s) that is 13.1 s; if a WDT left running by the previous firmware
 * kept WOV 0xA (1.64 s), it is 410 ms. */
static void tick_init(void)
{
    uint32_t wov = WDT_CON & WDT_CON_WOV_MASK;
    uint32_t wdt_ms = (16u << wov) / 10u;
    uint32_t tick = wdt_ms / 4u;
    if (tick > TICK_MAX_MS) tick = TICK_MAX_MS;
    if (tick < TICK_MIN_MS) tick = TICK_MIN_MS;

    uint32_t counts = tick * LPT_HZ / 1000u;
    if (counts < 1u) counts = 1u;
    if (counts > 0xFFFFu) counts = 0xFFFFu;
    s_tick_ms = counts * 1000u / LPT_HZ;

    (void)power_rcl_enable();
    SYSCTRL_PERI_CLKEN0 |= CLK_LPTIM0;

    LPTIMER0_CR = 0u; /* stop */
    (void)lptim_sync();
    LPTIMER0_CR = LPT_MD_RELOAD | LPT_TCK_RCL | LPT_PRS_256;
    (void)lptim_sync();
    LPTIMER0_ARR = 0x10000u - counts; /* counts up from ARR, irq on overflow */
    (void)lptim_sync();
    LPTIMER0_ICLR = 0u;               /* clear TF (write 0) */
    LPTIMER0_CR = LPT_MD_RELOAD | LPT_TCK_RCL | LPT_PRS_256 | LPT_IE | LPT_TR;
}

void power_init(void)
{
    SYSCTRL_PERI_CLKEN0 |= CLK_GPIO;

    /* Buttons are active low. PA09 (switch sense) and PA02 (BUSY) keep the
     * configuration board_init gave them. */
    pin_input(PIN_BTN_TOP, true);
    pin_input(PIN_BTN_CF, true);

    GPIO_RIE(PORT_A) &= ~PA_BUSY_BIT;
    GPIO_FIE(PORT_A) |= PA_TOP_BIT | PA_SW_BIT;
    GPIO_FIE(PORT_B) |= PB_CF_BIT;
    GPIO_ICLR(PORT_A) = 0u; /* write 0 clears; drop anything stale */
    GPIO_ICLR(PORT_B) = 0u;

    tick_init();

    static const uint8_t irqs[] = { PORTA_IRQn, PORTB_IRQn, LPTIM_IRQn, RTC_IRQn };
    for (uint32_t i = 0; i < sizeof irqs; i++) {
        irq_clear_pending(irqs[i]);
        irq_enable(irqs[i]);
    }
}

/* Called with PRIMASK set. WFI still wakes on a pending interrupt; the
 * handler runs once the caller restores PRIMASK. */
static void sleep_masked(void)
{
#ifndef POWER_NO_DEEPSLEEP
    if (s_deep_ok) {
        GPIO_CTRL0 |= GPIO_CTRL0_IESEL;
        SCB_SCR |= SCR_SLEEPDEEP;
    } else {
        SCB_SCR &= ~SCR_SLEEPDEEP;
    }
#else
    SCB_SCR &= ~SCR_SLEEPDEEP;
#endif
    wfi();
    /* HCLK comes back on RCH with the trim it had, so SystemCoreClock and
     * the flash timing are unchanged; only undo the sleep settings. */
    SCB_SCR &= ~SCR_SLEEPDEEP;
#ifndef POWER_NO_DEEPSLEEP
    GPIO_CTRL0 &= ~GPIO_CTRL0_IESEL;
#endif
}

void power_sleep(void)
{
    if (s_ticks == 0u) { delay_ms(100); return; }
    uint32_t pm = irq_save();
    if (s_events == 0u)
        sleep_masked();
    irq_restore(pm);
}

void power_idle(void)
{
    if (s_ticks == 0u) { delay_ms(100); return; }
    uint32_t pm = irq_save();
    if (s_events == 0u) {
        SCB_SCR &= ~SCR_SLEEPDEEP;
        wfi();
    }
    irq_restore(pm);
}

bool power_wait_busy(void)
{
    /* Before the first timer tick, keep the original bounded polling path:
     * a broken timer must not strand the first refresh in WFI. */
    if (s_ticks == 0u) {
        for (uint32_t ms = 0; ms < 4000u; ms++) {
            if (pin_read(PIN_EPD_BUSY)) return true;
            delay_ms(1);
            if (ms % 100u == 0u) wdt_feed();
        }
        return false;
    }
    if (pin_read(PIN_EPD_BUSY))
        return true;

    uint32_t limit = POWER_BUSY_TIMEOUT_MS / (s_tick_ms ? s_tick_ms : 1u) + 2u;
    uint32_t t0 = s_ticks;
    uint32_t last = t0;
    bool ok;

    GPIO_ICLR(PORT_A) = ~PA_BUSY_BIT;
    GPIO_RIE(PORT_A) |= PA_BUSY_BIT;

    for (;;) {
        uint32_t pm = irq_save();
        if (pin_read(PIN_EPD_BUSY)) { ok = true; irq_restore(pm); break; }
        if (s_ticks - t0 >= limit) { ok = false; irq_restore(pm); break; }
        sleep_masked();
        irq_restore(pm);
        if (s_ticks != last) { /* thread context, bounded by `limit` */
            last = s_ticks;
            wdt_feed();
        }
    }

    GPIO_RIE(PORT_A) &= ~PA_BUSY_BIT;
    GPIO_ICLR(PORT_A) = ~PA_BUSY_BIT;
    uint32_t pm = irq_save();
    s_events &= ~(uint32_t)EV_BUSY;
    irq_restore(pm);
    return ok;
}


/* ISRs: only set flags. Status bits clear by writing 0 (DDL Gpio_ClearIrq);
 * write 1 elsewhere to leave other pins' flags alone. */
void PORTA_IRQHandler(void)
{
    uint32_t st = GPIO_STAT(PORT_A);
    GPIO_ICLR(PORT_A) = ~st;
    uint32_t ev = 0u;
    if (st & PA_TOP_BIT) ev |= EV_BTN_TOP;
    if (st & PA_SW_BIT) ev |= EV_SWITCH;
    if (st & PA_BUSY_BIT) ev |= EV_BUSY;
    power_post_events(ev);
}

void PORTB_IRQHandler(void)
{
    uint32_t st = GPIO_STAT(PORT_B);
    GPIO_ICLR(PORT_B) = ~st;
    if (st & PB_CF_BIT)
        power_post_events(EV_BTN_CF);
}

void LPTIM_IRQHandler(void)
{
    LPTIMER0_ICLR = 0u; /* TFC: write 0 clears TF (DDL Lptim_ClrItStatus) */
    s_ticks++;
    power_post_events(EV_TICK);
}
