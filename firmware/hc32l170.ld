/* HC32L170FAUA: 128 KB flash, 16 KB SRAM. The app lives at 0x1000..0xFFFF,
 * after the 4 KB bootloader; the rest is frame slots and settings
 * (design/design.md 4.4, 4.7; src/fwu.h). */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x00001000, LENGTH = 60K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);
_min_stack = 2K;

ENTRY(Reset_Handler)

SECTIONS
{
    .text :
    {
        KEEP(*(.vectors))
        . = 0xC0;
        KEEP(*(.appinfo)) /* fwu_info_t at 0x10C0: the bootloader checks it */
        /* Flash erase/program only works when the code issuing it lies in
         * 0..32K (user manual Rev1.0 ch. 12, "FLASH operation source
         * defence"), so the driver goes first and is checked below. */
        *flash.o(.text*)
        __flash_code_end = .;
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > FLASH

    .ARM.exidx : { *(.ARM.exidx*) } > FLASH

    _sidata = LOADADDR(.data);
    .data :
    {
        . = ALIGN(4);
        _sdata = .;
        *(.data*)
        . = ALIGN(4);
        _edata = .;
    } > RAM AT > FLASH

    .bss (NOLOAD) :
    {
        . = ALIGN(4);
        _sbss = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } > RAM

    /* Not zeroed by startup.c: survives resets other than power-on
     * (main.c nv: boot-session counter, guarded by a magic). */
    .noinit (NOLOAD) :
    {
        . = ALIGN(4);
        *(.noinit*)
        . = ALIGN(4);
        _enoinit = .;
    } > RAM

    ASSERT(__flash_code_end <= 0x8000, "flash.c must be linked below 32 KB")
    /* The bootloader runs its stack and buffer in the top 2 KB (boot/boot.ld). */
    ASSERT(_enoinit <= 0x20003800, ".noinit must stay below the bootloader's RAM")
    ASSERT(_enoinit + _min_stack <= _estack, "RAM overflow: less than 2 KB left for the stack")

    /DISCARD/ : { *(.ARM.attributes) *(.comment) }
}
