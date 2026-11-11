/* Bootloader: the first 4 KB of flash, the top 2 KB of RAM (src/fwu.h,
 * design 4.7). It has no .data and does not clear .bss (nothing in it
 * relies on zeroed RAM). The app keeps its .noinit below 0x20003800. */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 4K
    RAM   (rwx) : ORIGIN = 0x20003800, LENGTH = 2K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

ENTRY(Reset_Handler)

SECTIONS
{
    .text :
    {
        KEEP(*(.vectors))
        . = 0xC0;
        KEEP(*(.info))
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > FLASH

    .data : { *(.data*) } > RAM AT > FLASH
    .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM

    ASSERT(SIZEOF(.data) == 0, "the bootloader must not have initialised data")
    ASSERT(SIZEOF(.bss) <= 1K, "bootloader RAM: leave 1 KB for the stack")

    /DISCARD/ : { *(.ARM.attributes) *(.comment) *(.ARM.exidx*) }
}
