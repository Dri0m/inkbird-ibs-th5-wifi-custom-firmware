/* UART0 transport to the CBU (docs/protocol.md 2): PB06 TX -> CBU RX1,
 * PB07 RX <- CBU TX1, 8N1, 115200 then 500000. CBU power on PB05 (low = on).
 * Not used in LINK_MBOX bench builds (mbox.c replaces it).
 *
 * Registers (UART0 base 0x40000000; vendor SVD HC32L170FAUA.sfr bit table,
 * DDL uart.c):
 *   SCON  RCIE 0, TCIE 1, B8CONT[3:2], REN 4, ADRDET 5, SM[7:6], TXEIE 8,
 *         OVER 9 (mode 1/3: 0 = /16, 1 = /8), PEIE 13, STOPBIT[15:14], ...
 *   ISR   RC 0, TC 1, FE 2, TXE 3, PE 4
 *   ICR   RCCF 0, TCCF 1, FECF 2, PECF 4: write 0 clears (DDL Uart_ClrStatus
 *         does ICR &= ~bit; Uart_ClrIsr writes 0); we write ~mask so other
 *         flags are left alone
 *   SCNT  baud counter: baud = PCLK / (16 or 8) / SCNT (DDL Uart_CalScnt)
 * Stock: mode 1 (SCON = 0x40 | REN | RCIE), PB06/PB07 AF2, IRQ 6 (O 0x6724).
 *
 * Dividers at PCLK = HCLK = 24 MHz (protocol.md 2.1):
 *   115200: /16, SCNT 13 -> 115 385 Bd (+0.16 %)
 *   500000: /8,  SCNT 6  -> 500 000 Bd (exact)
 * The RCH 24 MHz factory trim is +-1 % or so, well inside the ~3 % an 8N1
 * receiver tolerates. At 4 MHz neither rate is reachable, so the link runs
 * only at 24 MHz (link.c switches HCLK for the whole session). */
#ifndef LINK_MBOX
#include "link_port.h"

#include "board.h"
#include "link_proto.h"
#include "regs.h"
#include "wdt.h"

#define SCON_RCIE     (1u << 0)
#define SCON_REN      (1u << 4)
#define SCON_SM_MODE1 (1u << 6)
#define SCON_OVER8    (1u << 9)
#define ISR_RC        (1u << 0)
#define ISR_TC        (1u << 1)
#define ISR_FE        (1u << 2)
#define ISR_TXE       (1u << 3)
#define ISR_PE        (1u << 4)

#define AF_UART0      2u              /* PB06 TXD / PB07 RXD (O 0x6724) */
#define RING          2048u           /* > the largest frame (1032 B) */

static volatile uint8_t s_ring[RING];
static volatile uint32_t s_head; /* ISR */
static volatile uint32_t s_tail; /* thread */
static bool s_tx_busy;
static uint32_t s_tx_ms;    /* clock_ms() when the last byte entered the shifter */
static uint32_t s_byte_us;  /* one 10-bit character at the current rate, rounded up */

/* Wait for an ISR flag for at most `us`, timed with SysTick itself: the
 * 24-bit down-counter is sampled every loop and the deltas summed, so a
 * wrap (0.7 s at 24 MHz) cannot hide time. */
static bool wait_isr(uint32_t mask, uint32_t us)
{
    uint32_t budget = us * (SystemCoreClock / 1000000u), used = 0, last = SYST_CVR;
    while (!(UART0_ISR & mask)) {
        uint32_t cvr = SYST_CVR;
        used += (last - cvr) & 0x00FFFFFFu;
        last = cvr;
        if (used >= budget)
            return false;
    }
    return true;
}

static void set_baud(uint32_t baud)
{
    uint32_t div = baud > LINK_BAUD_SLOW ? 8u : 16u;
    uint32_t scnt = (SystemCoreClock + div * baud / 2u) / (div * baud);
    uint32_t scon = UART0_SCON & ~SCON_OVER8;
    if (div == 8u)
        scon |= SCON_OVER8;
    UART0_SCON = scon;
    UART0_SCNT = scnt;
    s_byte_us = (10000000u + baud - 1u) / baud;
}

void lport_init(void) {}

