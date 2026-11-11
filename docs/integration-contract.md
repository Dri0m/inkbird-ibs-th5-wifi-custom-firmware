# CBU ↔ Home Assistant: session and integration contract

> Reference material, copied from the project's working notes. Links and section
> references to `design.md`, `evidence/…`, `firmware/notes-*.md` and `power-plan.md`
> point at notes that are not published in this repo.

How the ESPHome component `th5_link` (on the Wi-Fi module) runs one sync, and what it
expects from the `th5_display` integration. The MCU ↔ CBU side is in [protocol.md](protocol.md).

## Session, as implemented

This follows [protocol.md](protocol.md) §6. The source is `th5_engine.cpp`.

1. **Boot.** `setup()` reads the learned AP, increments a persisted boot counter and syncs preferences (before any UART traffic), then sends HELLO at 115200 and resends it until the MCU answers.
2. **Readings.** The HELLO_RSP fields are published as sensors. The last one published is `session` (format in the integration contract below). A valid HELLO_RSP also marks the boot good for `safe_mode`, so sessions the MCU ends itself still count as good boots.
3. **Baud.**
   - `fast_baud: true`: the CBU switches to 500 k, sends the probe PING, and applies R1a/R1b.
   - `fast_baud: false` (the default): the CBU sets HELLO flags bit 0 (`SLOW_ONLY`, 0x01), so the MCU offers 115200 and there is no probe. With an older MCU that ignores the flag, the CBU stays at 115200 and silent for 110 ms, so the probe window lapses and the MCU reverts by itself.
