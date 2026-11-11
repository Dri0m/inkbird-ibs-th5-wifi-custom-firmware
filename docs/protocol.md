# MCU ↔ CBU link protocol (v1)

> Reference material, copied from the project's working notes. Links and section
> references to `design.md`, `evidence/…`, `firmware/notes-*.md` and `power-plan.md`
> point at notes that are not published in this repo.

Status: **binding spec, v1**, 2026-11-01. It refines [design.md](design.md) §3 and §7. Where it decides something the design left open, or departs from it, the item is listed in §9 *Decisions*. **(verify)** marks something that needs the bench (§10).

- **C:** [`firmware/src/link_proto.h`](../firmware/src/link_proto.h) has the constants, the payload offsets, the CRC and the mailbox struct.
- **Python reference:** [`tools/th5link/`](../tools/th5link/). `proto.py` has the frames and payloads. `link.py` is the stop-and-wait engine. `mcumodel.py` is an executable model of the MCU side. `fakecbu.py` is a scripted CBU. `transport.py` has the UART and SWD-mailbox transports.
- **Tests:** `uv run --with pytest pytest tools/th5link/tests`. They cover CRC vectors, resync, every payload, loopback sessions with injected faults, and a C-vs-Python byte cross-check.

If the text and the reference implementation disagree, fix whichever is wrong, then make the tests agree with both.

**Changes forced by the C implementation (2026-11-01).** The MCU side now exists (`firmware/src/link.c`; hardware results in [`firmware/notes-link.md`](../firmware/notes-link.md)). Building and running it on the TH5 changed these parts of the spec. Each place is marked *(changed by the implementation)*:
- §2.3 clock note: the MCU stays at 24 MHz for the whole session, and flash uses 24 MHz timings. It does not step down to 4 MHz.
- §3.5: the measured IMG_DATA and IMG_END costs replace the estimates.
- §4.2: `t_centi`/`rh_centi` currently carry 0.1 resolution (the value ×10).
- §4.6: the stored settings record format is now defined.
- §8: the SWD session trigger (`link_bench_req`), no automatic sessions in mailbox builds, PB05 never switched in mailbox builds, host retries on SWD WAIT, and the measured polling cost.
- §10: question 1 is resolved, and questions 6, 7 and 9 are partly answered.

**Second pass (2026-11-01: review fixes, and notes from the ESPHome `th5_link` implementation).** Each place is marked *(changed by review)* or *(CBU note)*:
- §4.1: HELLO byte 1 is now `flags`. Bit 0 `SLOW_ONLY` (0x01) asks the MCU to offer 115200.
- §4.4: NO_HA ends with BYE NO_HA and no NET failure first.
- §4.5: TIME refuses any time from 2100 on.
- §4.11, §5.1, §5.3: CLOSING has a hard end 100 ms after BYE, even without TC, and a HELLO in CLOSING is refused. This resolves the old contradiction between "a HELLO in any state restarts the link" and "after BYE, any request gets ERR STATE".
- §5.2: a skipped session (low battery) reschedules the next try.
- §2.3: the MCU applies a baud change only after TC. The clock note adds CR.WAIT = 1 at 24 MHz.
- §3.5 and §8.4: IMG_END is ≈ 35 ms with WAIT = 1.
- §6 and §4.12: the CBU-side facts — the LibreTiny baud change, its own shutdown, what FORGET can clear, and the HA actions.