void lport_power(bool on)
{
    if (on) {
        SYSCTRL_PERI_CLKEN0 |= CLK_UART0;
        UART0_SCON = 0;
        s_head = s_tail = 0;
        s_tx_busy = false;
        set_baud(LINK_BAUD_SLOW);
        UART0_SCON = (UART0_SCON & SCON_OVER8) | SCON_SM_MODE1 | SCON_REN | SCON_RCIE;
        UART0_ICR = 0u; /* drop stale flags */
        /* TX idles high as soon as the pin belongs to the UART; then the
         * CBU gets power (protocol.md 2.2 step 1). */
        pin_af(PIN_CBU_TX, AF_UART0, true);
        pin_af(PIN_CBU_RX, AF_UART0, false);
        irq_clear_pending(UART0_IRQn);
        irq_enable(UART0_IRQn);
        pin_clr(PIN_CBU_PWR);
    } else {
        pin_set(PIN_CBU_PWR);
        irq_disable(UART0_IRQn);
        UART0_SCON = 0;
        /* Park both pins low so nothing back-powers the module (stock
         * 0x6D20; board_init does the same at boot). */
        pin_output(PIN_CBU_TX, false);
        pin_output(PIN_CBU_RX, false);
        SYSCTRL_PERI_CLKEN0 &= ~CLK_UART0;
        s_tx_busy = false;
    }
}

int lport_getc(void)
{
    uint32_t t = s_tail;
    if (t == s_head)
        return -1;
    int c = s_ring[t];
    s_tail = (t + 1u) & (RING - 1u);
    return c;
}

/* Each wait is bounded by a few character times (a stuck TXE cannot hang
 * the session), and the WDT is fed every 64 bytes: 1032 B at 115200 take
 * 90 ms, far below the 1.64 s WDT period anyway. */
void lport_write(const uint8_t *p, uint32_t n)
{
    if (!n)
        return;
    uint32_t wait_us = 3u * s_byte_us + 100u;
    s_tx_busy = true;
    for (uint32_t i = 0; i < n; i++) {
        if ((i & 63u) == 0u)
            wdt_feed();
        (void)wait_isr(ISR_TXE, wait_us);
        UART0_SBUF = p[i];
    }
    /* Once TXE is set again the last byte has moved into the shifter, so
     * every earlier byte is complete: clear TC, and the next TC marks the
     * end of the last stop bit (protocol.md 2.3 step 2, 10 open question).
     * IRQs are masked from the TXE wait to the clear so that an ISR cannot
     * delay the clear past the end of the last byte, which would lose its
     * TC. The window is at most one character (87 us at 115200). */
    uint32_t pm;
    __asm volatile("mrs %0, primask\n cpsid i" : "=r"(pm)::"memory");
    (void)wait_isr(ISR_TXE, wait_us);
    UART0_ICR = ~ISR_TC;
    __asm volatile("msr primask, %0" ::"r"(pm) : "memory");
    s_tx_ms = clock_ms();
}

/* TC, or as a backstop 2-3 ms after the last byte entered the shifter
 * (a character is <= 87 us): a TC that is somehow lost delays the baud
 * switch or the power-off by a few ms instead of stalling the session. */
bool lport_tx_idle(void)
{
    if (s_tx_busy && ((UART0_ISR & ISR_TC) || clock_ms() - s_tx_ms >= 3u))
        s_tx_busy = false;
    return !s_tx_busy;
}

void lport_set_baud(uint32_t baud) { set_baud(baud); }

/* RX only (TX is polled from thread context). Clear, then read, as the DDL
 * examples do; a framing-error byte is passed on like any other, so that a
 * rate mismatch shows up as discarded garbage to the parser (R1b). */
void UART0_IRQHandler(void)
{
    uint32_t st = UART0_ISR;
    UART0_ICR = ~(st & (ISR_RC | ISR_FE | ISR_PE));
    if (st & ISR_RC) {
        uint8_t c = (uint8_t)UART0_SBUF;
        uint32_t h = s_head, next = (h + 1u) & (RING - 1u);
        if (next != s_tail) { /* full: dropped, as the parser would see it */
            s_ring[h] = c;
            s_head = next;
        }
    }
}
#endif
