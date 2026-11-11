# HC32L170 firmware for the IBS-TH5-WIFI. Needs the Arm GNU toolchain
# (arm-none-eabi-gcc) on PATH, or TOOLCHAIN=/path/to/its/bin/ (trailing slash).
TOOLCHAIN ?=
CC      := $(TOOLCHAIN)arm-none-eabi-gcc
OBJCOPY := $(TOOLCHAIN)arm-none-eabi-objcopy
SIZE    := $(TOOLCHAIN)arm-none-eabi-size
NM      := $(TOOLCHAIN)arm-none-eabi-nm

# make DEEPSLEEP=0 / DEEPSLEEP_TRIAL=N / LINK_MBOX=1 build into their own
# directories, so objects never mix (decided here, before OBJS uses it).
BUILD := build/$(if $(DEEPSLEEP_TRIAL),trial$(DEEPSLEEP_TRIAL),$(if $(filter 0,$(DEEPSLEEP)),nodeep,deep))$(if $(filter 1,$(LINK_MBOX)),-mbox,)
BOOT  := build/boot
SRCS  := $(wildcard src/*.c)
OBJS  := $(SRCS:src/%.c=$(BUILD)/%.o)

CFLAGS := -mcpu=cortex-m0plus -mthumb -std=c11 -Os -g3 \
          -Wall -Wextra -Werror -ffunction-sections -fdata-sections \
          -ffreestanding -fno-common -MMD -MP
LDFLAGS := -mcpu=cortex-m0plus -mthumb -T hc32l170.ld -nostartfiles \
           --specs=nano.specs -Wl,--gc-sections -Wl,-Map=$(BUILD)/th5.map

# make DEEPSLEEP=0: plain sleep only, so SWD can always attach (bench builds)
ifeq ($(DEEPSLEEP),0)
CFLAGS += -DPOWER_NO_DEEPSLEEP
endif
# make DEEPSLEEP_TRIAL=N: deep sleep for the first N minutes only (bench)
ifneq ($(DEEPSLEEP_TRIAL),)
CFLAGS += -DDEEPSLEEP_TRIAL_MIN=$(DEEPSLEEP_TRIAL)u
endif

# make LINK_MBOX=1: the link runs over the SWD mailbox instead of UART0
# (bench; protocol.md 8); the real CBU is never powered. Use with DEEPSLEEP=0.
ifeq ($(LINK_MBOX),1)
CFLAGS += -DLINK_MBOX
endif

# The app (linked at 0x1000), its update package and the SWD image with the
# bootloader (design 4.7).
all: $(BUILD)/th5.bin $(BUILD)/th5.fwu $(BUILD)/th5-full.bin

$(BUILD)/%.o: src/%.c | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

# flash.o must be self-contained: flash erase/program only works while the
# issuing code lies below 32 KB, and hc32l170.ld places (and asserts) only
# flash.o there. Any call out of flash.o (wdt_feed, memcpy, libgcc
# division, ...) could run from above 32 KB during an open operation, so
# the only undefined symbol allowed is the SystemCoreClock variable.
$(BUILD)/th5.elf: $(OBJS) hc32l170.ld
	@u="$$($(NM) -u $(BUILD)/flash.o | awk '{print $$2}' | grep -vx SystemCoreClock)"; \
	  if [ -n "$$u" ]; then echo "flash.o calls out of itself: $$u"; exit 1; fi
	$(CC) $(LDFLAGS) $(OBJS) -o $@
	$(SIZE) $@

$(BUILD)/th5.bin: $(BUILD)/th5.elf
	$(OBJCOPY) -O binary $< $@

$(BUILD) $(BOOT):
	mkdir -p $@

# Bootloader (boot/boot.c, 0x0000..0x0FFF): the same for every variant.
$(BOOT)/boot.o: boot/boot.c | $(BOOT)
	$(CC) $(CFLAGS) -I src -c $< -o $@

$(BOOT)/boot.elf: $(BOOT)/boot.o boot/boot.ld
	$(CC) -mcpu=cortex-m0plus -mthumb -T boot/boot.ld -nostartfiles -nostdlib \
	  -Wl,--gc-sections -Wl,-Map=$(BOOT)/boot.map $< -o $@
	$(SIZE) $@

$(BOOT)/boot.bin: $(BOOT)/boot.elf
	$(OBJCOPY) -O binary $< $@

$(BUILD)/th5.fwu $(BUILD)/th5-full.bin &: $(BUILD)/th5.bin $(BOOT)/boot.bin tools/mkfwu.py
	python3 tools/mkfwu.py $(BOOT)/boot.bin $(BUILD)/th5.bin $(BUILD)

# Safety checks (design 4.1): nothing may repurpose SWD or touch protection.
# Flash (notes-flash.md): the FLASH controller (registers by name, or raw
# 0x400200xx addresses) is reachable only from src/flash.c, whose runtime
# range check limits erase/program to the frame slots and settings sectors.
# On top of that, statically:
#  - no whole-array erase: its timings (TMERASE/TMRCV) are never written and
#    no OP_ constant has the value 3; CR is written in exactly one place
#    (cr_set_op, which refuses op > 2);
#  - CR.WAIT is only ever 0 or 1 (one assignment of s_wait, from a bool);
#  - SLOCK0 (code half) is only ever written with 0 (locked);
#  - nothing writes the extra lock words at 0x40020040/44 that stock's DDL
#    writes (not in the L170 register map);
#  - the factory info area 0x00100xxx (trims; the place a vendor option or
#    security word would live) is only accessed through const pointers.
check:
	! grep -rn -i "useio" src/
	! grep -rlE "FLASH_(TNVS|TPGS|TPROG|TSERASE|TMERASE|TPRCV|TSRCV|TMRCV|CR|IFR|ICLR|BYPASS|SLOCK)|SLOCK|0x400200[0-9A-Fa-f]{2}" src/ \
	  | grep -v -e '^src/regs.h$$' -e '^src/flash.c$$'
	! grep -nE "FLASH_(TMERASE|TMRCV)|0x4002004[0-9A-Fa-f]" src/*.c
	! grep -nE "#define +OP_[A-Z_]+ +(0x)?3u?" src/flash.c
	grep -q "if (op > OP_SECTOR_ER)" src/flash.c
	test "$$(grep -c 'prot_write(A_CR' src/flash.c)" -eq 1
	! grep -n "A_SLOCK0," src/flash.c | grep -vE "A_SLOCK0, *0u?,"
	test "$$(grep -c 's_wait = ' src/flash.c)" -eq 1 && grep -q 's_wait = one ? 1u : 0u;' src/flash.c
	! grep -rnE "0x0010[0-9A-Fa-f]{4}" src/ | grep -v "volatile const"
	# Bootloader: the same rules for its own flash driver.
	! grep -n -i "useio" boot/boot.c
	! grep -nE "FLASH_(TMERASE|TMRCV)|0x4002004[0-9A-Fa-f]" boot/boot.c
	! grep -nE "#define +OP_[A-Z_]+ +(0x)?3u?" boot/boot.c
	grep -q "if (op > OP_SECTOR_ER)" boot/boot.c
	test "$$(grep -c 'prot_write(&FLASH_CR' boot/boot.c)" -eq 1

# Host unit test of src/flash.c against a modelled FLASH controller.
HOSTCC ?= cc
test-flash:
	mkdir -p build/host
	$(HOSTCC) -std=c11 -Wall -Wextra -Werror -Wno-int-to-pointer-cast -DFLASH_HOST_TEST \
	  -I test -I src test/test_flash.c src/flash.c -o build/host/test_flash
	./build/host/test_flash

# Host test of the bootloader: install, refusals, a power cut at every step.
test-boot:
	mkdir -p build/host
	$(HOSTCC) -std=c11 -Wall -Wextra -Werror -DBOOT_HOST -I src test/test_boot.c boot/boot.c \
	  -o build/host/test_boot
	./build/host/test_boot

# Host test of src/policy.c (main.c's pure decisions) and render_hour.
test-policy:
	mkdir -p build/host
	$(HOSTCC) -std=c11 -Wall -Wextra -Werror -I src test/test_policy.c src/policy.c \
	  src/render.c src/fonts_gen.c -o build/host/test_policy
	./build/host/test_policy

# Host test of the link engine (src/link.c, settings.c, frame.c compiled
# with -DLINK_HOST into a shared library) driven by the Python fake CBU.
test-link: ../ha/out/frame-C.slot
	cd .. && uv run --with pytest --with pillow pytest -q tools/th5link/tests

# The sample forecast frame the link tests push (rendered as HA would).
../ha/out/frame-C.slot:
	cd .. && uv run ha/tools/th5_render.py

# The CBU side (esphome/components/th5_link) against the same Python model.
test-esphome: ../ha/out/frame-C.slot
	cd .. && uv run --with pytest pytest -q esphome/test

test-history:
	mkdir -p build/host
	$(HOSTCC) -std=c11 -Wall -Wextra -Werror -I src test/test_history.c src/history.c -o build/host/test_history
	./build/host/test_history

# The renderer on the host: band = top of the fallback screen, two-slot cache.
test-render:
	@mkdir -p build/host build/render
	$(HOSTCC) -std=c11 -Wall -Wextra -Werror -I src test/render_host.c src/render.c \
	  src/fonts_gen.c -o build/host/render_host
	./build/host/render_host build/render && echo "render_host: all passed"

test: test-flash test-boot test-policy test-render test-history test-link test-esphome

clean:
	rm -rf build

.PHONY: all check clean test test-flash test-boot test-policy test-render test-history test-link test-esphome
-include $(OBJS:.o=.d) $(BOOT)/boot.d
