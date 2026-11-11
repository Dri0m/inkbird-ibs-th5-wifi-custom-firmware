/* Vector table and reset handler (HC32L170: 16 core + 32 device vectors). */
#include <stdint.h>

#include "board.h"
#include "fwu.h"
#include "link.h"

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
int main(void);

void Reset_Handler(void);
void Default_Handler(void);

#define WEAK __attribute__((weak, alias("Default_Handler")))
void NMI_Handler(void) WEAK;
void HardFault_Handler(void) WEAK;
void SVC_Handler(void) WEAK;
void PendSV_Handler(void) WEAK;
void SysTick_Handler(void) WEAK;
void PORTA_IRQHandler(void) WEAK;
void PORTB_IRQHandler(void) WEAK;
void PORTC_E_IRQHandler(void) WEAK;
void PORTD_F_IRQHandler(void) WEAK;
void UART0_IRQHandler(void) WEAK;
void LPTIM_IRQHandler(void) WEAK;
void WDT_IRQHandler(void) WEAK;
void RTC_IRQHandler(void) WEAK;
void LVD_IRQHandler(void) WEAK;

__attribute__((section(".vectors"), used))
void (*const vectors[48])(void) = {
    (void (*)(void))&_estack,
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    0, 0, 0, 0, 0, 0, 0,
    SVC_Handler,
    0, 0,
    PendSV_Handler,
    SysTick_Handler,
    [16 + PORTA_IRQn] = PORTA_IRQHandler,
    [16 + PORTB_IRQn] = PORTB_IRQHandler,
    [16 + 2] = PORTC_E_IRQHandler,
    [16 + PORTD_F_IRQn] = PORTD_F_IRQHandler,
    [16 + UART0_IRQn] = UART0_IRQHandler,
    [16 + LPTIM_IRQn] = LPTIM_IRQHandler,
    [16 + WDT_IRQn] = WDT_IRQHandler,
    [16 + RTC_IRQn] = RTC_IRQHandler,
    [16 + LVD_IRQn] = LVD_IRQHandler,
};

/* The bootloader (boot/boot.c) checks this before starting the app. */
__attribute__((section(".appinfo"), used))
const fwu_info_t app_info = { FWU_APP_MAGIC, LINK_FW_VERSION };

void Reset_Handler(void)
{
    /* The bootloader sets VTOR; a debugger start from 0x1000 does not. */
    SCB_VTOR = FWU_APP_BASE;
    /* Power hold and CBU off before anything else (design 4.1). */
    board_early_init();

    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) *dst++ = *src++;
    for (uint32_t *dst = &_sbss; dst < &_ebss;) *dst++ = 0;

    main();
    for (;;) {
    }
}

/* Unexpected interrupt or fault. Make the board safe at once, then reset:
 * CBU off (PB05 high), its UART pins back to GPIO and parked low so nothing
 * back-powers it, then SYSRESETREQ (the reset cause reads RST_SYSREQ at the
 * next boot). Spinning until the WDT instead would leave the CBU powered
 * and HCLK at 24 MHz for up to a WDT period. Register writes only: no
 * stack beyond the exception frame, no calls. */
void Default_Handler(void)
{
    GPIO_BSET(PORT_B) = 1u << 5;                 /* PIN_CBU_PWR high: CBU off */
    GPIO_BCLR(PORT_B) = (1u << 6) | (1u << 7);   /* PB06/PB07 output latch low */
    GPIO_SEL(PORT_B, 6) = 0u;                    /* AF2 UART0 -> GPIO */
    GPIO_SEL(PORT_B, 7) = 0u;
    GPIO_DIR(PORT_B) &= ~((1u << 6) | (1u << 7)); /* outputs (1 = input) */
    __asm volatile("dsb" ::: "memory");
    SCB_AIRCR = 0x05FA0004u;                     /* VECTKEY | SYSRESETREQ */
    for (;;) {
    }
}
