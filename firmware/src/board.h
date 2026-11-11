/* Board pins, clocks and small helpers for the IBS-TH5-WIFI (HC32L170FAUA, QFN32). */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "regs.h"

/* GPIO ports as offsets from the port-A register blocks (SEL at 0x40020C00,
 * DIR/IN/OUT/... at 0x40020D00). Port B adds 0x40, port F adds 0x1040.
 * C, D and E (DDL gpio.h en_gpio_port_t) are on the die but not bonded out
 * in QFN32; only board_init's pull-ups touch them. */
enum { PORT_A = 0x0000, PORT_B = 0x0040, PORT_C = 0x0080, PORT_D = 0x00C0,
       PORT_E = 0x1000, PORT_F = 0x1040 };

#define GPIO_SEL(port, pin) REG32(0x40020C00u + (port) + 4u * (pin))
#define GPIO_DIR(port)      REG32(0x40020D00u + (port))  /* 1 = input */
#define GPIO_IN(port)       REG32(0x40020D04u + (port))
#define GPIO_OUT(port)      REG32(0x40020D08u + (port))
#define GPIO_ADS(port)      REG32(0x40020D0Cu + (port))  /* 1 = analog */
#define GPIO_BSET(port)     REG32(0x40020D10u + (port))
#define GPIO_BCLR(port)     REG32(0x40020D14u + (port))
#define GPIO_PU(port)       REG32(0x40020D20u + (port))
#define GPIO_PD(port)       REG32(0x40020D24u + (port))
#define GPIO_OD(port)       REG32(0x40020D2Cu + (port))
#define GPIO_HIE(port)      REG32(0x40020D30u + (port))  /* high-level irq enable */
#define GPIO_LIE(port)      REG32(0x40020D34u + (port))  /* low-level irq enable */
#define GPIO_RIE(port)      REG32(0x40020D38u + (port))  /* rising-edge irq enable */
#define GPIO_FIE(port)      REG32(0x40020D3Cu + (port))  /* falling-edge irq enable */
#define GPIO_STAT(port)     REG32(0x40020E00u + (port))  /* irq status */
#define GPIO_ICLR(port)     REG32(0x40020E10u + (port))  /* write 0 to clear */

/* PERI_CLKEN0 gate bits */
enum {
    CLK_UART0 = 1u << 0, CLK_SPI0 = 1u << 6, CLK_LPTIM0 = 1u << 9,
    CLK_WDT = 1u << 15, CLK_ADC = 1u << 16, CLK_RTC = 1u << 20,
    CLK_GPIO = 1u << 28, CLK_FLASH = 1u << 31,
};

/* Pin map (evidence/hc32-re-hardware.md) */
#define PIN_EPD_BUSY   PORT_A, 2   /* input, 1 = idle */
#define PIN_EPD_CS     PORT_A, 4   /* AF1 SPI0_CS, held low */
#define PIN_EPD_SCK    PORT_A, 5   /* AF1 */
#define PIN_EPD_RST    PORT_A, 6
#define PIN_EPD_MOSI   PORT_A, 7   /* AF1 */
#define PIN_EPD_DC     PORT_B, 0
#define PIN_PWR_HOLD   PORT_A, 8   /* high = stay powered */
#define PIN_PWR_SW     PORT_A, 9   /* slide switch sense, low = off */
#define PIN_BTN_TOP    PORT_A, 10  /* active low */
#define PIN_SHT_SCL    PORT_A, 11
#define PIN_SHT_SDA    PORT_A, 12
#define PIN_BAT_ADC    PORT_B, 1   /* AIN9 */
#define PIN_LED        PORT_B, 3   /* active low; no LED fitted on REV1.2 */
#define PIN_BUZZER     PORT_B, 11  /* VT1 -> BZ1; low = silent */
#define PIN_BTN_CF     PORT_B, 4   /* active low */
#define PIN_CBU_PWR    PORT_B, 5   /* active low: low = CBU powered */
#define PIN_CBU_TX     PORT_B, 6   /* AF2 UART0_TXD */
#define PIN_CBU_RX     PORT_B, 7   /* AF2 UART0_RXD */
#define PIN_CHG_STDBY  PORT_F, 0   /* TC4056A STDBY, active low */
#define PIN_CHG_CHRG   PORT_F, 1   /* TC4056A CHRG, active low */

static inline void pin_set(uint32_t port, uint32_t pin) { GPIO_BSET(port) = 1u << pin; }
static inline void pin_clr(uint32_t port, uint32_t pin) { GPIO_BCLR(port) = 1u << pin; }
static inline void pin_write(uint32_t port, uint32_t pin, bool v)
{
    if (v) pin_set(port, pin); else pin_clr(port, pin);
}
static inline bool pin_read(uint32_t port, uint32_t pin) { return (GPIO_IN(port) >> pin) & 1u; }

/* Configure a pin as GPIO. Output pins get their level written before the
 * direction flips, so they never glitch. */
void pin_output(uint32_t port, uint32_t pin, bool level);
void pin_input(uint32_t port, uint32_t pin, bool pull_up);
void pin_af(uint32_t port, uint32_t pin, uint32_t af, bool output);
void pin_analog(uint32_t port, uint32_t pin);

/* RESET_FLAG bits, captured at boot then cleared */
enum {
    RST_POR5V = 1u << 0, RST_POR15V = 1u << 1, RST_LVD = 1u << 2, RST_WDT = 1u << 3,
    RST_PCA = 1u << 4, RST_LOCKUP = 1u << 5, RST_SYSREQ = 1u << 6, RST_RSTB = 1u << 7,
};

extern uint32_t SystemCoreClock;   /* Hz */
extern uint32_t board_reset_cause; /* RESET_FLAG at boot */

void board_early_init(void); /* first thing after reset: power hold, CBU off */
void board_init(void);       /* clocks (4 MHz RCH, trimmed), remaining pins */
void delay_us(uint32_t us);  /* busy wait on SysTick */
void delay_ms(uint32_t ms);  /* busy wait; feeds the WDT every 100 ms */

/* HCLK = PCLK on RCH: 4 MHz (default, everything outside a link session) or
 * 24 MHz (link sessions: UART0 needs it for 115200/500000). Steps the RCH
 * trim through the factory values with HCLK divided by 128 meanwhile, as
 * stock 0x34F4 does, and updates SystemCoreClock. The flash WAIT field stays 0
 * (allowed up to 24 MHz). Callers re-run flash_init() afterwards so the
 * program/erase timings match. Returns the new SystemCoreClock. */
uint32_t clock_set_fast(bool fast);

/* Milliseconds from SysTick (free-running, 24 bit). Only meaningful while
 * it is called at least every 0.7 s (24 MHz) / 4 s (4 MHz) and the core is
 * not deep-sleeping: used for the link session timers. */
uint32_t clock_ms(void);

static inline bool button_top(void) { return !pin_read(PIN_BTN_TOP); }
static inline bool button_cf(void) { return !pin_read(PIN_BTN_CF); }
static inline bool power_switch_on(void) { return pin_read(PIN_PWR_SW); }

static inline void irq_enable(uint32_t n) { NVIC_ISER = 1u << n; }
static inline void irq_disable(uint32_t n) { NVIC_ICER = 1u << n; }
static inline void irq_clear_pending(uint32_t n) { NVIC_ICPR = 1u << n; }
