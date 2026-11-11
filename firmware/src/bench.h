/* SWD-driven bench hooks (LINK_MBOX builds only; notes-link.md). */
#pragma once
#include <stdint.h>

/* Called from the main loop on every wake: runs a command the host wrote
 * to bench_cmd, then clears it and sets bench_result. */
void bench_poll(void);
