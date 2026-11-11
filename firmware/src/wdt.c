/* Watchdog. Register facts and sources: firmware/notes-rtc-power.md. */
#include "wdt.h"

#include "board.h"

/* WDT_CON fields (vendor header stc_wdt_con_field_t; DDL wdt.c/wdt.h) */
#define WDT_CON_WOV_MASK  0x0Fu       /* overflow period, 1.6 ms << WOV on the 10 kHz WDT clock */
#define WDT_CON_R         (1u << 4)   /* read: WDT running (DDL Wdt_ReadwdtStatus) */
#define WDT_CON_WINT_EN   (1u << 5)   /* 0 = reset on overflow, 1 = interrupt */
#define WDT_WOV_WANTED    0x0Fu       /* about 52.4 s (DDL WdtT52s4) */

/* Feed/start key: write 0x1E then 0xE1 to WDT_RST (DDL Wdt_Start/Wdt_Feed,
 * stock 0x6848). The same sequence starts a stopped WDT. */
static void wdt_key(void)
{
    WDT_RST = 0x1Eu;
    WDT_RST = 0xE1u;
}

void wdt_start(void)
{
    /* Bus clock first: with the gate off, writes to WDT_RST/WDT_CON are
     * dropped while a WDT started by the previous firmware keeps counting
     * (evidence/m0-display-tests.md). */
    SYSCTRL_PERI_CLKEN0 |= CLK_WDT;

    /* If the previous firmware left it running, feed now so the CON write
     * below cannot race an overflow. */
    if (WDT_CON & WDT_CON_R)
        wdt_key();

    /* Reset mode, longest period. Whether WOV can still be changed once the
     * WDT runs is not documented: read back WDT_CON and let power_init pick
     * the feed tick from whatever WOV is actually in effect. */
    WDT_CON = (WDT_CON & ~(WDT_CON_WOV_MASK | WDT_CON_WINT_EN)) | WDT_WOV_WANTED;

    wdt_key(); /* start, or restart the count with the new period */
}

void wdt_feed(void)
{
    wdt_key();
}
