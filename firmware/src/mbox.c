/* SWD mailbox transport (docs/protocol.md 8), bench builds only
 * (make LINK_MBOX=1). The host (tools/th5link/transport.py) plays the CBU
 * through `link_mbox` while the core runs. The real CBU stays unpowered:
 * PB05 is never driven low here (it runs the stock Tuya firmware). */
#ifdef LINK_MBOX
#include "link_port.h"

#include "link_proto.h"
#include "wdt.h"

link_mbox_t link_mbox __attribute__((aligned(4)));

void lport_init(void) { link_mbox_init(&link_mbox); }

void lport_power(bool on) { link_mbox_power(&link_mbox, on); }

int lport_getc(void)
{
    if (!link_mbox_booted(&link_mbox))
        return -1;
    return link_mbox_getc(&link_mbox);
}

void lport_write(const uint8_t *p, uint32_t n)
{
    /* Bytes are dropped while the "CBU" is off or not booted, like a UART
     * into an unpowered module (8.3). A full ring means the host stopped
     * reading: wait a little (the ring holds more than any frame), then
     * drop. */
    for (uint32_t i = 0; i < n; i++) {
        uint32_t spins = 0;
        while (link_mbox_booted(&link_mbox) && !link_mbox_putc(&link_mbox, p[i])) {
            if (++spins > 200000u)
                return;
            if ((spins & 0x3FFu) == 0)
                wdt_feed();
        }
    }
}

bool lport_tx_idle(void) { return true; }

void lport_set_baud(uint32_t baud)
{
    if (baud == LINK_BAUD_FAST)
        link_mbox.mcu_ctl |= LINK_MBOX_MCU_FAST;
    else
        link_mbox.mcu_ctl &= ~LINK_MBOX_MCU_FAST;
}
#endif