4. **Progress.** NET `WIFI_CONNECTING`, then `API_UP` once an API client has subscribed to states. On failure: NET `FAIL_WIFI` or `FAIL_API`, then BYE `NET_FAIL`. Defaults: Wi-Fi 10 s from boot, API 10 s after Wi-Fi.
5. **`th5_config(json)`.** TIME, then CONFIG; answered after both are ACKed (an MCU ERR becomes an action failure, e.g. `TIME refused: BAD_VALUE, detail 0`). Then NET `HA_CONFIG`. `maintenance: true` also sends HOLD 900 and from then on `th5_done` does not end the session; a later `th5_config` with `maintenance: false` (or `flags` without the bit) turns that off again, restarts the timers, and `th5_done` then ends the session with BYE `DONE` (during the MCU's HOLD).
6. **`th5_frame(offset, data)`.** Buffers a range of the slot image (contract below). The header is checked as soon as its 32 bytes are in, against the MCU's IMG_BEGIN rules (protocol.md §4.7), and the payload CRC-32 when the last byte arrives. A complete frame is frozen: a write at offset > 0 is refused unless it is byte-identical (a retried chunk). Uploads are refused while the frame is being sent to the MCU.
7. **`th5_firmware(offset, data)`.** Buffers an MCU firmware package (`th5.fwu`, design.md §4.7), as `th5_frame`, up to 50 176 B. Refused at once if HELLO_RSP `boot_ver` is 0. The first call (offset 0) must hold the 32-byte header, which is checked then (magic, load 0x1000, length, `min_boot_ver` ≤ `boot_ver`) and sizes the buffer; the image CRC-32 is checked when the last byte arrives, and a mismatch frees the buffer. RAM: the package replaces the frame buffer (a complete package refuses `th5_frame`; a frame upload drops an unfinished package), it is `malloc`ed only if 20 KB of heap stay free beside it, and freed after the push.
8. **`th5_done()`.** The CBU answers at once, then runs its own shutdown:
   1. `safe_mode` `mark_successful`;
   2. `APIServer::on_shutdown()`, which sends DisconnectRequest; the CBU waits up to `api_disconnect_timeout` (default 500 ms, max 1 s) for the clients to close;
   3. real BDK DEAUTH (`wlan_sta_disconnect()`; ESPHome's `disable()` on BK72xx only fakes a beacon loss, and the AP then rejects a quick rejoin), a blocking 500 ms for transmission, then `wifi.disable()` in the same call (if the Wi-Fi loop ran in between it would re-associate to the pinned AP, and `disable()` then blocked 1.3 s aborting that), and up to 300 ms for the link state to drop;
   4. if a complete firmware package is buffered: FW_BEGIN, then IMG_DATA × n (image offsets; ≤ 50 for the largest image), then FW_END, and no frame (HAVE skips the data). It is skipped if it would end within 2 s of the MCU's 45 s cap (estimated: ~6.3 s for 50 KB at 115200). Otherwise, if the buffered frame's id differs from the MCU's: IMG_BEGIN, then IMG_DATA × 25, then IMG_END;
   5. `global_preferences->sync()`;
   6. BYE DONE.

   After a NET failure or a link failure (urgent), or with no API client, step 2 does not wait: a connected client gets the DisconnectRequest queued, Wi-Fi goes off at once, and BYE follows within about 0.3 s plus the sync, well inside the MCU's 2 s NET-failure grace.
9. **HA never takes over.** No `th5_config` within `ha_timeout` (3 s) of the API connecting, or of HA's last action call, accepted or rejected → the same shutdown without a frame, BYE `NO_HA`. Also if the API drops before `th5_done` and stays down for 3 s.
10. **Soft session deadline.** No `th5_done` within `session_timeout` (default 25 s from boot) → the shutdown runs anyway: a complete buffered frame (or firmware package, if it fits the time budget) is still pushed, then BYE `NO_HA` (HA never finished; the MCU records NET_FAIL). This keeps the session well under the MCU's 45 s cap, which would end as CAP. Not applied in bench, maintenance or OTA.
11. **OTA.** When an OTA upload starts: NET `OTA`, then HOLD 900. The NO_HA, API and soft-deadline timers stop and `th5_done` does not end the session; before the reboot `on_powerdown()` sends the same DEAUTH, and the next HELLO restarts the link (protocol.md §4.1).
12. **FORGET** (from the MCU, after a factory reset): `global_preferences->reset()`, ACK, then shutdown and BYE `FORGOT`. No preference writes follow. A `th5_done` still pending is answered with a failure, as for any shutdown not caused by `th5_done`.
13. **MCU `rx_max` too small.** If HELLO_RSP announces `rx_max` < 64, or any request would exceed it, the session ends with BYE `LINK` and pending actions fail.
14. **Frame and firmware push failures.** `th5_done` answers OK before the push (it runs with the radio off, to save power), so **OK does not mean committed**. A push failure (MCU ERR, link loss) is stored in a preference and published in the next session's `last_result` as `<mcu result>; frame push failed: <reason>` (at once as well, in bench or maintenance). The confirmation is the next session's `frame_id` in `session`. A firmware push is reported the same way (`firmware push failed: <reason>`); the confirmation is the next session's `mcu_fw`.

## Integration contract (Home Assistant side)

ESPHome registers four actions, which HA names `esphome.<node>_<action>`; for `name: th5` they are `esphome.th5_th5_config`, `esphome.th5_th5_frame`, `esphome.th5_th5_firmware` and `esphome.th5_th5_done`. Each must be called with a response (`return_response`/`supports_response: status`): the response is only **success + an error message**, no data. Unanswered calls time out on the device after 30 s.

| Action | Args | Answered |
|---|---|---|
| `th5_config` | `json`: string, see below | after the MCU ACKed TIME and CONFIG |
| `th5_frame` | `offset`: int, `data`: string (base64) | when the bytes are buffered (and checked, for the header and the last chunk) |
| `th5_firmware` | `offset`: int, `data`: string (base64) | as `th5_frame` (header checked on the first call, CRC on the last) |
| `th5_done` | none | at once (normal), or after the push (bench, maintenance) |

**Trigger.** The `session` text sensor is `nonce;units;frame_id;settings_crc;boot;mcu_fw;boot_ver`:
`nonce` random u32 (8 hex), `units` `C`/`F`, `frame_id` the MCU's committed frame (16 hex, 0 = none), `settings_crc` (8 hex), `boot` the CBU's persisted boot counter (8 hex), `mcu_fw` HELLO_RSP `fw_version` (8 hex), `boot_ver` (2 hex, 0 = no bootloader). Parse by position and ignore extra fields. It is published last, after all other HELLO_RSP sensors; **a change in its value is HA's trigger** to run the session.

**Order and timing.**
1. The `session` change arrives (the API is up). HA must call `th5_config` within 3 s, and gets another 3 s after each call it makes (a rejected call counts as activity, so HA can fix and retry).
2. `th5_config` first; it carries `utc`, so the clock is set even if the rest fails.
3. If the MCU's `frame_id` differs from HA's current frame: `th5_frame` chunks, offset 0 first, strictly contiguous. Recommended chunk: **4096 raw bytes** (5 464 base64 characters); the API message limit is 32 768 bytes. A 2 bpp frame is 25 088 bytes = 7 calls. Send each chunk after the previous one is answered.
4. `th5_done`. Everything must be done within `session_timeout` (25 s from CBU boot, about 20 s after the trigger on a normal Wi-Fi join); after that the CBU closes by itself.

**`th5_done` semantics.** Normal: OK means the CBU has started its shutdown and will push the buffered frame; it is not proof of commit (see 13 above). In bench or maintenance the frame is pushed with the radio on, and the answer comes after IMG_END (OK = committed).

**Maintenance and OTA.** `maintenance: true` keeps the CBU up (HOLD 900, at most 900 s after the MCU's session start) for OTA or debugging; send `maintenance: false` and then `th5_done` to end it early. Starting an OTA upload holds the session by itself. The four booleans `badge`, `maintenance`, `monochrome` and `single_reset` share one `flags` byte: send all four (or `flags`) whenever changing it.

`th5_config` JSON. Absent settings keep the MCU's stored value, and unknown keys are ignored:

```json
{"utc": 1793520000, "offset_min": 60, "next_change_utc": 1806195600, "next_offset_min": 120,
 "mode": "forecast", "sync_min": 60, "clock_min": 1, "clock_24h": true, "full_n": 30,
 "t_off": 0, "rh_off": 0, "badge": false, "maintenance": false, "monochrome": false, "single_reset": false, "history_ack": false}
```

- `utc` is required.
- `mode` is `0`/`1` or `"picture"`/`"forecast"`.
- `t_off` and `rh_off` are in 0.01 units.
- `flags` (an integer) may be given instead of `badge`, `maintenance`, `monochrome` and `single_reset`. They share one byte, so provide all four booleans whenever changing it. Bits: badge=1, maintenance=2, monochrome band=4, single-reset grey experiment=8. Greyscale remains default.
- `history_ack` is a strict boolean delivery receipt, separate from settings flags: set it only after importing this session’s History records successfully. It defaults false. A successful TIME+CONFIG arms the optional BYE DONE flags byte (bit 0). Failure endings never acknowledge history.

## History tail

The `History` text sensor publishes HELLO_RSP's validated history tail **before** `session`, as `nonce8/boot8;base64`: count u8, flags u8 (0), then up to eight 17-byte hourly records (UTC hour start u32; temperature mean/min/max i16 and humidity mean/min/max u16, in 0.01 units; samples u8). The token must match this session's nonce and the CBU boot counter (the nonce can repeat on cold boots). Bytes after the records are ignored, so a later MCU can extend HELLO_RSP. An old MCU, unknown flags or a malformed tail publish an empty string and never earn a history ACK; the MCU keeps its records until BYE DONE carries the receipt.
