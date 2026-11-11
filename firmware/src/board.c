#include "board.h"
#include "flash.h"
#include "power.h"
#include "wdt.h"

uint32_t SystemCoreClock = 4000000u;
uint32_t board_reset_cause;

/* Factory trim words in the info area (as stock SystemInit 0x6444 reads them) */
#define TRIM_RCH_4M  (*(volatile const uint16_t *)0x00100C08u)
/* RCH trims for 8, 16, 22.12 and 24 MHz (DDL sysctrl.c RCH_CR_TRIM_*_VAL;
 * stock 0x34F4 steps through 0x100C06, 04, 02, 00) */
#define TRIM_RCH_8M  (*(volatile const uint16_t *)0x00100C06u)
#define TRIM_RCH_16M (*(volatile const uint16_t *)0x00100C04u)
#define TRIM_RCH_22M (*(volatile const uint16_t *)0x00100C02u)
#define TRIM_RCH_24M (*(volatile const uint16_t *)0x00100C00u)
#define RCH_TRIM_MASK 0x7FFu
#define SC0_HCLK_PRS_MASK (7u << 8) /* SYSCTRL0 HCLK_PRS[10:8]: 7 = /128 */
#define TRIM_RCL_32K (*(volatile const uint16_t *)0x00100C22u)

void pin_output(uint32_t port, uint32_t pin, bool level)
{
    GPIO_SEL(port, pin) = 0;
    GPIO_ADS(port) &= ~(1u << pin);
    pin_write(port, pin, level);
    GPIO_DIR(port) &= ~(1u << pin);
}

void pin_input(uint32_t port, uint32_t pin, bool pull_up)
{
    GPIO_SEL(port, pin) = 0;
    GPIO_ADS(port) &= ~(1u << pin);
    GPIO_DIR(port) |= 1u << pin;
    if (pull_up) GPIO_PU(port) |= 1u << pin; else GPIO_PU(port) &= ~(1u << pin);
    GPIO_PD(port) &= ~(1u << pin);
}

void pin_af(uint32_t port, uint32_t pin, uint32_t af, bool output)
{
    GPIO_ADS(port) &= ~(1u << pin);
    if (output) GPIO_DIR(port) &= ~(1u << pin); else GPIO_DIR(port) |= 1u << pin;
    GPIO_SEL(port, pin) = af;
}

void pin_analog(uint32_t port, uint32_t pin)
{
    GPIO_SEL(port, pin) = 0;
    GPIO_DIR(port) |= 1u << pin;
    GPIO_PU(port) &= ~(1u << pin);
    GPIO_PD(port) &= ~(1u << pin);
    GPIO_ADS(port) |= 1u << pin;
}

/* Called from the reset handler before .data/.bss are set up: no globals. */
void board_early_init(void)
{
    SYSCTRL_PERI_CLKEN0 |= CLK_GPIO;
    pin_output(PIN_CBU_PWR, true);   /* CBU off */
    /* Hold power only when the slide switch is on, as stock does: booting
     * from USB with the switch off must not latch the power on. */
    pin_input(PIN_PWR_SW, true);
    /* Let the pull-up settle before sampling: 2 ms timed with SysTick (core
     * clock is RCH ~4 MHz out of reset, so 8000 cycles; board_init sets
     * SysTick up again afterwards). A fixed loop count was ~0.2 ms and
     * depended on the compiler. */
    SYST_RVR = 0x00FFFFFFu;
    SYST_CVR = 0;
    SYST_CSR = 5u; /* core clock, enable, no interrupt */
    for (uint32_t used = 0, last = SYST_CVR; used < 8000u;) {
        uint32_t cvr = SYST_CVR; /* counts down; the deltas survive a reload */
        used += (last - cvr) & 0x00FFFFFFu;
        last = cvr;
    }
    pin_output(PIN_PWR_HOLD, pin_read(PIN_PWR_SW));
}

