#pragma once
// C++ access to the shared protocol header firmware/src/link_proto.h, the single
// source of truth for constants, payload offsets and the CRC (docs/protocol.md),
// and to firmware/src/fwu.h (the MCU firmware package header, design.md 4.7).
//
// The headers are not copied into this directory. The ESPHome build gets them from
// __init__.py, which copies them into <build>/th5_link_include/ at code-generation
// time and adds that directory (and only that one) to the include path. The host
// test compiles with -I firmware/src.
//
// Both headers are C11. Their only C-only construct is _Static_assert, which g++
// does not accept in C++ mode; clang++ accepts it as an extension.
#if defined(__cplusplus) && !defined(__clang__) && !defined(_Static_assert)
#define _Static_assert static_assert
#define TH5_UNDEF_STATIC_ASSERT
#endif

#include "link_proto.h"
#include "fwu.h"

#ifdef TH5_UNDEF_STATIC_ASSERT
#undef _Static_assert
#undef TH5_UNDEF_STATIC_ASSERT
#endif