**Third pass (2026-11-01: firmware review round 2, and the ESPHome side's session changes).** Marked *(changed by review 2)*:
- §4.2: `batt_mv` 0 and `batt_pct` 0xFF while the charger is connected; the trigger list says when each trigger fires.
- §4.6: the stored record carries a local UNITS tag (0x81) outside settings_crc; record `len` 30.
- §4.10, §5.3: HOLD is refused in GRACE.
- §4.11: BYE NO_HA also covers the CBU's soft session deadline.
- §5.4: a granted-HOLD session (maintenance, OTA, bench) that ends on the cap schedules like OK; the boot-session gate and the low-battery stop.
- §6: the `session` string gains a `boot` field; maintenance can be switched off; OTA; the soft session deadline; `th5_done` is answered before the image push; the shutdown no longer waits for the API after a failure.

---

## 1. Roles and conventions

- **The CBU leads the session.** It sends HELLO and every request that moves the session forward. The MCU answers.
- **The MCU originates only two requests:** FORGET (§4.12) and, optionally, PING. It sends them only in state LINKED (§5), and only when it has nothing outstanding.
- **The MCU enforces every limit.** It owns the CBU's power (PB05, active low), the HELLO timeout, the session cap, HOLD and the backoff.
- **Byte order:** every multi-byte field is little-endian, the CRC included. Signed fields are two's complement.
- **Payload layouts are byte offsets.** C code reads and writes them with `link_get*/link_put*`, never through packed structs.
- **Names:** "request" = any command without bit 7. "Response" = ACK, HELLO_RSP or ERR (bit 7 set). A response is never answered.

---

## 2. Physical layer

### 2.1 Line

- **Pins:** UART0 on the HC32. PB06 is MCU TX, which goes to CBU RX1 (P10, pad `RXD`). PB07 is MCU RX, which comes from CBU TX1 (P11, pad `TXD`). Source: [uart-capture](../evidence/uart-capture-2026-10-31.md).
- **Format:** 8N1, no flow control, LSB first.
- **Rates:** 115200 at the start of every session. 500000 after the switch in §2.3.
  - HC32 at 24 MHz: 115200 = ÷16 with SCNT 13 (115 385, +0.16 %); 500000 = ÷8 with SCNT 6 (exact).
  - BK7231N at 26 MHz: 500000 = ÷52 (exact). **(verify both divider tables)**

### 2.2 Power-on and boot noise

1. **MCU, before PB05 goes low:**
   - Configure PB06 as UART TX, idling high, and PB07 as RX.
   - Clear the RX buffer and enable RX.

   Stock parks both pins low while the CBU is off, and the sniffer sees stray `00` bytes at each power-up. Our firmware parks them the same way between sessions, so that no I/O pin back-powers an unpowered CBU.
2. **MCU, at PB05 low:** start the HELLO timer (5 s, §5).
3. **The BK7231N boots.** Its ROM and LibreTiny print text and line noise on UART1 at whatever rate they use. **The MCU treats every byte before a valid frame as garbage** (§3.4): it parses, discards, and counts it.
   - Exception: a complete, checksum-valid **Tuya MCU frame** means a stock or wrong CBU. A Tuya frame is `55 AA ver cmd len_hi len_lo data… sum8`, with ver 0 or 3 and len ≤ 64. Stock's first frame is `55 AA 00 01 00 00 00`. On such a frame the MCU ends the session at once, with result TUYA (§5.4).
   - A bare `55 AA` in the noise does **not** count.
4. **CBU:** `logger: baud_rate: 0`. Nothing but frames may leave UART1 after boot.
5. **CBU:** sends HELLO at 115200 as soon as `th5_link` is set up, and **always** sends HELLO at 115200, including after a reboot during HOLD.

### 2.3 Baud switch

This is design §3 step 5 [emb M7], made exact. There is one fast rate, 500000, and one fallback, 115200.

**The switch:**

1. HELLO_RSP carries `baud`, the rate both sides use after that frame.
   - The MCU offers 500000 unless this session has already fallen back, or HELLO has the `SLOW_ONLY` flag (4.1). Then it offers 115200, and nothing below happens.
   - *(Changed by review.)* The MCU changes its rate only once its transmitter is idle (TC). That applies to every change: the switch below and any revert (R1). A revert decided while a response is still in the shifter takes effect after that response's last stop bit.
2. **MCU:** when the last stop bit of HELLO_RSP has left the shifter (UART TC flag), it switches to `baud`, resets its parser, and opens the **probe window** of 100 ms.
3. **CBU:** on a valid HELLO_RSP with baud 500000, it switches at once and resets its parser. It waits ≥ 2 ms (`LINK_T_PROBE_GAP_MS`), then sends PING (4.2) at 500000.
   - This **probe attempt** has a timeout of 98 ms, so it ends 100 ms after the switch. It does not count as a retry.
4. **MCU:** any valid frame inside the window confirms 500000 for this session. If no valid frame arrives within the window, the MCU reverts to 115200.
5. **CBU:** if the probe attempt times out, it reverts to 115200, waits 10 ms (`LINK_T_SETTLE_MS`), and sends **the same PING (same seq)** again at 115200, with the normal timeout and the normal 3 retries.

**Revert rules (R1),** which apply for the rest of the session, on both sides:

- **R1a:** a request sent at 500000 timed out → revert to 115200 before resending it.
- **R1b:** the parser discarded a byte, and no valid frame has arrived for ≥ 50 ms (`LINK_T_GARBAGE_MS`) → revert. Before the first valid frame, the 50 ms run from the switch.

**Sticky:** a revert lasts until the CBU is powered off. Later HELLOs in the same session (a CBU reboot under HOLD) get `baud = 115200`. The next session tries 500000 again.

**Why this converges.** R1a and R1b move each side to 115200 on its own. The worst case is a lost probe ACK: the MCU stays at 500000 and the CBU drops to 115200.
- The CBU's retry at 115200 looks like garbage to the MCU, and the probe was ≥ 50 ms earlier, so R1b moves the MCU down.
- The next retry gets through. It is a duplicate, so the MCU re-ACKs it (3.5).
- Any single lost frame at 500000 costs at most two of the three retries. The loopback tests inject each of these cases: `test_lost_hello_rsp_falls_back_to_115200`, `test_lost_probe_ack_converges`, `test_bad_fast_link_reverts_both`, `test_duplicate_seq_is_reacked_not_reapplied`.

**Clock note** *(changed by the implementation)*. At 4 MHz, 115200 is not reachable: the nearest rate is 125 000, 8.5 % off. So UART0 must run at 24 MHz.
- The original plan was to step down to 4 MHz around every flash operation. It was dropped. The MCU now switches HCLK to RCH 24 MHz **once, before PB05 goes low,** and back to 4 MHz after PB05 goes high.
- `flash_init()` loads the 24 MHz flash timings (the 4 MHz table ×6, as HDSC's DDL does) after each switch. The driver refuses to erase or program if HCLK changed since the last `flash_init()`.
- Why: there is no clock step, no SCNT reload and no UART re-init in the middle of a session, and the flash work runs 6× faster (§3.5).
- Hardware result: HCLK measured against the RTC crystal is 24.17 MHz (+0.7 %) and 4.04 MHz. Erase and program at 24 MHz were verified over all 49 slot-B sectors.
- The trim is stepped through the factory 8/16/22/24 MHz words with HCLK ÷128 during each step, as stock does.
- *(Changed by review.)* **CR.WAIT = 1 for the session.**
  - Why: DDL uses one flash wait state above 24 MHz and none at or below it. The measured 24.17 MHz is just over that line. (Sources: DDL's `Flash_WaitCycle`, and the HC32L17x datasheet's IDD table, which runs 24 MHz with no flash wait and 32/40/48 MHz with FlashWait = 1. The reference manual is not available here.)
  - `clock_set_fast()` sets WAIT before HCLK goes up and clears it after HCLK is back at 4 MHz.
  - On the device, FLASH_CR reads 0x204 during the session and 0x200 after it.
  - The cost is the CRC over flash at IMG_END: ≈ 35 ms instead of ≈ 22 ms.

---

## 3. Framing

### 3.1 Frame

```
 off   size  field
  0     1    sof      0xA5
  1     1    ver      protocol version of this frame (1)
  2     1    seq      requester's sequence number; a response echoes it
  3     1    cmd      command; bit 7 = response
  4     2    len      payload length, LE
  6     len  payload
 6+len  2    crc      CRC-16/CCITT-FALSE, LE
```

Overhead: 8 bytes. The largest frame is 1032 bytes (CBU→MCU) or 264 bytes (MCU→CBU).

### 3.2 CRC

- **Algorithm:** CRC-16/CCITT-FALSE. Poly 0x1021, init 0xFFFF, not reflected (MSB first), xorout 0. Check value: `"123456789"` → `0x29B1`.
- **Coverage:** bytes 1 … 5+len, that is `ver`, `seq`, `cmd`, `len` and the payload. The `0xA5` is **not** covered, because it carries no information.
- **Transmission:** `crc & 0xFF` first, then `crc >> 8`.
- **Implementation:** `link_crc16()` uses a 16-entry nibble table (32 B). The HC32 CRC unit may be used instead if it gives the same check value.

### 3.3 Limits

| Direction | Max payload | Receiver buffer |
|---|---|---|
| CBU → MCU | 1024 B (`LINK_MAX_TO_MCU`) | MCU: 1032 B |
| MCU → CBU | 256 B (`LINK_MAX_TO_CBU`) | CBU: ≥ 264 B. The LibreTiny RX ring is 4096 B, set with `-DLT_SERIAL_BUFFER_SIZE` [ha M7] |

- Each side announces the largest payload it accepts: CBU `rx_max` in HELLO, MCU `rx_max` in HELLO_RSP. Neither side sends more than the peer's value. In v1 the values are fixed at 256 and 1024.
- A receiver treats a header whose `len` exceeds its own maximum as a false start (3.4).

### 3.4 Receiver and resync

The receiver keeps the bytes it has not consumed in a buffer and repeats these steps:

1. **Hunt.** Drop bytes until the buffer starts with `A5`.
2. **Header.** Wait for 6 bytes. If `ver` is not a version this side speaks (v1: only 1), or if `len` > its maximum, drop the `A5` and go to step 1.
3. **Body.** Wait for `len + 2` more bytes, then check the CRC. If it fails, **drop only the `A5`** and go to step 1, rescanning the bytes already buffered. A real frame hidden behind a false start is found this way.
4. **Inter-byte timeout.** If a partial frame sees no byte for 20 ms (`LINK_T_INTERBYTE_MS`), drop its `A5` and rescan.
5. **Count.** Every dropped byte counts as *discarded* for R1b (2.3). Every complete frame counts as a *valid frame*.

A frame whose `ver` is known but different from the session version (§4.1) is answered with ERR VERSION and not applied.

### 3.5 Stop-and-wait, sequence numbers, timeouts, duplicates

- **One outstanding request at a time, across both directions** [emb M6].
  - A side sends a new request only after the response to its previous one arrived, or after that request finally failed.
  - The CBU never sends anything unsolicited while the MCU might be programming flash, because programming only happens before the MCU ACKs a CBU request.
- **seq:**
  - Each side has its own 8-bit counter for the requests *it* originates. It starts at 0 when that side boots (the CBU) or when the CBU is powered (the MCU).
  - The counter increments for each new request and wraps at 256.
  - A retry reuses the seq and is byte-identical to the first attempt.
  - A response carries the seq of the request it answers.
- **Matching:** the requester accepts a response only if all of these hold:
  - seq = the outstanding seq;
  - for ACK and ERR, `payload[0]` = the outstanding cmd;
  - for HELLO_RSP, the outstanding cmd is HELLO.

  It ignores every other response, such as a late duplicate ACK.
- **Timeouts:** 200 ms (`LINK_T_ACK_MS`), or 1 s for IMG_BEGIN, IMG_END, FW_BEGIN and FW_END. Each runs from the end of the request's transmission to the receipt of the complete response.
  - **Responder budget:** ≤ 150 ms, or ≤ 800 ms for IMG_BEGIN, IMG_END, FW_BEGIN and FW_END, from the last byte received to the first response byte sent.
  - *(Changed by the implementation: these are the measured costs at 24 MHz.)* A real IMG_DATA costs **≤ 20 ms** on hardware: up to 3 lazy sector erases (≈ 4.7 ms each, the CPU stalls) plus 255 word programs. Already-blank sectors are not erased.
  - IMG_END costs **≈ 35 ms** with CR.WAIT = 1 (≈ 22 ms with WAIT = 0): the CRC-32 over 25 KB of flash plus one word program.
- **Retries:** 3 after the first attempt, so 4 attempts in all. The baud probe attempt is extra (2.3). After the 4th timeout the request has **failed**:
  - **CBU:** abandons the session. It goes through its orderly shutdown, without further frames, and lets the MCU's limits end it.
  - **MCU** (FORGET): keeps FORGET pending for the next session.
- **Duplicates:**
  - Each side remembers, for the last request it answered, the triple `(seq, cmd, crc16)` and the exact response bytes.
  - A request with the same triple is a duplicate. The side **resends the stored response and does not apply the request again.**
  - A request with the same seq but a different cmd or CRC is a new request.
  - Exception: **HELLO is never treated as a duplicate.** It is idempotent and always processed (4.1).
- **Collisions:** if both sides send a request at the same moment:
  - **CBU:** while it has a request outstanding, it **drops** any request from the MCU without answering.
  - **MCU:** answers the CBU's request normally and keeps waiting for its own response. That response never comes, so it resends on timeout.

  The CBU's request always wins. The MCU's retry gets through once the CBU is idle.

### 3.6 Responses

- **ACK** `0x80`: `payload[0]` = the acknowledged cmd, followed by command-specific data (§4). An ACK means *applied and durable*: flash is programmed, settings are written, the RTC is set.
- **HELLO_RSP** `0x81`: answers HELLO in place of an ACK.
- **ERR** `0x82`: `cmd u8, code u8, detail u32`. The request was **not** applied, and nothing changed.
  - ERR is final for that request: the requester does not retry it.
  - Exception: on BUSY, it may send the request again after ≥ 100 ms, as a new request with a new seq.

| Code | Name | Meaning | `detail` |
|---|---|---|---|
| 1 | UNKNOWN_CMD | cmd not known or not valid in this direction | 0 |
| 2 | BAD_LEN | payload shorter than the minimum, or longer than allowed (PING), or a bad IMG_DATA length | minimum length, or the CONFIG TLV offset |
| 3 | BAD_VALUE | a field is out of range | payload offset of the field, or the CONFIG tag |
| 4 | VERSION | protocol version not supported | highest version the responder speaks |
| 5 | STATE | not valid now (before HELLO, while closing, IMG_DATA/END without BEGIN) | 0 |
| 6 | OFFSET | IMG_DATA not at the next offset, or IMG_END before all data | next offset expected |
| 7 | TOO_BIG | IMG length or chunk beyond the limit | largest acceptable length |
| 8 | CRC | IMG_END: the CRC-32 over the received data differs | CRC-32 the MCU computed |
| 9 | FLASH | erase or program failed verify | flash address |
| 10 | BUSY | cannot do it now; retry later with a new seq | 0 |

---

## 4. Commands

**General rules:**
- **Growth:** fixed layouts may **grow at the end** in later versions. A receiver accepts `len ≥` the v1 length and ignores the extra bytes. It answers BAD_LEN only when the payload is too short.
- **Reserved:** reserved fields are sent as 0 and ignored on receipt.

| cmd | Name | Dir | Payload | Response |
|---|---|---|---|---|
| 0x01 | HELLO | C→M | 8 | HELLO_RSP |
| 0x02 | PING | C→M (M→C allowed) | 0–16 | ACK + echo |
| 0x03 | NET | C→M | 3 | ACK |
| 0x04 | TIME | C→M | 12 | ACK |
| 0x05 | CONFIG | C→M | TLV, 0–1024 | ACK |
| 0x06 | IMG_BEGIN | C→M | 20 | ACK + START/HAVE |
| 0x07 | IMG_DATA | C→M | 5–1024 | ACK + next offset |
| 0x08 | IMG_END | C→M | 8 | ACK |
| 0x09 | HOLD | C→M | 2 | ACK + granted s |
| 0x0A | BYE | C→M | 1 | ACK |
| 0x0B | FW_BEGIN | C→M | 16 | ACK + START/HAVE |
| 0x0C | FW_END | C→M | 4 | ACK |
| 0x40 | FORGET | M→C | 0 | ACK |
| 0x80 | ACK | ↔ | 1+ | none |
| 0x81 | HELLO_RSP | M→C | 44 | none |
| 0x82 | ERR | ↔ | 6 | none |

### 4.1 HELLO (CBU → MCU), 8 bytes

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | proto_max | Highest protocol version the CBU speaks (1) |
| 1 | u8 | flags | *(Changed by review; was "reserved, 0".)* Bit 0 `SLOW_ONLY` (`LINK_HELLO_F_SLOW_ONLY` = 0x01): the CBU will not switch rates, so the MCU offers 115200 in HELLO_RSP. Other bits are 0, and the MCU ignores them |
| 2 | u16 | rx_max | Largest payload the CBU accepts (256) |
| 4 | u32 | fw_version | `th5_link` version, `major<<24 \| minor<<16 \| patch` |

- **Header version:** HELLO and HELLO_RSP are always sent with header `ver = 1`. Every later frame of the session uses the version chosen in HELLO_RSP.
- **The MCU accepts HELLO in any session state, at any time.** A CBU that rebooted starts over. On HELLO the MCU:
  - aborts any IMG transfer: the inactive slot stays uncommitted, and there is no resume;
  - clears its duplicate memory;
  - keeps the session deadline as it is.
- If `proto_max` = 0: ERR VERSION, with detail = 1.

- **`SLOW_ONLY` and older MCUs.** The CBU sets the flag whenever it will not switch rates (`th5_link`: `fast_baud` off; the ESPHome side still has to add this). An MCU built before the flag existed ignores byte 1 and may still offer 500000. In that case the CBU stays at 115200 and silent for ≥ 100 ms. The probe window then lapses and the MCU reverts by itself (2.3 step 4).

### 4.2 HELLO_RSP (MCU → CBU), 44-byte base plus optional history

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | proto | `min(proto_max, MCU max)`; 1 |
| 1 | u8 | reset_cause | Cause of the last MCU reset. Bits: 0 POR, 1 BOR, 2 LVD, 3 WDT, 4 PIN, 5 SOFT, 6 LOCKUP, 7 OTHER |
| 2 | u32 | baud | Rate after this frame: 500000 or 115200 (2.3) |
| 6 | u16 | rx_max | 1024 |
| 8 | u32 | fw_version | MCU firmware, `major<<24 \| minor<<16 \| patch` |
| 12 | u64 | frame_id | Id of the active committed slot; **0 = no valid frame**. HA never uses 0 as an id |
| 20 | u32 | settings_crc | CRC-32 of the canonical settings (4.5) |
| 24 | u8 | units | 0 °C, 1 °F |
| 25 | u8 | trigger | 0 TIMER, 1 BUTTON (top), 2 UNITS (C/F, at the 2nd minute tick after the last press: 60–120 s), 3 BOOT (no time or frame), 4 CHARGE (charge state changed; not within 15 min of the last session), 5 BENCH (top button held 10 s), 6 RETRY (backoff or no-HELLO retry) |
| 26 | u16 | flags | Bits: 0 TIME_VALID, 1 FRAME_VALID, 2 SENSOR_OK, 3 FORGET_PENDING, 4 BENCH, 5 FACTORY_RESET (settings erased since the last successful session) |
| 28 | i16 | t_centi | Temperature in **0.01 °C**, with the T offset applied. −32768 = no reading. *(Changed by the implementation: the firmware currently keeps 0.1 °C, so this is t10 × 10. The same holds for `rh_centi`. The wire format is unchanged.)* |
| 30 | u16 | rh_centi | Relative humidity in **0.01 %RH**, with the RH offset applied, clamped to 0…10000. 0xFFFF = no reading |
| 32 | u16 | batt_mv | Battery in mV, **at rest**, measured before PB05 went low (design §4.6). *(Changed by review 2.)* 0 while the charger is connected (charging or full): the reading is not the cell's |
| 34 | u8 | batt_pct | 0–100. 0xFF = unknown. *(Changed by review 2.)* 0xFF while the charger is connected; `charge` tells charging from full |
| 35 | u8 | charge | 0 discharging, 1 charging, 2 full, 3 unknown |
| 36 | u32 | last_session_ms | How long the CBU was powered in the previous session. 0 = none since boot |
| 40 | u8 | last_result | Result of the previous session (5.4). 0 = none |
| 41 | u8 | fail_count | Consecutive failed sessions, saturating at 255 |
| 42 | u8 | boot_ver | Bootloader version (design §4.7). 0 = no bootloader, so FW_BEGIN is refused |
| 43 | u8 | reserved | 0 |

**Optional history tail** (new MCU, bounded by CBU `rx_max`): byte 44 is count (0–8), byte 45 is flags (currently 0), followed by count 17-byte records in oldest-first order. Each record is LE: UTC hour start u32, temperature mean/min/max i16 in 0.01 °C, humidity mean/min/max u16 in 0.01 %RH, and sample count u8. Older MCUs end at byte 43. If `rx_max` permits fewer records, only the oldest fitting records are sent; if it is 44 or 45 there is no tail. Receivers must validate the count, flags and records before use, and ignore any bytes after the last record (room for later fields).

The MCU samples once per RTC minute while a valid TIME has been received since boot, independently of display cadence and mode. Failed readings do not count; partial observed hours have fewer samples. No empty hours are synthesized. History is volatile and lost on MCU reset. A clock correction over two minutes discards the partial hour; completed records remain, and repeated minutes after a backwards jump are ignored. At most eight completed hours are retained, dropping the oldest on overflow.

The MCU snapshots which completed records were sent on the latest HELLO. They remain available across failed or aborted sessions and old one-byte BYE messages. A DONE with HISTORY_ACK in BYE's optional flag byte acknowledges only that snapshot. Hours completed later remain queued. HA should deduplicate retries using the UTC hour and statistic ID. Because the producer and acknowledgement are volatile, a reset loses pending history.

- **Units are always °C** in `t_centi`, whatever the display shows. `units` is the display setting, and the CBU uses it to choose which frame to request.
- **The reading is taken right before the session** (SHT40, then the ADC), never during Wi-Fi [emb M9].
- **Why 0.01 °C:** see D6.

### 4.3 PING (either direction), 0–16 bytes

- The payload is arbitrary, and the ACK echoes it: `ACK [0x02, payload…]`. A payload over 16 bytes gets ERR BAD_LEN, with detail 16.
- The CBU uses it for the baud probe (2.3), with payload `55 AA 00 FF`.
- The MCU may use it as a liveness check under HOLD. v1 MCU firmware does not.

### 4.4 NET (CBU → MCU), 3 bytes

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | state | 1 WIFI_CONNECTING, 2 WIFI_UP, 3 API_UP, 4 HA_CONFIG (`th5_config` received), 5 OTA (OTA running). **Bit 7 = failure:** 0x80 FAIL_WIFI, 0x81 FAIL_API, 0x82 FAIL_NO_HA, 0x83 FAIL_NO_CREDENTIALS |
| 1 | u8 | detail | Optional, such as the ESPHome Wi-Fi disconnect reason. 0 otherwise |
| 2 | i8 | rssi | dBm; 0 = unknown |

- NET is informational and **never changes the MCU's settings**.
- **On a failure state,** the MCU ACKs, then gives the CBU **2 s** (`LINK_T_NET_GRACE_MS`) to send BYE: the CBU needs that time to sync its flash. On BYE it powers off as usual. Otherwise it powers off when the 2 s are up. Either way the result is NET_FAIL.
- The MCU keeps the last state for diagnostics.
- *(CBU note.)* **NO_HA sends no NET failure first.**
  - When HA never calls `th5_config` (3 s after the API connects), `th5_link` runs its normal shutdown (§6) and ends with **BYE NO_HA**. It does not send NET FAIL_NO_HA (0x82) first.
  - The MCU's result is NET_FAIL either way (4.11). Skipping the NET avoids starting the 2 s grace in the middle of the API disconnect and preferences sync.
  - 0x82 stays defined. An MCU must still accept it and apply the grace.

### 4.5 TIME (CBU → MCU), 12 bytes

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u32 | utc | Seconds since 1970-01-01 UTC, **at the moment the first byte of this frame is sent**. The CBU adds the time elapsed since `th5_config` arrived |
| 4 | i16 | offset_min | Current local offset in minutes; local = UTC + offset |
| 6 | u32 | next_change_utc | UTC of the next offset change. 0 = none known |
| 10 | i16 | next_offset_min | Offset in force from `next_change_utc` on |

- **Validation:**
  - `utc ≥ 1735689600` (2025-01-01), else BAD_VALUE with detail 0.
  - `|offset| ≤ 1080`, else detail 4.
  - `next_change_utc` is 0 or greater than `utc`, else detail 6.
  - `|next_offset| ≤ 1080`, else detail 10.
  - *(Changed by review.)* Local time `utc + offset` must be before 2100-01-01 (4102444800), else detail 0. `next_change_utc` must also be before 2100, else detail 6. The RTC stores a 2-digit BCD year.
- **On ACK:** the RTC is set and TIME_VALID holds.
- **DST transition:** when the RTC passes `next_change_utc` between syncs, the MCU applies `next_offset_min` itself [ha m6].
- u32 runs until 2106.

### 4.6 CONFIG (CBU → MCU), TLV list

The payload is a sequence of `tag u8, len u8, value[len]`, little-endian values. Rules:

- **Delta update.** Tags that are present are set. Absent tags keep their stored values.
- **Unknown tags are skipped** using their `len`. Tag values 0 and 0x80–0xFF are reserved for future use.
- **Atomic.** If any known tag has the wrong `len`, the MCU answers ERR BAD_LEN with detail = the TLV's offset. A TLV that runs past the end gets the same answer. If any value is out of range, it answers ERR BAD_VALUE with detail = the tag. In both cases **nothing** is applied.
- **Flash.** The MCU writes the settings sector (ping-pong) **only if the canonical CRC changes**, and does so before the ACK. Resending identical settings each session costs no flash wear.

| Tag | Name | Type | Range | Default | Meaning |
|---|---|---|---|---|---|
| 1 | MODE | u8 | 0–1 | 1 | 0 picture, 1 forecast. In MODE 1 the MCU draws its live band over any frame with FRAME_FLAG_BAND; HA (0.2.6) also sends 1 for a picture with the top bar, whose frame carries the flag |
| 2 | SYNC_MIN | u16 | 15–240 | 60 | Sync interval, minutes |
| 3 | CLOCK_MIN | u8 | 1–30 | 1 | Clock/band update period, minutes |
| 4 | CLOCK_24H | u8 | 0–1 | 1 | 0 = 12 h, 1 = 24 h |
| 5 | FULL_N | u8 | 1–255 | 30 | Band updates between forced full refreshes |
| 6 | T_OFF | i16 | −1000…1000 | 0 | Temperature offset, 0.01 °C |
| 7 | RH_OFF | i16 | −2000…2000 | 0 | RH offset, 0.01 %RH |
| 8 | FLAGS | u8 | 0–15 | 0 | Bit 0 BADGE (MODE 0 only; HA 0.2.6 and later always send 0), bit 1 MAINTENANCE, bit 2 monochrome live band, bit 3 experimental single-reset grey band |

**settings_crc** = zlib CRC-32 over the **canonical encoding**: all 8 v1 tags, in ascending tag order, each with its v1 length, holding the MCU's current values. With the defaults it is `0xFAFE77C2`.
- HA can compute the same value from its desired settings and skip the update when they match.
- A future MCU with more tags adds them to the canonical set. The CRC then differs from an older HA's, which only means that HA resends.

**Why TLV:** see D7.

**Stored record** *(added by the implementation; `firmware/src/settings.h`)*. The settings sectors 0x1FC00 and 0x1FE00 form a ping-pong pair. Each write goes to the sector that does not hold the current record, is read back, and only then becomes current. The record at the start of a sector is:
- `magic` u32 = `0x53354854` ("TH5S"), `version` u16 = 1, `len` u16 = 30 (27 in records written before review 2, still accepted);
- `seq` u32, +1 on each write; the valid record with the higher seq wins at boot;
- the canonical TLV of all 8 tags (27 bytes), so settings_crc is the CRC-32 of exactly these bytes;
- *(changed by review 2)* then local tags the CBU never sees, outside settings_crc: `0x81 UNITS` u8 (0 °C, 1 °F; written once a C/F change has settled, when the UNITS countdown runs out). A record without it means °C;
- then a zlib CRC-32 over everything before it.

Stock's settings bytes have no magic, so the first write leaves them in the other sector unchanged. A CONFIG whose canonical CRC equals the stored one does not touch flash.

### 4.7 IMG_BEGIN (CBU → MCU), 20 bytes

The fields are those of the slot header in `frame.h`, so the MCU can program the header right away.

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u64 | frame_id | ≠ 0. HA's id (truncated SHA-256 of the payload, read as LE u64) |
| 8 | u8 | bpp | 1 or 2 |
| 9 | u8 | encoding | 0 raw. PackBits is reserved and answered with BAD_VALUE in v1 |
| 10 | u8 | flags | Frame flags; bit 0 BAND |
| 11 | u8 | reserved | 0 |
| 12 | u32 | length | Payload bytes. In v1 it must equal `12480 × bpp`, and it is never more than 25 056 (slot − header) |
| 16 | u32 | crc32 | zlib CRC-32 of the payload |

**The ACK data is one byte:**

- **1 = HAVE.** The active committed slot already holds `frame_id`. The CBU skips the transfer, sending no DATA and no END. Nothing is written.
- **0 = START.** The MCU has:
  1. chosen the inactive slot (A if neither is valid);
  2. erased the slot's first sector;
  3. programmed the 32-byte header, with `seq = active.seq + 1` (or 1), the fields above, and the commit word left erased (`0xFFFFFFFF`).

  It expects offset 0 next.

**Errors:**

| Condition | Answer |
|---|---|
| frame_id = 0 | BAD_VALUE, detail 0 |
| bpp ∉ {1, 2} | BAD_VALUE, detail 8 |
| encoding ≠ 0 | BAD_VALUE, detail 9 |
| bad length | TOO_BIG, detail 25 056 |
| erase or program failure | FLASH, detail = address |

An IMG_BEGIN while another transfer is open abandons the old transfer and starts again.

### 4.8 IMG_DATA (CBU → MCU), 5–1024 bytes

| Off | Type | Field |
|---|---|---|
| 0 | u32 | offset into the payload (not counting the 32-byte header) |
| 4 | u8[n] | data, 1 ≤ n ≤ 1020 (`LINK_IMG_CHUNK`) |

- **Chunk size:** the CBU sends **1020-byte chunks**, with a shorter last chunk. A 2 bpp frame is 25 chunks: 24 × 1020 + 480.
- **Alignment:** `n` must be a multiple of 4, except in the last chunk. `offset` is then always word-aligned, so the MCU programs whole words.
- **MCU rules**, where `next` is the number of bytes accepted so far:

  | Condition | Answer |
  |---|---|
  | `offset + n > length` | TOO_BIG, detail = length |
  | `offset + n ≤ next` (a late repeat with a new seq) | ACK [0x07, next u32], **without writing** |
  | `offset ≠ next` | OFFSET, detail = next |
  | `n % 4 ≠ 0` and not the last chunk | BAD_LEN, detail 4 |
  | otherwise | Erase every not-yet-erased sector the chunk touches (lazy, per sector, so ≤ 3 erases); program; verify; `next += n`; ACK [0x07, next u32] |

- No IMG_BEGIN in this session → STATE.
- The CBU continues from the `next` in each ACK. On ERR OFFSET it continues from `detail`.

### 4.9 IMG_END (CBU → MCU), 8 bytes

| Off | Type | Field |
|---|---|---|
| 0 | u64 | frame_id: must match IMG_BEGIN |

**The MCU's checks, in order:**

1. No open transfer → STATE.
2. Id mismatch → BAD_VALUE, detail 0.
3. `next ≠ length` → OFFSET, detail = next.

**Then it commits:**

1. It computes the CRC-32 **over the flash contents** of the slot payload.
2. If the CRC differs → ERR CRC, detail = the computed CRC. The transfer is closed and the slot stays uncommitted. **There is no resume** [emb M5]: the next session sends the whole frame again.
3. If the CRC matches, it programs the commit word to 0 and then sends `ACK [0x08]`. The new frame is active from then on.

The new frame reaches the panel with a full refresh **after** the CBU is off (design §3 step 10).

### 4.10 HOLD (CBU → MCU), 2 bytes

| Off | Type | Field |
|---|---|---|
| 0 | u16 | seconds, 1–900 |

- **The deadline:** `deadline = max(deadline, min(now + seconds, session_start + 900 s))`. HOLD only ever raises the deadline, and no session lasts more than 900 s.
- **While holding,** neither the 5 s HELLO limit nor CBU silence ends the session. A CBU reboot followed by a fresh HELLO is accepted (4.1).
- **ACK:** `[0x09, granted u16]`, the seconds left until the deadline.
- 0 or > 900 → BAD_VALUE, detail 0.
- **Uses** (design §6.1, §6.3): maintenance and OTA (the CBU sends HOLD 900 and skips `th5_done`), and adoption.
- *(Changed by review 2.)* **Refused in GRACE** with ERR STATE: after a NET failure the CBU has only the 2 s to send BYE, and a HOLD must not stretch it.
- **Maintenance off:** a later `th5_config` without MAINTENANCE does not shorten the deadline on the MCU; the CBU simply sends BYE DONE on `th5_done` (§6 item 4).
- **Bench mode** (top button held 10 s) is the MCU's own HOLD 900. It sets BENCH in HELLO_RSP, which tells `th5_link` to stay connected.

### 4.11 BYE (CBU → MCU), 1 or 2 bytes

| Off | Type | Field |
|---|---|---|
| 0 | u8 | reason: 0 DONE (HA called `th5_done`), 1 NO_HA (no `th5_config` within 3 s, or *(changed by review 2)* HA did not finish within the CBU's soft session deadline, §6 item 6; a frame may still have been committed), 2 NET_FAIL, 3 LINK (internal error), 4 FORGOT (after FORGET) |
| 1 | u8 | Optional flags: bit 0 HISTORY_ACK, set only after HA successfully imports the history sent in this session; other bits 0. Ignored for reasons other than DONE and during GRACE. Omitted means no acknowledgement. |

- **The CBU sends BYE only after its flash is synced** (`global_preferences->sync()`) and the radio is off [emb M8, ha B3].
- **The MCU** ACKs, and **cuts PB05 50 ms after the ACK's last byte** has left (`LINK_T_BYE_OFF_MS`). It then parks PB06/PB07 low.
- **After BYE,** any further request gets ERR STATE. *(Changed by review.)* That includes HELLO, so a CBU that reboots between BYE and power-off cannot turn the result into CAP.
- *(Changed by review.)* **Hard end:** if the ACK's TC is never seen, the MCU still ends the session 100 ms after it queued the BYE ACK (`LINK_T_BYE_TC_MAX_MS`), with BYE's result. It also ends with BYE's result if the deadline passes first.
- **Result:** OK for DONE and FORGOT, NET_FAIL for the others.

### 4.12 FORGET (MCU → CBU), 0 bytes

- **When it is sent:** after a factory reset (both buttons held together 10 s) the MCU sets FORGET_PENDING. It sends FORGET once it is in state LINKED, after the baud probe, while nothing is outstanding.
- **CBU:**
  1. Clears its saved Wi-Fi and API preferences, which are stored outside the compiled-in YAML. *(CBU note.)* `th5_link` calls `global_preferences->reset()`. That cannot forget Wi-Fi credentials compiled in from the YAML: those need a reflash. So FORGET clears only what the CBU saved at run time, such as `fast_connect`'s BSSID and channel, and component state. No preference writes follow it in that session.
  2. Syncs flash.
  3. ACKs `[0x40]`.
  4. Sends BYE FORGOT.
- **MCU:** clears FORGET_PENDING only on the ACK. FORGET_PENDING lives in RAM; it survives deep sleep but not a reset.

### 4.14 FW_BEGIN (CBU → MCU), 16 bytes

This starts an MCU firmware transfer into the staging area (design §4.7). Its fields are those of the package header.

| Off | Type | Field |
|---|---|---|
| 0 | u32 | fw_version |
| 4 | u32 | length: 64 ≤ length ≤ 50 144, a multiple of 4 |
| 8 | u32 | crc32 (zlib) of the image |
| 12 | u32 | load address, 0x00001000 |

**The ACK data is one byte:**
- **1 = HAVE:** the running app has this `fw_version` and the installed app region `[0x1000, 0x1000 + length)` has this CRC. The CBU skips the transfer.
- **0 = START:**
  - The MCU erases the first sector of both frame slots, so no frame is valid any more.
  - It programs the staging header with commit and done erased, `min_boot_ver` = 1.
  - It expects IMG_DATA at offset 0. The offsets are image offsets: the MCU adds the 32-byte header.

**Errors:**

| Condition | Answer |
|---|---|
| no bootloader (`boot_ver` = 0) | STATE, detail 1 |
| a firmware transfer already completed in this session | STATE, detail 2 |
| load ≠ 0x1000 | BAD_VALUE, detail 12 |
| bad length | TOO_BIG, detail 50 144 |
| erase or program failure | FLASH, detail = address |

**Rules:**
- A FW_BEGIN abandons an open frame transfer. An IMG_BEGIN abandons an open firmware transfer.
- After a successful FW_END, IMG_BEGIN and FW_BEGIN answer STATE detail 2 for the rest of the session.
- IMG_DATA works for both transfers: same rules, with `length` from FW_BEGIN.

### 4.15 FW_END (CBU → MCU), 4 bytes

| Off | Type | Field |
|---|---|---|
| 0 | u32 | crc32: must match FW_BEGIN |

**The MCU's checks**, in this order:
1. No open firmware transfer → STATE, detail 0.
2. CRC field ≠ FW_BEGIN's → BAD_VALUE, detail 0.
3. `next ≠ length` → OFFSET, detail = next.
4. CRC-32 over the staged image in flash ≠ crc32 → CRC, detail = the computed CRC. The transfer closes uncommitted.

**On success:**
- The MCU programs the commit word to 0 and ACKs.
- After the session ends (CBU off, before anything is drawn), it resets.
- The bootloader installs the image, and the new app's boot session fetches a frame.
- The next HELLO_RSP `fw_version` is the confirmation.

### 4.13 ACK and ERR

See 3.6.

| Response to | ACK data after `payload[0]` |
|---|---|
| PING | echo |
| IMG_BEGIN | u8 START/HAVE |
| IMG_DATA | u32 next |
| HOLD | u16 granted s |
| all others | none |

---

## 5. MCU session state machine

### 5.1 States

```
          trigger, battery ok
   OFF ───────────────────────► WAIT_HELLO ── Tuya frame ───────────────► end(TUYA)
    ▲     (PB05 low, t0 = now)       │      ── 5 s, not holding ─────────► end(NO_HELLO)
    │                                │ HELLO
    │                                ▼
    │                         HELLO_RSP sent ──baud=115200──┐
    │                                │ baud=500000          │
    │                                ▼                      │
    │                              PROBE ── 100 ms, no valid frame: revert ─┤
    │                                │ valid frame at 500 k                 │
    │                                ▼                                      ▼
    │                              LINKED ◄──── HELLO (CBU reboot) ── any state except CLOSING
    │                     NET fail │      │ BYE ACKed
    │                              ▼      ▼
    │                           GRACE   CLOSING ── TC + 50 ms ──► end(BYE's result: OK | NET_FAIL)
    │                      2 s or BYE ────┘  └─ no TC by BYE + 100 ms, or deadline ──► end(BYE's result)
    └──── end(result): PB05 high, pins parked, then panel refresh
   any state except CLOSING: now ≥ deadline → end(CAP)   (CLOSING ends with BYE's result instead)
   any state: PA09 off → end(ABORT)
```

### 5.2 Timers

| Timer | Value | Notes |
|---|---|---|
| HELLO | 5 s from PB05 low | Not applied while holding |
| deadline | t0 + 45 s | HOLD raises it, up to t0 + 900 s. Bench mode starts at t0 + 900 s |
| probe | 100 ms from the HELLO_RSP TC | 2.3 |
| NET grace | deadline = min(deadline, now + 2 s) on a NET failure | 4.4 |
| BYE off | 50 ms after the BYE ACK leaves | 4.11 |
| BYE hard end | 100 ms after the BYE ACK is queued, whether or not TC was seen | 4.11 *(changed by review)* |

- **Before PB05 low:** the battery is checked at rest. Below 3.65 V, and not charging, the session is **skipped**. This is not a failure: the backoff does not change and the icon does not change.
  - *(Changed by review.)* When the skipped session was the timer or boot session, the countdown has run out. The MCU then schedules the next try as the last result would: the aligned sync interval after OK or before any session, otherwise the backoff. Without that, no sync would ever start again. A skipped button session leaves a running countdown alone.
- **While the CBU is powered,** the MCU does not deep-sleep, because UART0 needs PCLK. It feeds the WDT from the main loop as usual.

### 5.3 In-session rules

- **LINKED** accepts every C→M request (§4). Before HELLO, any request other than HELLO gets ERR STATE.
- *(Changed by review 2.)* **HOLD in GRACE** gets ERR STATE (4.10).
- **A HELLO in any state except CLOSING** restarts the link (4.1). The deadline is unchanged; the baud follows 2.3 (sticky). *(Changed by review.)* In CLOSING it gets ERR STATE (4.11).
- **Panel and flash:**
  - The MCU never refreshes the panel while the CBU is powered.
  - Flash writes happen only inside request handling, before the ACK.
- **PA09 falling** (power switch): the MCU finishes the current flash word or sector, cuts PB05, and ends with ABORT, then runs design §4.3.

### 5.4 Results and backoff

| Result | Code | When | Next session | Counters |
|---|---|---|---|---|
| OK | 1 | BYE DONE or FORGOT | sync interval, aligned to the RTC | fail_count = nohello = 0 |
| NO_HELLO | 2 | no HELLO within 5 s | 1st: +60 s, 2nd: +300 s, then **+24 h**; status "link error" (§5.5) from the first | nohello += 1, fail_count += 1 |
| TUYA | 3 | Tuya frame before HELLO | as NO_HELLO | as NO_HELLO |
| NET_FAIL | 4 | NET failure, or BYE with a reason other than DONE/FORGOT | interval × 2^fail_count, capped at 6 h | fail_count += 1 |
| CAP | 5 | deadline reached | as NET_FAIL | fail_count += 1 |
| LINK | 6 | reserved: the MCU's own link gave up (v1 does not produce it; the CBU's link failures end as CAP) | as NET_FAIL | fail_count += 1 |
| ABORT | 7 | power switch off | schedule unchanged | unchanged |

- *(Changed by review 2.)* **Maintenance is not a failure.** A session that ends in CAP after the MCU granted a HOLD (maintenance or OTA), or a bench session that got a HELLO, keeps result code CAP but is scheduled like OK (sync interval), leaves fail_count, nohello and the status line unchanged, and leaves the icon as for ABORT. A CAP without a granted HOLD, and a soft-deadline BYE NO_HA (NET_FAIL), are failures as before.
- **Buttons:** a top-button session ignores the backoff and runs at once; a C/F change runs as UNITS 60–120 s later. Only a success resets the counters.
- *(Changed by review 2.)* **Low battery:** while the MCU's low-battery latch is set (design §4.6) no session starts, automatic or button.
- *(Changed by review 2.)* **Boot session gate.** A BOOT session (no time or no frame) runs at once after a power-on, pin or soft reset. After a BOR, LVD, WDT or lockup reset it waits 30 min. If boot sessions keep starting without a success in between (counted in `.noinit` RAM, so across resets), the wait is 60, 120, 240, then 360 min. Any OK clears the count.
- **Example:** with the 60 min default, failures give retries at 2 h, 4 h, then 6 h, 6 h, …

### 5.5 What the band shows

The panel is refreshed only after PB05 is high (design §3 step 10).

| Icon (`render.h`) | When |
|---|---|
| `LINK_WAIT` | No session has ended since boot or factory reset. Also kept after a session that was skipped for low battery |
| `LINK_OK` | The last session's result was OK |
| `LINK_ERR` | The last session failed (any of NO_HELLO … LINK). ABORT leaves the icon unchanged |

- **Status message** (fallback screen, i.e. no valid frame): the band, then one centred title and hint, from the last session's outcome (`LINK_STATUS_*`, render.h `RENDER_ST_*`):
  - no session ended since boot or factory reset → "Not connected yet";
  - NO_HELLO/TUYA/LINK → "Wi-Fi module not responding";
  - NET_FAIL/CAP, by the CBU's last NET state: none, WIFI_CONNECTING, FAIL_WIFI or FAIL_NO_CREDENTIALS → "Cannot join Wi-Fi"; WIFI_UP or FAIL_API → "Cannot reach Home Assistant"; API_UP or later, or FAIL_NO_HA → "Home Assistant did not answer";
  - OK without a frame → "No picture yet"; plus the MCU's own "Battery low" and "Switched off".
  ABORT and a maintenance/bench CAP leave the message unchanged.
- **Refresh after the session:**
  - A frame was committed → a full refresh.
  - Otherwise, if the icon or any band field changed → a band update (design §5.3).

---

## 6. CBU obligations (`th5_link`)

1. **Boot and HELLO.** Send HELLO at 115200 (2.2). Do the baud switch (2.3).
2. **Report progress** with NET: WIFI_CONNECTING, then API_UP, then HA_CONFIG, or a failure state, then BYE.
3. **Publish the session state to HA.**
   - The sensors come from HELLO_RSP: T and RH from `t_centi`/`rh_centi`, battery, charge, units.
   - The `session` text sensor is `nonce;units;frame_id;settings_crc;boot` *(changed by review 2: `boot` added)*:
     - `nonce`: a random u32, 8 hex digits;
     - `units`: `C` or `F`;
     - `frame_id`: 16 lowercase hex digits of the u64, 0 = none;
     - `settings_crc`: 8 hex digits;
     - `boot`: the CBU's persisted boot counter, 8 hex digits;
     - `mcu_fw`: HELLO_RSP `fw_version`, 8 hex digits;
     - `boot_ver`: HELLO_RSP `boot_ver`, 2 hex digits.
   - HA parses by position and ignores extra fields, so later fields can be appended.
   - `last_session_ms` and `last_result` go to diagnostic sensors.
4. **On `th5_config`:** send TIME, then CONFIG.
   - Answer the HA action only after both are ACKed. Report an ERR as an action failure.
   - If MAINTENANCE is set, send HOLD 900 and skip `th5_done`. A later `th5_config` whose flags lack MAINTENANCE switches it off again; `th5_done` then sends BYE DONE while the HOLD is still running.
   - **OTA:** when an upload starts, send NET OTA, then HOLD 900. The HA and session timers are suspended; the CBU reboots into the new image and its next HELLO restarts the link.
5. **On `th5_frame`:** buffer the frame in RAM. Answer each call once its bytes are stored.
   - **On `th5_firmware`:** buffer an MCU firmware package (design §4.7) in RAM, in the same way, up to 50 176 B.
   - Refuse it at once if HELLO_RSP `boot_ver` is 0.
   - When the last byte arrives, check the header (magic, load 0x1000, length, `min_boot_ver` ≤ `boot_ver`) and the image CRC; a mismatch fails that call.
   - *(CBU note.)* `th5_link` checks the header already on the first call, which must hold its 32 bytes, and allocates exactly `32 + length` (only with 20 KB of heap to spare); a CRC mismatch frees the buffer. The package replaces the frame buffer: while a complete package is buffered `th5_frame` is refused, and a frame upload drops an unfinished package.
6. **On `th5_done`, or 3 s without `th5_config`, or the soft session deadline:**
   - *(Changed by review 2.)* **Soft deadline:** by default 25 s from CBU boot (max 38, 0 = off; skipped in bench, maintenance and OTA), so the session ends well before the MCU's 45 s cap. When it fires, a frame is pushed only if it is completely buffered, then BYE NO_HA (the MCU records NET_FAIL).
   - `th5_done` is answered **before** the image push (the push runs with the radio off), so OK does not mean committed: the next session's HELLO_RSP `frame_id` is the confirmation. A push failure is persisted and published as `last_result` in the next session.
   1. `safe_mode.mark_successful`;
   2. the orderly API disconnect;
   3. Wi-Fi off;
   4. if a complete firmware package was buffered: FW_BEGIN, then IMG_DATA × n, then FW_END, and **no frame**. Otherwise, if a frame was buffered and its id ≠ the MCU's `frame_id`: IMG_BEGIN, then IMG_DATA × n, then IMG_END. *(CBU note.)* The firmware push also runs on the soft deadline, but outside HOLD it is skipped (logged, and reported as `last_result` next session) unless its estimate ends ≥ 2 s before the MCU's 45 s cap: ≈ 6.3 s for 50 144 B at 115200, ≈ 2.8 s at 500 k;
   5. preferences sync;
   6. BYE.
7. **Respect the one-outstanding rule.** Never send a request while one is outstanding, and that includes NET.
8. **Answer MCU requests** (FORGET, PING) whenever the CBU itself has nothing outstanding.

*(CBU notes from the ESPHome implementation, `esphome/components/th5_link`; details in [integration-contract.md](integration-contract.md).)*
- **Runtime baud.** LibreTiny's UART has no `load_settings()`. `th5_link` changes the rate with `SerialClass::configure()`, which only reprograms the divider (`uart1_init` + `uart_hw_set_change`).
  - The BK UART clock is 26 MHz, so 500000 is exact (÷ 52). 115200 comes out at +0.31 % (as reported by the ESPHome implementation).
  - This is not yet tested on hardware, so `fast_baud` defaults to off, and HELLO must then carry `SLOW_ONLY` (4.1).
- **Shutdown.** On bk72xx, `deep_sleep.enter` never returns, because power is cut while it waits. So `th5_link` runs its own orderly shutdown instead of using it:
  1. `safe_mode` `mark_successful`;
  2. `APIServer::on_shutdown()`, which sends DisconnectRequest, then waits ≤ 1 s (default 500 ms) for clients to close;
  3. `wifi.disable()`, then up to 300 ms for the link to drop;
  4. the IMG transfer if the frame id differs;
  5. `global_preferences->sync()`;
  6. BYE.
  
  *(Changed by review 2.)* After a NET or link failure, or with no API client, step 2 does not wait: a connected client gets the DisconnectRequest queued, Wi-Fi goes off at once, and BYE follows within about 0.3 s plus the preferences sync, inside the 2 s GRACE.
- **HA actions** (items 4 and 5 above):
  - **`th5_config(json)`** takes `utc` (required), `offset_min`, `next_change_utc`, `next_offset_min`, `mode` (`0`/`1` or `"picture"`/`"forecast"`), `sync_min`, `clock_min`, `clock_24h`, `full_n`, `t_off`, `rh_off` (0.01 units), `badge`, `maintenance`, or an integer `flags` in place of the last two.
    - Absent keys keep the MCU's stored value. Unknown keys are ignored.
    - It is answered after TIME and CONFIG are both ACKed. An MCU ERR becomes an action failure.
  - **`th5_frame(offset, data)`** takes base64 of a byte range of the **slot image**: the 32-byte `TH5F` header plus the payload, as in `ha/out/*.slot`, at most 25 088 B.
    - Offsets must be contiguous, and offset 0 starts a new upload.
    - When the last byte arrives, the CBU checks the header and the payload CRC-32. A mismatch fails that call.
    - A complete frame is frozen: a write at offset > 0 is refused unless it is byte-identical.

---

## 7. Worked example

**The setup:**
- A button-triggered first session with an empty MCU: no frame, no time, default settings.
- HA pushes the forecast frame `ha/out/frame-C.slot` (2 bpp, id `0x25557a3ef2461ba8`, 24 960 B).
- Frames are hex, as on the wire.
- Times are from the virtual-time loopback, which gives the MCU zero processing time. On hardware each IMG_DATA ACK comes about 30 ms later.

```
t (ms)  dir  bytes
   0    C>M  (BK boot text and noise: discarded by the MCU parser)
 300.0  C>M  a5 01 00 01 08 00 | 01 00 00 01 00 00 01 00 | de 21
             HELLO seq 0: proto_max 1, rx_max 0x0100 = 256, fw 0x00010000
 301.4  M>C  a5 01 00 81 2c 00 | 01 01 20 a1 07 00 00 04 00 00 01 00
             00 00 00 00 00 00 00 00 c2 77 fe fa 00 01 04 00 5c 08 a0 14
             8c 0f 4e 00 00 00 00 00 00 00 00 00 | cd 4b
             HELLO_RSP seq 0: proto 1, reset POR, baud 0x0007a120 = 500000,
             rx_max 1024, fw 1.0.0, frame_id 0 (none), settings_crc 0xfafe77c2,
             units C, trigger BUTTON, flags 0x0004 SENSOR_OK, T 0x085c = 21.40 C,
             RH 0x14a0 = 52.80 %, 0x0f8c = 3980 mV, 78 %, discharging,
             last 0 ms, last result NONE, 0 failures
             -- MCU switches to 500 k on TC; CBU on receipt --
 307.9  C>M  a5 01 01 02 04 00 | 55 aa 00 ff | 13 7f          PING seq 1 (probe)
 308.1  M>C  a5 01 01 80 05 00 | 02 55 aa 00 ff | 29 08       ACK PING + echo: 500 k confirmed
 308.4  C>M  a5 01 02 03 03 00 | 01 00 00 | 0c 01             NET WIFI_CONNECTING
 308.6  M>C  a5 01 02 80 01 00 | 03 | 58 d5                    ACK NET
             -- Wi-Fi + API + HA (1.5 s in this run) --
1808.8  C>M  a5 01 03 03 03 00 | 03 00 c6 | 87 6e             NET API_UP, rssi -58
1809.0  M>C  a5 01 03 80 01 00 | 03 | 09 7f
1809.2  C>M  a5 01 04 04 0c 00 | 81 f1 e6 6a 3c 00 90 5b a8 6b 78 00 | f9 b7
             TIME seq 4: utc 0x6ae6f181 = 2026-11-01T08:00:01Z, offset +60 min,
             next change 0x6ba85b90 = 2027-03-28T01:00Z, then +120 min
1809.6  M>C  a5 01 04 80 01 00 | 04 | 3a 68
1809.8  C>M  a5 01 05 05 1b 00 | 01 01 01 | 02 02 3c 00 | 03 01 01 | 04 01 01 |
             05 01 1e | 06 02 00 00 | 07 02 00 00 | 08 01 00 | 7f 9a
             CONFIG seq 5: MODE 1, SYNC_MIN 60, CLOCK_MIN 1, 24H 1, FULL_N 30,
             T_OFF 0, RH_OFF 0, FLAGS 0 (= defaults: no flash write)
1810.5  M>C  a5 01 05 80 01 00 | 05 | 4a d2
1810.7  C>M  a5 01 06 03 03 00 | 04 00 c6 | b0 92             NET HA_CONFIG
1810.9  M>C  a5 01 06 80 01 00 | 03 | 5e 5c
             -- th5_done: API disconnect, radio off --
1811.1  C>M  a5 01 07 06 14 00 | a8 1b 46 f2 3e 7a 55 25 02 00 01 00
             80 61 00 00 c5 59 95 79 | 1d c8
             IMG_BEGIN seq 7: id 0x25557a3ef2461ba8, bpp 2, raw, flags BAND,
             length 0x6180 = 24960, crc32 0x799559c5
1811.6  M>C  a5 01 07 80 02 00 | 06 00 | f0 e4               ACK IMG_BEGIN START
1811.8  C>M  a5 01 08 07 00 04 | 00 00 00 00 | ff ff ff … (1020 B) | 6d 11
             IMG_DATA seq 8: offset 0, 1020 bytes (len 0x0400 = 1024)
1832.5  M>C  a5 01 08 80 05 00 | 07 fc 03 00 00 | 5e cd       ACK IMG_DATA next 1020
             … seq 9 … 31: offsets 1020 … 23460 …
2313.4  C>M  a5 01 20 07 e4 01 | a0 5f 00 00 | … (480 B) | f4 c4
             IMG_DATA seq 32: offset 24480, last 480 bytes
2323.3  M>C  a5 01 20 80 05 00 | 07 80 61 00 00 | 11 bb       ACK next 24960 = length
2323.5  C>M  a5 01 21 08 08 00 | a8 1b 46 f2 3e 7a 55 25 | cb 84   IMG_END
2323.8  M>C  a5 01 21 80 01 00 | 08 | 55 82                   ACK: CRC ok, committed
2324.0  C>M  a5 01 22 0a 01 00 | 00 | 1c 58                   BYE DONE (prefs synced)
2324.2  M>C  a5 01 22 80 01 00 | 0a | c5 4c                   ACK BYE
2374.4       PB05 high (50 ms later). Result OK: next sync in 60 min, LINK_OK,
             full refresh with the new frame.
```

**Error examples:**
- A chunk sent out of order is answered `a5 01 08 82 06 00 | 07 06 fc 03 00 00 | f8 33`. That is ERR seq 8, for IMG_DATA, code 6 OFFSET, expected 1020.
- If the ACK of seq 8 is lost, the CBU resends seq 8 at 115200 (R1a). The first resend is garbage to the MCU, which is still at 500 k, so it reverts (R1b). The second resend is recognised as a duplicate, and the stored ACK is sent again without reprogramming.

---

## 8. Bench transport: SWD mailbox

The CBU link is not wired to the dev Mac. For bench tests, a firmware built with **`-DLINK_MBOX`** replaces the UART0 driver with a RAM mailbox that the host reads and writes over SWD while the core runs.
- **The byte stream is identical** to the UART stream: the same frames, CRC, timeouts, retries and state machine.
- Only the transport differs.
- **Baud switching is a no-op.** The MCU still offers 500000 in HELLO_RSP and runs the probe, so that code path is exercised. Nothing changes on the line.

### 8.1 Layout

There is one object, `link_mbox_t link_mbox` (C symbol **`link_mbox`**, in `.bss`, 4-byte aligned). The host finds its address with `arm-none-eabi-nm th5.elf`.

| Off | Size | Field | Written by | Notes |
|---|---|---|---|---|
| 0 | 4 | magic | MCU | `0x4D354854` ("TH5M"). Written **last** in `link_mbox_init`, zeroed first |
| 4 | 2 | version | MCU | 1 |
| 6 | 2 | ring_size | MCU | 1536 (`LINK_MBOX_RING`) |
| 8 | 4 | h2m_head | host | Next write index into h2m |
| 12 | 4 | h2m_tail | MCU | Next read index |
| 16 | 4 | m2h_head | MCU | |
| 20 | 4 | m2h_tail | host | |
| 24 | 4 | host_ctl | host | Bit 0 BOOTED: the "CBU" is powered and booted |
| 28 | 4 | mcu_ctl | MCU | Bit 0 CBU_ON: the MCU would have PB05 low. Bit 1 FAST: informational |
| 32 | 1536 | h2m[] | host | Host → MCU bytes (CBU → MCU) |
| 1568 | 1536 | m2h[] | MCU | MCU → host bytes |

- **Total:** 3104 B. It replaces the 2 × 1.1 KB UART buffers in bench builds, so it fits the ≈ 10 KB free (design §4.5).
- **Ring capacity:** 1535 bytes, more than the largest frame (1032).

### 8.2 Ring rules

- **Indices** stay in `[0, 1536)`. A ring is empty when `head == tail` and full when `head + 1 ≡ tail` (mod 1536).
- **Single producer, single consumer.** Each index has exactly one writer, given in the table above. Nobody ever writes the other side's index.
- **Producer:** writes the data bytes, then a barrier, then **one aligned 32-bit write** of the new head.
- **Consumer:** reads the head, then a barrier, then the data, then a barrier, then one 32-bit write of the new tail.
- **Memory ordering:**
  - **Host side:** SWD accesses go through the AHB-AP in the order issued. Each aligned 32-bit word access is single-copy atomic with respect to the core. The host must make sure the data writes have completed before it issues the head write (pyOCD: `target.flush()` between them). Data may be written with byte or block transfers. Indices may not.
  - **MCU side:** the Cortex-M0+ has no cache and does not reorder normal memory accesses, but the code still uses `volatile` and `dmb` (`LINK_MB()`), so the compiler cannot reorder either.
- **Corrupt index:** the MCU treats an index ≥ 1536 as "empty" and does not touch it. The host aborts.

### 8.3 Power and boot handshake

1. **MCU, at "PB05 low":**
   1. sets `h2m_tail = h2m_head`, discarding stale host bytes;
   2. sets `mcu_ctl.CBU_ON`;
   3. starts the 5 s HELLO timer.

   *(Changed by the implementation:)* mailbox builds never drive PB05, so the real CBU stays unpowered (PB05 high) for the whole bench session.
2. **Host,** when it sees `CBU_ON` rise (polling every ≤ 10 ms):
   1. sets `m2h_tail = m2h_head`, discarding stale MCU bytes;
   2. sets `host_ctl.BOOTED`;
   3. starts the fake CBU, which sends HELLO.

   A host that is late misses the 5 s window: the session ends with NO_HELLO, as with a mute CBU.
3. **MCU at "PB05 high":** clears `CBU_ON` and `FAST`. The host sees this, clears `BOOTED`, and drops its session state, as a real power cut would.
4. **Who consumes and produces when:**
   - The MCU consumes h2m and produces m2h **only while `CBU_ON && BOOTED`**. Otherwise its TX bytes are dropped, like a UART into an unpowered CBU.
   - The host produces only while `BOOTED`.
5. **Transmit done.** "Transmit done" on the MCU (the TC equivalent) means "the bytes are in m2h". On the host it means "h2m_tail has caught up with h2m_head". The response timeouts run from then.
6. **Bench builds use `DEEPSLEEP=0`** so that SWD stays attachable. The MCU polls the ring at least every 1 ms while `CBU_ON`.

### 8.4 Host

The host is `tools/th5link/transport.py` `SwdMailboxTransport`:
- pyOCD, target `hc32l170faua`, pack `~/.cache/hc32/HC32L17X.pack`;
- `connect_mode="attach"` and `resume_on_disconnect`: it never halts, resets or touches flash.

```
uv run --with pyocd python -m tools.th5link.fakecbu swd --elf firmware/build/nodeep/th5.elf --slot ha/out/frame-C.slot
```

**Polling cost** *(measured)*: with the Raspberry Pi Debug Probe, a full session that pushes a 25 KB frame takes ≈ 1.25 s from HELLO to the BYE ACK (MCU session time 1285 ms), or ≈ 1.35 s (1423 ms) with CR.WAIT = 1. A HAVE session takes ≈ 0.3 s.

*(Added by the implementation:)*
- **SWD WAIT.** While the flash erases or programs, and while the clock is stepped, the bus stalls and SWD accesses can get WAIT ACKs. `transport.py` retries every access (20 tries, 5 ms apart). A full session saw 31 retries. A HAVE session saw none.
- **Starting a session.** Mailbox builds never start sessions on their own (`LINK_AUTO` = 0): no boot session and no sync timer. Otherwise a session with no host would end in NO_HELLO and backoff. A session starts from the top button, or when the host writes `0xB5E55000 | trigger` to the RAM word `link_bench_req` (`fakecbu swd --trigger N`). The MCU clears the word and runs the session.
- `bench.c` adds flash and clock self-tests, run the same way through `bench_cmd` (notes-link.md).

```
uv run --with pyocd python -m tools.th5link.fakecbu swd --elf firmware/build/nodeep-mbox/th5.elf --trigger 5 --slot ha/out/frame-C.slot
```

---

## 9. Decisions

These are made here; the design left them open or said otherwise.

| # | Decision | Why |
|---|---|---|
| D1 | CRC-16/CCITT-FALSE over `ver…payload`, not the `A5`; CRC sent LE | The SOF is constant. LE matches every other field |
| D2 | Each side numbers its own requests. Responses echo the seq. Duplicates are detected by `(seq, cmd, crc16)`. HELLO is never deduplicated | A CBU reboot restarts its counter at 0. HELLO is idempotent, so it must always run (it resets the transfer and the baud) |
| D3 | The MCU originates only FORGET (and optionally PING), only in LINKED. On a collision the CBU drops the MCU's request | Design §7 lists FORGET as M→C. This keeps "one outstanding across both directions" deterministic |
| D4 | Baud rules R1a/R1b and the 2/10/50 ms constants. The probe attempt is not counted as a retry. The fallback is sticky for the session, across CBU reboots | The design rule alone ("no valid frame within 100 ms") deadlocks if only the probe ACK is lost (2.3) |
| D5 | HELLO_RSP carries the `baud` in force. HELLO is always at 115200 | The fallback is announced explicitly; a rebooted CBU is always heard (R1b brings the MCU down) |
| D6 | **Temperature in 0.01 °C (i16), RH in 0.01 %RH (u16)**, always °C on the wire | The SHT40's native resolution, still 2 bytes. HA rounds for display and converts to °F from the fine value, so there is no double rounding against the panel's 0.1 °C. The offsets use the same unit |
| D7 | **CONFIG is a TLV delta.** settings_crc is the CRC-32 of a canonical TLV | Forward compatible both ways: an old MCU skips new tags, and a new MCU keeps its stored values for tags an old CBU does not send. The canonical CRC lets HA compare without knowing the MCU's flash format |
| D8 | IMG_BEGIN carries crc32 and the frame flags. The MCU writes the header at BEGIN and the commit word at END. IMG_END repeats frame_id. 1020 B chunks, strictly sequential offsets, no resume, a late repeat is ACKed without a write | It matches the self-describing slots (design §4.4). Word-aligned writes. A 1024 B payload exactly |
| D9 | HOLD only raises the deadline, and the total is capped at t0 + 900 s | A buggy CBU cannot keep itself powered forever |
| D10 | NET failure → 2 s grace for BYE | The CBU must sync flash before the power cut [emb M8] |
| D11 | NO_HELLO retries at +60 s and +300 s, then 24 h. The backoff exponent is the consecutive-failure count | The design said "retry twice", and the spacing was unspecified |
| D12 | BYE carries a reason. FORGOT counts as OK | The MCU can classify the result without NET |
| D13 | HELLO_RSP adds `last_session_ms`, `last_result`, `fail_count`, `rx_max`, `fw_version` | Design §8 wants CBU-on time in HA. Only the MCU knows the true on-time, including shutdown, and it reports the previous session's |
| D14 | Mailbox rings of 1536 B with wrapped indices. The MCU still offers 500 k on the mailbox | It fits the RAM budget. It exercises the same probe code as the UART |
| D15 | frame_id 0 = "no frame" | One sentinel instead of a separate flag on the CBU side. FRAME_VALID mirrors it |
| D16 | R1a: **any** timeout at 500 k reverts, even a single one | Simple and deterministic, and it keeps one spare retry. The cost is that one lost frame drops the session to 115200 (+≈ 1.7 s for a 25 KB frame). Losses should be rare, because flash never stalls the RX (3.5) |
| D17 | Tuya detection needs a complete, checksum-valid frame | A bare `55 AA` can appear in BK boot noise. Stock's first frame `55 AA 00 01 00 00 00` is checksum-valid |

---

## 10. Open questions for hardware verification

1. **HC32 UART0 dividers.** *Partly resolved by the implementation.* The clock question is decided: 24 MHz flash timings, no step-down (§2.3). The measured turnarounds are IMG_DATA ≤ 20 ms and IMG_END ≈ 22 ms. **Still open:** confirm on a real UART that 115200 (÷16, SCNT 13) and 500000 (÷8, SCNT 6) work at the measured 24.17 MHz. That clock is +0.7 %, so 115200 would be off by +0.9 % and 500000 by +0.7 %.
2. **BK7231N runtime baud change.** *(CBU note: there is no `load_settings()`; `th5_link` uses `SerialClass::configure()`, §6.)*
   - Does `configure()` change the divider without a glitch, within the 2 ms gap?
   - Is 500000 exact (26 MHz ÷ 52)?
   - Does `-DLT_SERIAL_BUFFER_SIZE=4096` reach the core build [ha M7]?
3. **ESPHome loop latency.** Is the CBU's 98 ms probe timeout and 200 ms response timeout met with Wi-Fi and Noise traffic? Measure the HELLO → PING gap on a scope.
4. **BK boot output.** What rate and how long, and does it ever contain a checksum-valid Tuya-looking frame? (That would be a false TUYA.) Capture 20 boots with `tools/uart_sniff.py`.
5. **Pin parking.** Check that PB06/PB07 driven low (stock) do not back-power the CBU, or idle high before PB05 low. Check that the stray `00` seen at stock power-up is harmless to the parser. It is: it is discarded.
6. **Flash timing inside the budget** *(measured on USB power: ≤ 20 ms, so the remaining question is only the low-battery case)*: lazy erase plus 1020 B programming, with the WDT fed, under 150 ms at the lowest battery voltage. The MCU's IMG_END CRC over flash must match HA's zlib CRC (frame.c `frame_crc32`).
7. **SWD mailbox** *(verified: attach without halting works, the access latency is in §8.4, and erases only cause WAIT retries)*:
   - pyOCD attach without halting on this target;
   - the real per-access latency (it sets the frame push time);
   - that SWD traffic during flash program/erase does not disturb it (the AHB-AP accesses only SRAM).
8. **The FORGET semantics** on ESPHome: which preferences exist to clear when Wi-Fi is compiled in. Possibly only `fast_connect`'s saved BSSID and channel, plus the API state.
9. **PB05 inrush.** Check whether CBU power-on browns the MCU out (reset cause BOR in HELLO_RSP). If so, soft-start PB05 [emb m12].
10. **TC timing.** Check that the HC32's UART TC flag really marks the end of the stop bit, so the MCU's switch is never early.