void board_init(void)
{
    board_reset_cause = RESET_FLAG;
    RESET_FLAG = 0;

    /* Same trims as stock SystemInit: RCH 4 MHz, RCL 32.768 kHz. */
    SYSCTRL_RCL_CR = (SYSCTRL_RCL_CR & ~0x3FFu) | (TRIM_RCL_32K & 0x3FFu);
    SYSCTRL_RCH_CR = (SYSCTRL_RCH_CR & ~0x7FFu) | (TRIM_RCH_4M & 0x7FFu);
    SystemCoreClock = 4000000u;

    /* SysTick free-running on HCLK for delays */
    SYST_RVR = 0x00FFFFFFu;
    SYST_CVR = 0;
    SYST_CSR = 5u; /* CLKSOURCE = core, ENABLE, no interrupt */

    /* Unbonded pads as inputs with pull-up: the datasheet (Rev1.76 §3,
     * QFN32 pinout note) asks for this on every IO the package does not
     * bring out, so no input floats in sleep. Stock did PF04/05/07 only.
     * Bonded (pinout of HC32L170FAUA): PA02, PA04-15, PB00/01, PB03-07,
     * PB11, PC14/15 (XTL, left alone), PF00/01; PF11 is BOOT0. */
    static const struct { uint32_t port; uint16_t mask; } unbonded[] = {
        { PORT_A, 0x000Bu },  /* PA00, PA01, PA03 */
        { PORT_B, 0xF704u },  /* PB02, PB08-10, PB12-15 */
        { PORT_C, 0x3FFFu },  /* PC00-13 */
        { PORT_D, 0xFFFFu },
        { PORT_E, 0xFFFFu },
        { PORT_F, 0x06FCu },  /* PF02-07, PF09, PF10 */
    };
    for (uint32_t i = 0; i < sizeof unbonded / sizeof unbonded[0]; i++) {
        GPIO_ADS(unbonded[i].port) &= ~(uint32_t)unbonded[i].mask;
        GPIO_DIR(unbonded[i].port) |= unbonded[i].mask;
        GPIO_PU(unbonded[i].port) |= unbonded[i].mask;
    }

    pin_output(PIN_LED, true);            /* LED off */
    pin_output(PIN_BUZZER, false);        /* silent */
    pin_output(PIN_CBU_TX, false);        /* parked low so the CBU isn't back-powered */
    pin_output(PIN_CBU_RX, false);
    pin_input(PIN_PWR_SW, true);
    pin_input(PIN_BTN_TOP, true);
    pin_input(PIN_BTN_CF, true);
    pin_input(PIN_CHG_STDBY, true);
    pin_input(PIN_CHG_CHRG, true);
    pin_input(PORT_A, 15, true);          /* unused, stock: input pull-up */
}

static void rch_trim(uint16_t trim)
{
    SYSCTRL_RCH_CR = (SYSCTRL_RCH_CR & ~RCH_TRIM_MASK) | (trim & RCH_TRIM_MASK);
}

uint32_t clock_set_fast(bool fast)
{
    uint32_t want = fast ? 24000000u : 4000000u;
    if (want == SystemCoreClock)
        return SystemCoreClock;
    (void)clock_ms(); /* bank the SysTick cycles counted at the old rate */
    if (fast)
        (void)flash_set_wait(true); /* before HCLK passes 24 MHz (flash.c) */
    uint32_t pm;
    __asm volatile("mrs %0, primask\n cpsid i" : "=r"(pm)::"memory");
    power_sysctrl0_set(SC0_HCLK_PRS_MASK, true); /* HCLK /128 while the trim moves */
    if (fast) {
        rch_trim(TRIM_RCH_8M);
        rch_trim(TRIM_RCH_16M);
        rch_trim(TRIM_RCH_22M);
        rch_trim(TRIM_RCH_24M);
    } else {
        rch_trim(TRIM_RCH_22M);
        rch_trim(TRIM_RCH_16M);
        rch_trim(TRIM_RCH_8M);
        rch_trim(TRIM_RCH_4M);
    }
    power_sysctrl0_set(SC0_HCLK_PRS_MASK, false);
    SystemCoreClock = want;
    __asm volatile("msr primask, %0" ::"r"(pm) : "memory");
    if (!fast)
        (void)flash_set_wait(false);
    return SystemCoreClock;
}

static uint32_t s_cvr_last, s_cyc, s_ms;

uint32_t clock_ms(void)
{
    uint32_t cvr = SYST_CVR;
    s_cyc += (s_cvr_last - cvr) & 0x00FFFFFFu; /* SysTick counts down */
    s_cvr_last = cvr;
    uint32_t per_ms = SystemCoreClock / 1000u;
    s_ms += s_cyc / per_ms;
    s_cyc %= per_ms;
    return s_ms;
}

void delay_us(uint32_t us)
{
    /* SysTick counts down from 0xFFFFFF at HCLK (0.7 s at 24 MHz). Split
     * long waits. */
    while (us) {
        uint32_t chunk = us > 500000u ? 500000u : us;
        uint32_t ticks = chunk * (SystemCoreClock / 1000000u);
        uint32_t start = SYST_CVR;
        while (((start - SYST_CVR) & 0x00FFFFFFu) < ticks) {
        }
        us -= chunk;
    }
}

void delay_ms(uint32_t ms)
{
    while (ms) {
        uint32_t chunk = ms > 100u ? 100u : ms;
        delay_us(chunk * 1000u);
        wdt_feed();
        ms -= chunk;
    }
}
