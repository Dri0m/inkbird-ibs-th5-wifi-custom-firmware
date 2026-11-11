#include "th5_engine.h"

#include <cinttypes>
#include <cstdarg>
#include <cstdio>
#include <cstring>

namespace th5link {

// --- helpers -------------------------------------------------------------------------

uint32_t crc32(const uint8_t *p, size_t n, uint32_t crc) {
  // zlib CRC-32 (reflected 0xEDB88320), a nibble at a time.
  static const uint32_t T[16] = {0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4,
                                 0x4DB26158, 0x5005713C, 0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C,
                                 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C};
  crc = ~crc;
  while (n--) {
    crc ^= *p++;
    crc = (crc >> 4) ^ T[crc & 15u];
    crc = (crc >> 4) ^ T[crc & 15u];
  }
  return ~crc;
}

static int b64_value(char c) {
  if (c >= 'A' && c <= 'Z')
    return c - 'A';
  if (c >= 'a' && c <= 'z')
    return c - 'a' + 26;
  if (c >= '0' && c <= '9')
    return c - '0' + 52;
  if (c == '+')
    return 62;
  if (c == '/')
    return 63;
  return -1;
}

bool base64_decode(const char *in, size_t len, uint8_t *out, size_t cap, size_t &out_len) {
  out_len = 0;
  while (len > 0 && in[len - 1] == '=')
    len--;
  if (len % 4 == 1)
    return false;
  uint32_t acc = 0;
  int bits = 0;
  for (size_t i = 0; i < len; i++) {
    int v = b64_value(in[i]);
    if (v < 0)
      return false;
    acc = (acc << 6) | (uint32_t) v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (out_len >= cap)
        return false;
      out[out_len++] = (uint8_t) (acc >> bits);
    }
  }
  return true;
}

std::string session_string(uint32_t nonce, const HelloInfo &h, uint32_t boot) {
  // No %llx: newlib-nano's printf on the BK7231N has no 64-bit conversions.
  char buf[72];
  snprintf(buf, sizeof(buf),
           "%08" PRIx32 ";%c;%08" PRIx32 "%08" PRIx32 ";%08" PRIx32 ";%08" PRIx32 ";%08" PRIx32 ";%02x", nonce,
           h.units == 1 ? 'F' : 'C', (uint32_t) (h.frame_id >> 32), (uint32_t) h.frame_id, h.settings_crc, boot,
           h.fw_version, h.boot_ver);
  return buf;
}

const char *err_name(uint8_t code) {
  static const char *const N[] = {"?",     "UNKNOWN_CMD", "BAD_LEN", "BAD_VALUE", "VERSION", "STATE",
                                  "OFFSET", "TOO_BIG",     "CRC",     "FLASH",     "BUSY"};
  return code < sizeof(N) / sizeof(N[0]) ? N[code] : "?";
}

const char *cmd_name(uint8_t cmd) {
  switch (cmd) {
    case LINK_CMD_HELLO:
      return "HELLO";
    case LINK_CMD_PING:
      return "PING";
    case LINK_CMD_NET:
      return "NET";
    case LINK_CMD_TIME:
      return "TIME";
    case LINK_CMD_CONFIG:
      return "CONFIG";
    case LINK_CMD_IMG_BEGIN:
      return "IMG_BEGIN";
    case LINK_CMD_IMG_DATA:
      return "IMG_DATA";
    case LINK_CMD_IMG_END:
      return "IMG_END";
    case LINK_CMD_HOLD:
      return "HOLD";
    case LINK_CMD_BYE:
      return "BYE";
    case LINK_CMD_FW_BEGIN:
      return "FW_BEGIN";
    case LINK_CMD_FW_END:
      return "FW_END";
    case LINK_CMD_FORGET:
      return "FORGET";
    case LINK_CMD_ACK:
      return "ACK";
    case LINK_CMD_HELLO_RSP:
      return "HELLO_RSP";
    case LINK_CMD_ERR:
      return "ERR";
    default:
      return "?";
  }
}

static std::string describe_err(const RxFrame &f) {
  char buf[64];
  if (f.len < LINK_ERR_LEN) {
    snprintf(buf, sizeof(buf), "%s refused (short ERR)", cmd_name(f.payload[0]));
  } else {
    snprintf(buf, sizeof(buf), "%s refused: %s, detail %" PRIu32, cmd_name(f.payload[LINK_ERR_O_CMD]),
             err_name(f.payload[LINK_ERR_O_CODE]), link_get32(f.payload + LINK_ERR_O_DETAIL));
  }
  return buf;
}

bool HelloInfo::parse(const uint8_t *p, size_t n, HelloInfo &h) {
  if (n < LINK_HELLO_RSP_LEN)
    return false;
  h.proto = p[LINK_HR_O_PROTO];
  h.reset_cause = p[LINK_HR_O_RESET_CAUSE];
  h.baud = link_get32(p + LINK_HR_O_BAUD);
  h.rx_max = link_get16(p + LINK_HR_O_RX_MAX);
  h.fw_version = link_get32(p + LINK_HR_O_FW);
  h.frame_id = link_get64(p + LINK_HR_O_FRAME_ID);
  h.settings_crc = link_get32(p + LINK_HR_O_SETTINGS_CRC);
  h.units = p[LINK_HR_O_UNITS];
  h.trigger = p[LINK_HR_O_TRIGGER];
  h.flags = link_get16(p + LINK_HR_O_FLAGS);
  h.t_centi = (int16_t) link_get16(p + LINK_HR_O_T);
  h.rh_centi = link_get16(p + LINK_HR_O_RH);
  h.batt_mv = link_get16(p + LINK_HR_O_BATT_MV);
  h.batt_pct = p[LINK_HR_O_BATT_PCT];
  h.charge = p[LINK_HR_O_CHARGE];
  h.last_session_ms = link_get32(p + LINK_HR_O_LAST_MS);
  h.last_result = p[LINK_HR_O_LAST_RESULT];
  h.fail_count = p[LINK_HR_O_FAIL_COUNT];
  h.boot_ver = p[LINK_HR_O_BOOT_VER];
  h.history.clear();
  // Malformed or unknown (flags != 0) history must not break normal readings and
  // never earns a delivery ACK. Old MCUs send the fixed 44-byte response; bytes
  // after the records are ignored, so a later MCU can extend HELLO_RSP.
  if (n >= LINK_HELLO_RSP_LEN + 2) {
    const uint8_t *tail = p + LINK_HELLO_RSP_LEN;
    size_t bytes = 2u + size_t(tail[0]) * 17u;
    if (tail[0] <= 8 && tail[1] == 0 && n >= LINK_HELLO_RSP_LEN + bytes)
      h.history.assign(tail, tail + bytes);
  }
  return true;
}

// CONFIG tag -> wire width and signedness (protocol.md 4.6).
struct TagType {
  uint8_t width;
  bool is_signed;
  const char *name;
};
static const TagType TAG_TYPES[LINK_CFG_TAG_COUNT + 1] = {
    {0, false, "?"},        {1, false, "mode"},    {2, false, "sync_min"}, {1, false, "clock_min"},
    {1, false, "clock_24h"}, {1, false, "full_n"}, {2, true, "t_off"},     {2, true, "rh_off"},
    {1, false, "flags"},
};

size_t ConfigRequest::encode_tlv(uint8_t *out, size_t cap, std::string &err) const {
  size_t n = 0;
  for (uint8_t tag = 1; tag <= LINK_CFG_TAG_COUNT; tag++) {
    if (!this->has[tag])
      continue;
    const TagType &t = TAG_TYPES[tag];
    int32_t v = this->value[tag];
    int32_t lo = t.is_signed ? -(1 << (8 * t.width - 1)) : 0;
    int32_t hi = t.is_signed ? (1 << (8 * t.width - 1)) - 1 : (1 << (8 * t.width)) - 1;
    if (v < lo || v > hi) {
      err = std::string(t.name) + " does not fit its wire type";
      return 0;
    }
    if (n + 2 + t.width > cap) {
      err = "CONFIG too long";
      return 0;
    }
    out[n++] = tag;
    out[n++] = t.width;
    if (t.width == 1) {
      out[n++] = (uint8_t) v;
    } else {
      link_put16(out + n, (uint16_t) v);
      n += 2;
    }
  }
  return n;
}

void ConfigRequest::encode_time(uint32_t utc_now, uint8_t out[LINK_TIME_LEN]) const {
  link_put32(out + LINK_TIME_O_UTC, utc_now);
  link_put16(out + LINK_TIME_O_OFFSET, (uint16_t) (int16_t) this->offset_min);
  link_put32(out + LINK_TIME_O_NEXT_UTC, this->next_change_utc);
  link_put16(out + LINK_TIME_O_NEXT_OFFSET, (uint16_t) (int16_t) this->next_offset_min);
}

// --- Parser (protocol.md 3.4) ------------------------------------------------------------

void Parser::drop_(size_t n) {
  if (n > this->len_)
    n = this->len_;
  memmove(this->buf_, this->buf_ + n, this->len_ - n);
  this->len_ -= n;
  this->discarded_ += n;
}

void Parser::reset() {
  this->discarded_ += this->len_;
  this->len_ = 0;
}

void Parser::push(uint8_t b, uint32_t now) {
  if (this->len_ == sizeof(this->buf_))
    this->drop_(1);  // cannot happen if next() is drained after every push
  this->buf_[this->len_++] = b;
  this->last_byte_ = now;
}

bool Parser::expire(uint32_t now) {
  if (this->len_ == 0 || (int32_t) (now - this->last_byte_) <= (int32_t) LINK_T_INTERBYTE_MS)
    return false;
  this->drop_(1);  // stale partial frame: drop its A5 and rescan
  return true;
}

bool Parser::next(RxFrame &out) {
  while (this->len_ > 0) {
    size_t i = 0;
    while (i < this->len_ && this->buf_[i] != LINK_SOF)
      i++;
    if (i)
      this->drop_(i);
    if (this->len_ < LINK_HDR_BYTES)
      return false;
    uint16_t n = link_get16(this->buf_ + 4);
    if (this->buf_[1] != LINK_VER || n > this->max_payload_) {
      this->drop_(1);
      continue;
    }
    if (this->len_ < LINK_OVERHEAD + n)
      return false;
    if (link_frame_check(this->buf_, LINK_OVERHEAD + n, this->max_payload_) < 0) {
      this->drop_(1);  // CRC: drop only the A5, rescan the rest
      continue;
    }
    out.ver = this->buf_[1];
    out.seq = this->buf_[2];
    out.cmd = this->buf_[3];
    out.len = n;
    memcpy(out.payload, this->buf_ + LINK_HDR_BYTES, n);
    size_t used = LINK_OVERHEAD + n;
    memmove(this->buf_, this->buf_ + used, this->len_ - used);
    this->len_ -= used;
    return true;
  }
  return false;
}

// --- link layer (link.py Endpoint, role "cbu") ----------------------------------------------

void Engine::logf_(int level, const char *fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  this->host_->log(level, buf);
}

uint32_t Engine::wire_ms_(size_t n) const {
  return (uint32_t) ((n * 10000u + this->baud_ - 1) / this->baud_) + 1u;
}

void Engine::emit_(const uint8_t *raw, size_t n, uint32_t now) {
  (void) now;
  this->host_->uart_write(raw, n);
  this->stats.tx_frames++;
}

bool Engine::request_(uint8_t cmd, const uint8_t *payload, uint16_t len, uint32_t now, Done done, bool probe) {
  if (this->out_.active) {
    this->logf_(1, "stop-and-wait violated: %s while %s outstanding", cmd_name(cmd), cmd_name(this->out_.cmd));
    return false;
  }
  uint16_t peer_max = this->have_hello_ ? this->hello_.rx_max : (uint16_t) LINK_MAX_TO_MCU;
  if (len > peer_max || len > LINK_MAX_TO_MCU) {
    this->logf_(1, "%s payload %u exceeds the MCU's rx_max %u", cmd_name(cmd), len, peer_max);
    this->oversize_(cmd, now);
    return false;
  }
  auto &o = this->out_;
  o.active = true;
  o.seq = this->tx_seq_++;
  o.cmd = cmd;
  if (len)
    memcpy(o.raw + LINK_HDR_BYTES, payload, len);
  o.raw_len = (uint16_t) link_frame_seal(o.raw, o.seq, cmd, len);
  // IMG_BEGIN/IMG_END and FW_BEGIN/FW_END erase or CRC flash: the long timeout.
  bool long_ack = cmd == LINK_CMD_IMG_BEGIN || cmd == LINK_CMD_IMG_END || cmd == LINK_CMD_FW_BEGIN ||
                  cmd == LINK_CMD_FW_END;
  o.timeout = probe ? (LINK_T_PROBE_MS - LINK_T_PROBE_GAP_MS) : (long_ack ? LINK_T_ACK_LONG_MS : LINK_T_ACK_MS);
  o.probe = probe;
  o.attempts = 1;
  o.busy_left = BUSY_RETRIES;
  o.resend_pending = false;
  o.done = std::move(done);
  this->busy_pending_ = false;
  this->emit_(o.raw, o.raw_len, now);
  o.deadline = now + this->wire_ms_(o.raw_len) + o.timeout;
  o.deadline_set = true;
  return true;
}

void Engine::set_baud_(uint32_t baud, uint32_t now) {
  if (baud == this->baud_)
    return;
  this->baud_ = baud;
  this->host_->uart_set_baud(baud);
  this->parser_.reset();
  this->fast_ = baud == LINK_BAUD_FAST;
  this->fast_since_ = now;
}

void Engine::revert_(uint32_t now) {
  if (this->baud_ != LINK_BAUD_SLOW) {
    this->stats.reverts++;
    this->logf_(2, "reverting to 115200 for the rest of the session");
  }
  this->sticky_slow_ = true;
  this->set_baud_(LINK_BAUD_SLOW, now);
}

void Engine::feed(const uint8_t *data, size_t len, uint32_t now) {
  uint32_t before = this->parser_.discarded();
  // Inter-byte timeout only on a call that brought no bytes: after a loop stall
  // the rest of a frame is already in the UART ring and arrives here with a late
  // loop timestamp; expiring first would drop a valid frame.
  if (len == 0 && this->parser_.expire(now))
    this->drain_(now);
  for (size_t i = 0; i < len; i++) {
    this->parser_.push(data[i], now);
    this->drain_(now);
  }
  // R1b: garbage, and no valid frame for >= 50 ms (since the switch if none yet).
  if (this->parser_.discarded() > before && this->fast_) {
    uint32_t ref = this->have_valid_rx_ ? this->last_valid_rx_ : this->fast_since_;
    if ((int32_t) (now - ref) >= (int32_t) LINK_T_GARBAGE_MS)
      this->revert_(now);
  }
  this->pump_(now);
}

void Engine::drain_(uint32_t now) {
  RxFrame f;
  while (this->parser_.next(f))
    this->on_frame_(f, now);
}

void Engine::on_frame_(const RxFrame &f, uint32_t now) {
  this->have_valid_rx_ = true;
  this->last_valid_rx_ = now;
  this->stats.rx_frames++;
  if (f.cmd & LINK_CMD_RESPONSE)
    this->on_response_(f, now);
  else
    this->on_request_(f, now);
}

void Engine::on_response_(const RxFrame &f, uint32_t now) {
  auto &o = this->out_;
  if (!o.active || f.seq != o.seq)
    return;  // stale duplicate
  if (f.cmd == LINK_CMD_ACK || f.cmd == LINK_CMD_ERR) {
    if (f.len < 1 || f.payload[0] != o.cmd)
      return;
  } else if (f.cmd == LINK_CMD_HELLO_RSP) {
    if (o.cmd != LINK_CMD_HELLO)
      return;
  } else {
    return;
  }
  if (f.cmd == LINK_CMD_ERR && f.len >= 2 && f.payload[LINK_ERR_O_CODE] == LINK_E_BUSY && o.busy_left > 0) {
    // 3.6: BUSY may be retried after >= 100 ms as a new request with a new seq.
    o.busy_left--;
    o.deadline_set = false;
    o.resend_pending = false;
    this->busy_pending_ = true;
    this->busy_at_ = now + BUSY_RETRY_MS;
    return;
  }
  Done d = std::move(o.done);
  o.done = nullptr;
  o.active = false;
  if (d)
    d(&f, now);
}

void Engine::on_request_(const RxFrame &f, uint32_t now) {
  if (this->out_.active) {
    this->stats.collisions++;  // 3.5: the CBU's own request wins; drop the MCU's
    return;
  }
  uint8_t hdr[5] = {f.ver, f.seq, f.cmd, (uint8_t) f.len, (uint8_t) (f.len >> 8)};
  uint16_t crc = link_crc16(link_crc16(0xFFFFu, hdr, 5), f.payload, f.len);
  auto &dd = this->dedup_;
  if (f.cmd != LINK_CMD_HELLO && dd.valid && dd.seq == f.seq && dd.cmd == f.cmd && dd.crc == crc) {
    this->stats.dup_requests++;
    this->emit_(dd.rsp, dd.rsp_len, now);  // resend the stored response, do not re-apply
    return;
  }
  uint8_t *p = dd.rsp + LINK_HDR_BYTES;
  uint8_t rcmd;
  uint16_t rlen;
  if (f.cmd == LINK_CMD_PING && f.len <= LINK_PING_MAX) {
    rcmd = LINK_CMD_ACK;
    p[0] = LINK_CMD_PING;
    memcpy(p + 1, f.payload, f.len);
    rlen = (uint16_t) (1 + f.len);
  } else if (f.cmd == LINK_CMD_FORGET) {
    this->logf_(2, "FORGET: clearing saved Wi-Fi/API preferences");
    if (!this->forget_received_)
      this->host_->forget();  // clear + sync flash before the ACK (4.12)
    this->forget_received_ = true;
    rcmd = LINK_CMD_ACK;
    p[0] = LINK_CMD_FORGET;
    rlen = 1;
  } else {
    rcmd = LINK_CMD_ERR;
    p[LINK_ERR_O_CMD] = f.cmd;
    p[LINK_ERR_O_CODE] = f.cmd == LINK_CMD_PING ? LINK_E_BAD_LEN : LINK_E_UNKNOWN_CMD;
    link_put32(p + LINK_ERR_O_DETAIL, f.cmd == LINK_CMD_PING ? LINK_PING_MAX : 0);
    rlen = LINK_ERR_LEN;
  }
  dd.rsp_len = (uint16_t) link_frame_seal(dd.rsp, f.seq, rcmd, rlen);
  dd.valid = true;
  dd.seq = f.seq;
  dd.cmd = f.cmd;
  dd.crc = crc;
  this->emit_(dd.rsp, dd.rsp_len, now);
}

void Engine::poll_link_(uint32_t now) {
  auto &o = this->out_;
  if (!o.active)
    return;
  if (this->busy_pending_) {
    if (!due_(now, this->busy_at_))
      return;
    this->busy_pending_ = false;
    o.seq = this->tx_seq_++;
    o.raw_len = (uint16_t) link_frame_seal(o.raw, o.seq, o.cmd, link_get16(o.raw + 4));
    o.attempts = 1;
    this->emit_(o.raw, o.raw_len, now);
    o.deadline = now + this->wire_ms_(o.raw_len) + o.timeout;
    o.deadline_set = true;
    return;
  }
  if (o.resend_pending) {
    if (!due_(now, o.resend_at))
      return;
    o.resend_pending = false;
    this->emit_(o.raw, o.raw_len, now);
    o.deadline = now + this->wire_ms_(o.raw_len) + o.timeout;
    o.deadline_set = true;
    return;
  }
  if (!o.deadline_set || !due_(now, o.deadline))
    return;
  if (o.probe) {
    // The probe attempt failed: revert, settle, resend the same PING at 115200
    // with the normal timeout. Not counted as a retry (2.3 step 5).
    this->revert_(now);
    o.probe = false;
    o.timeout = LINK_T_ACK_MS;
    o.deadline_set = false;
    o.resend_pending = true;
    o.resend_at = now + LINK_T_SETTLE_MS;
    return;
  }
  if (o.attempts > LINK_RETRIES) {
    uint8_t cmd = o.cmd;
    Done d = std::move(o.done);
    o.done = nullptr;
    o.active = false;
    this->logf_(2, "%s: no response after %u attempts", cmd_name(cmd), (unsigned) (LINK_RETRIES + 1));
    if (cmd == LINK_CMD_HELLO) {
      if (d)
        d(nullptr, now);
    } else {
      this->link_failed_(cmd, now);
    }
    return;
  }
  if (this->fast_)
    this->revert_(now);  // R1a
  o.attempts++;
  this->stats.retries++;
  this->emit_(o.raw, o.raw_len, now);
  o.deadline = now + this->wire_ms_(o.raw_len) + o.timeout;
  o.deadline_set = true;
}

void Engine::tick(uint32_t now) {
  this->feed(nullptr, 0, now);  // inter-byte timeout, R1b
  this->poll_link_(now);
  this->pump_(now);
}

bool Engine::next_deadline(uint32_t now, uint32_t &when) const {
  bool any = false;
  auto take = [&](uint32_t t) {
    if (!any || (int32_t) (t - when) < 0)
      when = t;
    any = true;
  };
  const auto &o = this->out_;
  if (o.active) {
    if (this->busy_pending_)
      take(this->busy_at_);
    else if (o.resend_pending)
      take(o.resend_at);
    else if (o.deadline_set)
      take(o.deadline);
  }
  if (this->parser_.partial())
    take(this->parser_.last_byte() + LINK_T_INTERBYTE_MS + 1);
  if ((this->phase_ == PH_PROBE && !this->probe_sent_) || this->phase_ == PH_SLOW_WAIT)
    take(this->wait_until_);
  if (this->phase_ == PH_LINKED) {
    if (this->forget_flag_)
      take(this->forget_deadline_);
    if (!this->wifi_up_)
      take(this->boot_at_ + this->opts_.wifi_timeout_ms);
    if (this->wifi_up_ && !this->api_ever_up_ && !this->holding_())
      take(this->wifi_up_at_ + this->opts_.api_timeout_ms);
    if (this->api_ever_up_ && !this->config_seen_ && !this->holding_())
      take(this->last_ha_at_ + this->opts_.ha_timeout_ms);
    if (this->opts_.session_timeout_ms && !this->holding_())
      take(this->soft_base_ + this->opts_.session_timeout_ms);
    if (this->api_ever_up_ && !this->api_up_ && !this->done_pending_ && !this->holding_())
      take(this->api_changed_at_ + this->opts_.ha_timeout_ms);
  }
  (void) now;
  return any;
}

// --- session (fakecbu.py _script, protocol.md 6) ---------------------------------------

const char *Engine::phase_name() const {
  static const char *const N[] = {"idle", "hello", "probe", "slow_wait", "linked", "shutdown", "closing", "finished"};
  return N[this->phase_];
}

void Engine::start(uint32_t now) {
  this->boot_at_ = now;
  this->soft_base_ = now;
  this->phase_ = PH_HELLO;
  this->send_hello_(now);
}

void Engine::send_hello_(uint32_t now) {
  uint8_t p[LINK_HELLO_LEN] = {};
  p[LINK_HELLO_O_PROTO_MAX] = LINK_VER;
  // fast_baud off: ask the MCU not to offer 500 k (no probe, no revert). An MCU
  // that ignores the flag and offers 500 k anyway is still handled by PH_SLOW_WAIT.
  if (!this->opts_.fast_baud)
    p[LINK_HELLO_O_FLAGS] |= LINK_HELLO_F_SLOW_ONLY;
  link_put16(p + LINK_HELLO_O_RX_MAX, LINK_MAX_TO_CBU);
  link_put32(p + LINK_HELLO_O_FW, FW_VERSION);
  this->request_(LINK_CMD_HELLO, p, sizeof(p), now,
                 [this](const RxFrame *rsp, uint32_t t) { this->on_hello_rsp_(rsp, t); });
}

void Engine::on_hello_rsp_(const RxFrame *rsp, uint32_t now) {
  if (rsp == nullptr) {
    // Keep sending HELLO (2.2 step 5): the MCU cuts power if it never answers.
    this->send_hello_(now);
    return;
  }
  HelloInfo h;
  if (rsp->cmd != LINK_CMD_HELLO_RSP || !HelloInfo::parse(rsp->payload, rsp->len, h)) {
    if (rsp->cmd == LINK_CMD_ERR)
      this->logf_(1, "%s", describe_err(*rsp).c_str());
    else
      this->logf_(1, "bad HELLO_RSP (%u bytes)", rsp->len);
    this->start_shutdown_(LINK_BYE_LINK, now);
    return;
  }
  if (h.rx_max < MIN_MCU_RX_MAX) {
    // Every later request could fail the size check; end at once at 115200 with
    // BYE LINK (1 byte) instead of looping on refused requests.
    this->logf_(1, "MCU rx_max %u < %u: cannot run a session", h.rx_max, (unsigned) MIN_MCU_RX_MAX);
    this->hello_ = h;
    this->have_hello_ = true;
    this->start_shutdown_(LINK_BYE_LINK, now);
    return;
  }
  this->hello_ = h;
  this->have_hello_ = true;
  this->bench_ = h.flags & LINK_F_BENCH;
  this->forget_flag_ = h.flags & LINK_F_FORGET_PENDING;
  std::string session = session_string(this->host_->random32(), h, this->host_->boot_count());
  this->host_->on_hello(h, session);
  if (h.baud == LINK_BAUD_FAST) {
    if (this->opts_.fast_baud) {
      this->set_baud_(LINK_BAUD_FAST, now);  // at once; then >= 2 ms, then the probe
      this->phase_ = PH_PROBE;
      this->probe_sent_ = false;
      this->wait_until_ = now + LINK_T_PROBE_GAP_MS;
    } else {
      // Stay at 115200 and say nothing until the MCU's 100 ms probe window has
      // lapsed; it then reverts to 115200 for the session (2.3 step 4).
      this->sticky_slow_ = true;
      this->phase_ = PH_SLOW_WAIT;
      this->wait_until_ = now + LINK_T_PROBE_MS + LINK_T_SETTLE_MS;
    }
  } else {
    this->enter_linked_(now);
  }
}

void Engine::enter_linked_(uint32_t now) {
  this->phase_ = PH_LINKED;
  if (this->forget_flag_)
    this->forget_deadline_ = now + FORGET_WAIT_MS;  // the MCU sends FORGET right after the probe
}

void Engine::pump_(uint32_t now) {
  if (this->out_.active)
    return;
  switch (this->phase_) {
    case PH_PROBE:
      if (!this->probe_sent_ && due_(now, this->wait_until_)) {
        this->probe_sent_ = true;
        this->request_(
            LINK_CMD_PING, PROBE_PAYLOAD, sizeof(PROBE_PAYLOAD), now,
            [this](const RxFrame *rsp, uint32_t t) {
              if (rsp->cmd != LINK_CMD_ACK)
                this->logf_(2, "probe PING: %s", describe_err(*rsp).c_str());
              this->enter_linked_(t);
              this->pump_(t);
            },
            true);
      }
      break;
    case PH_SLOW_WAIT:
      if (due_(now, this->wait_until_)) {
        this->enter_linked_(now);
        this->linked_step_(now);
      }
      break;
    case PH_LINKED:
      this->linked_step_(now);
      break;
    case PH_SHUTDOWN:
      this->shutdown_step_(now);
      break;
    default:
      break;
  }
}

void Engine::send_net_(uint8_t state, uint8_t detail, int8_t rssi, uint32_t now, std::function<void(uint32_t)> then) {
  uint8_t p[LINK_NET_LEN];
  p[LINK_NET_O_STATE] = state;
  p[LINK_NET_O_DETAIL] = detail;
  p[LINK_NET_O_RSSI] = (uint8_t) rssi;
  this->request_(LINK_CMD_NET, p, sizeof(p), now, [this, then](const RxFrame *rsp, uint32_t t) {
    if (rsp->cmd != LINK_CMD_ACK)
      this->logf_(2, "%s", describe_err(*rsp).c_str());
    if (then)
      then(t);
    else
      this->pump_(t);
  });
}

void Engine::linked_step_(uint32_t now) {
  if (this->out_.active)
    return;
  if (this->forget_received_) {
    this->start_shutdown_(LINK_BYE_FORGOT, now);
    return;
  }
  if (this->forget_flag_) {
    if (due_(now, this->forget_deadline_))
      this->start_shutdown_(LINK_BYE_FORGOT, now);
    return;  // nothing else this session
  }
  if (!this->net_connecting_sent_) {
    this->net_connecting_sent_ = true;
    this->send_net_(LINK_NET_WIFI_CONNECTING, 0, 0, now);
    return;
  }
  if (this->net_fail_state_ && !this->net_fail_sent_) {
    this->net_fail_sent_ = true;
    this->logf_(2, "network failure 0x%02x (detail %u)", this->net_fail_state_, this->net_fail_detail_);
    this->send_net_(this->net_fail_state_, this->net_fail_detail_, this->rssi_, now,
                    [this](uint32_t t) { this->start_shutdown_(LINK_BYE_NET_FAIL, t); });
    return;
  }
  if (this->ota_started_ && !this->ota_net_sent_) {
    this->ota_net_sent_ = true;
    this->send_net_(LINK_NET_OTA, 0, this->rssi_, now);
    return;
  }
  if (this->wifi_up_ && !this->net_wifi_up_sent_) {
    this->net_wifi_up_sent_ = true;
    this->send_net_(LINK_NET_WIFI_UP, 0, this->rssi_, now);
    return;
  }
  if (this->api_up_ && !this->api_up_sent_) {
    this->api_up_sent_ = true;
    this->send_net_(LINK_NET_API_UP, 0, this->rssi_, now);
    return;
  }
  if (this->config_job_) {
    this->config_step_(now);
    return;
  }
  if (this->config_seen_ && !this->ha_config_net_sent_) {
    this->ha_config_net_sent_ = true;
    this->send_net_(LINK_NET_HA_CONFIG, 0, this->rssi_, now);
    return;
  }
  if (this->hold_pending_) {
    this->hold_pending_ = false;
    uint8_t p[LINK_HOLD_LEN];
    link_put16(p, HOLD_MAINTENANCE_S);
    this->request_(LINK_CMD_HOLD, p, sizeof(p), now, [this](const RxFrame *rsp, uint32_t t) {
      if (rsp->cmd == LINK_CMD_ACK && rsp->len >= 3) {
        this->hold_granted_ = true;
        this->logf_(3, "HOLD granted: %u s", link_get16(rsp->payload + LINK_ACK_O_DATA));
      } else {
        this->logf_(2, "%s", describe_err(*rsp).c_str());
      }
      this->pump_(t);
    });
    return;
  }
  if (this->done_pending_ && !this->done_busy_) {
    if (this->holding_()) {
      // Bench or maintenance: stay connected (4.10). Push a buffered frame (or
      // firmware) now, with the radio on, and answer th5_done once the MCU has
      // committed it.
      if (this->image_wanted_()) {
        this->done_busy_ = true;
        this->done_reply_after_image_ = true;
        this->start_image_();
        this->image_step_(now);
      } else {
        this->done_pending_ = false;
        this->host_->respond(this->done_call_, true, this->bench_ ? "bench mode: staying on" : "maintenance: staying on");
      }
      return;
    }
    this->done_pending_ = false;
    this->host_->respond(this->done_call_, true, "");
    this->start_shutdown_(LINK_BYE_DONE, now);
    return;
  }
  if (this->img_ == IMG_BEGIN || this->img_ == IMG_DATA || this->img_ == IMG_END) {
    this->image_step_(now);
    return;
  }
  // Timers. Failures go out as NET first (the MCU gives 2 s grace for BYE).
  if (!this->wifi_up_ && !this->net_fail_state_ && due_(now, this->boot_at_ + this->opts_.wifi_timeout_ms)) {
    this->net_fail_state_ = LINK_NET_FAIL_WIFI;
    this->linked_step_(now);
    return;
  }
  if (this->holding_())
    return;
  if (this->wifi_up_ && !this->api_ever_up_ && !this->net_fail_state_ &&
      due_(now, this->wifi_up_at_ + this->opts_.api_timeout_ms)) {
    this->net_fail_state_ = LINK_NET_FAIL_API;
    this->linked_step_(now);
    return;
  }
  if (this->api_ever_up_ && !this->config_seen_ && due_(now, this->last_ha_at_ + this->opts_.ha_timeout_ms)) {
    this->logf_(2, "no th5_config within %" PRIu32 " ms of the API connecting", this->opts_.ha_timeout_ms);
    this->start_shutdown_(LINK_BYE_NO_HA, now);  // as fakecbu: BYE NO_HA, no NET failure
    return;
  }
  if (this->api_ever_up_ && !this->api_up_ && due_(now, this->api_changed_at_ + this->opts_.ha_timeout_ms)) {
    this->logf_(2, "API connection lost before th5_done");
    this->start_shutdown_(LINK_BYE_NO_HA, now);
    return;
  }
  if (this->opts_.session_timeout_ms && due_(now, this->soft_base_ + this->opts_.session_timeout_ms)) {
    // HA stalled after (or before) th5_config. End well before the MCU's 45 s cap
    // (CAP is a failure with backoff): push a complete frame if there is one, then
    // BYE NO_HA, since HA never called th5_done (4.11).
    this->logf_(2, "no th5_done within %" PRIu32 " ms of boot: closing", this->opts_.session_timeout_ms);
    this->soft_deadline_hit_ = true;
    this->start_shutdown_(LINK_BYE_NO_HA, now);
    return;
  }
}

void Engine::config_step_(uint32_t now) {
  if (this->config_stage_ == 0) {
    // utc is "at the first byte of this frame" (4.5): add the time since th5_config.
    uint32_t utc = this->config_.utc + (uint32_t) ((now - this->config_at_) / 1000u);
    uint8_t p[LINK_TIME_LEN];
    this->config_.encode_time(utc, p);
    this->request_(LINK_CMD_TIME, p, sizeof(p), now, [this](const RxFrame *rsp, uint32_t t) {
      if (rsp->cmd != LINK_CMD_ACK) {
        this->config_job_ = false;
        this->host_->respond(this->config_call_, false, describe_err(*rsp));
      } else {
        this->config_stage_ = 1;
      }
      this->pump_(t);
    });
    return;
  }
  uint8_t p[CONFIG_TLV_MAX];
  std::string err;
  size_t n = this->config_.encode_tlv(p, sizeof(p), err);  // checked in action_config
  this->request_(LINK_CMD_CONFIG, p, (uint16_t) n, now, [this](const RxFrame *rsp, uint32_t t) {
    this->config_job_ = false;
    if (rsp->cmd != LINK_CMD_ACK) {
      this->host_->respond(this->config_call_, false, describe_err(*rsp));
    } else {
      this->history_ack_ = this->config_.history_ack && !this->hello_.history.empty();
      if (this->config_.maintenance() && !this->maintenance_) {
        this->maintenance_ = true;
        this->hold_pending_ = true;  // 6.4: HOLD 900, and th5_done does not end the session
      } else if (this->config_.has_flags() && !this->config_.maintenance() && this->maintenance_) {
        // maintenance:false: th5_done ends the session again (BYE DONE during the
        // MCU's HOLD), and the timers restart from now.
        this->maintenance_ = false;
        this->soft_base_ = t;
        this->last_ha_at_ = t;
        this->logf_(3, "maintenance off");
      }
      this->host_->respond(this->config_call_, true, "");
    }
    this->pump_(t);
  });
}

void Engine::start_shutdown_(uint8_t reason, uint32_t now) {
  if (this->phase_ >= PH_SHUTDOWN)
    return;
  this->phase_ = PH_SHUTDOWN;
  this->bye_reason_ = reason;
  this->fail_pending_calls_("session closing");
  if (reason != LINK_BYE_DONE && this->done_pending_) {
    // e.g. FORGET or a NET failure won the race with th5_done: never leave it unanswered.
    this->done_pending_ = false;
    this->done_busy_ = false;
    this->done_reply_after_image_ = false;
    char why[40];
    snprintf(why, sizeof(why), "session closing (BYE reason %u)", reason);
    this->host_->respond(this->done_call_, false, why);
  }
  if ((reason == LINK_BYE_DONE || this->soft_deadline_hit_) && this->image_wanted_())
    this->start_image_();
  else if (this->img_ != IMG_OVER)
    this->img_ = IMG_NONE;
  if (!this->shutdown_begun_) {
    this->shutdown_begun_ = true;
    this->host_->begin_shutdown(reason == LINK_BYE_NET_FAIL || reason == LINK_BYE_LINK);
  }
  this->pump_(now);
}

void Engine::shutdown_step_(uint32_t now) {
  if (!this->radio_off_ || this->out_.active)
    return;
  if (this->link_dead_) {
    this->host_->sync_prefs();
    this->finish_(false);
    return;
  }
  if (this->img_ == IMG_BEGIN || this->img_ == IMG_DATA || this->img_ == IMG_END) {
    this->image_step_(now);
    return;
  }
  // 4.11: BYE only after the preferences are synced and the radio is off.
  this->host_->sync_prefs();
  this->phase_ = PH_CLOSING;
  uint8_t p[2] = {this->bye_reason_, LINK_BYE_F_HISTORY_ACK};
  uint16_t bytes = this->bye_reason_ == LINK_BYE_DONE && this->history_ack_ ? 2 : LINK_BYE_LEN;
  this->request_(LINK_CMD_BYE, p, bytes, now, [this](const RxFrame *rsp, uint32_t t) {
    (void) t;
    if (rsp->cmd != LINK_CMD_ACK)
      this->logf_(2, "%s", describe_err(*rsp).c_str());
    this->finish_(rsp->cmd == LINK_CMD_ACK);
  });
}

void Engine::finish_(bool ok) {
  if (this->phase_ == PH_FINISHED)
    return;
  this->phase_ = PH_FINISHED;
  this->logf_(3, "session over (%s): %" PRIu32 " frames out, %" PRIu32 " in, %" PRIu32 " retries, %" PRIu32 " reverts",
              ok ? "ok" : "failed", this->stats.tx_frames, this->stats.rx_frames, this->stats.retries,
              this->stats.reverts);
  this->host_->on_finished(ok);
}

void Engine::link_failed_(uint8_t cmd, uint32_t now) {
  // 3.5: the CBU abandons the session: orderly shutdown, no further frames.
  this->logf_(1, "link failed on %s", cmd_name(cmd));
  this->link_dead_ = true;
  if (this->img_ == IMG_BEGIN || this->img_ == IMG_DATA || this->img_ == IMG_END) {
    std::string what = std::string("link failed during ") + cmd_name(cmd);
    if (this->img_fw_)
      this->host_->on_firmware_result(false, what);
    else
      this->host_->on_image_result(false, what);
  }
  this->img_ = IMG_NONE;
  this->fail_pending_calls_("link to the MCU failed");
  if (this->phase_ == PH_CLOSING) {
    this->finish_(false);
    return;
  }
  this->phase_ = PH_SHUTDOWN;
  if (!this->shutdown_begun_) {
    this->shutdown_begun_ = true;
    this->host_->begin_shutdown(true);
  }
  this->pump_(now);
}

void Engine::oversize_(uint8_t cmd, uint32_t now) {
  // A request that can never be sent must not be retried by the step functions
  // (that would loop). End the session: stop the image, fail HA's calls, BYE LINK.
  if (cmd == LINK_CMD_BYE || cmd == LINK_CMD_HELLO || this->oversize_failed_) {
    this->link_failed_(cmd, now);  // nothing more can be said: finish without BYE
    return;
  }
  this->oversize_failed_ = true;
  if (this->img_ == IMG_BEGIN || this->img_ == IMG_DATA || this->img_ == IMG_END) {
    if (this->img_fw_)
      this->host_->on_firmware_result(false, "request too large for the MCU");
    else
      this->host_->on_image_result(false, "request too large for the MCU");
  }
  this->img_ = IMG_OVER;
  this->config_stage_ = 0;
  this->fail_pending_calls_("request too large for the MCU");
  if (this->phase_ < PH_SHUTDOWN) {
    this->start_shutdown_(LINK_BYE_LINK, now);
  } else {
    this->bye_reason_ = LINK_BYE_LINK;
    this->pump_(now);
  }
}

void Engine::fail_pending_calls_(const char *why) {
  if (this->config_job_) {
    this->config_job_ = false;
    this->host_->respond(this->config_call_, false, why);
  }
  if (this->done_pending_ && (this->done_busy_ || this->link_dead_)) {
    this->done_pending_ = false;
    this->done_busy_ = false;
    this->done_reply_after_image_ = false;
    this->host_->respond(this->done_call_, false, why);
  }
}

// --- frame buffer, firmware package and image transfer (4.7-4.9, 4.14-4.15) ---------------

bool Engine::image_wanted_() const {
  if (this->img_ == IMG_OVER)
    return false;
  if (this->fw_ready_)
    return true;  // 6.6 step 4: a complete firmware package goes instead of the frame
  return this->frame_ready_ && this->frame_id_ != 0 && this->frame_id_ != this->hello_.frame_id;
}

void Engine::start_image_() {
  this->img_fw_ = this->fw_ready_;
  this->img_ = IMG_BEGIN;
}

uint32_t Engine::fw_push_ms_() const {
  // Estimated FW_BEGIN .. FW_END at the current rate: each chunk on the wire, its
  // ACK, and the MCU's erase/program; 300 ms for FW_BEGIN and FW_END (erases and
  // a CRC over up to 50 KB of flash each). The largest image at 115200: ~6.3 s.
  uint32_t chunks = (this->fw_len_ + LINK_IMG_CHUNK - 1) / LINK_IMG_CHUNK;
  return chunks * (this->wire_ms_(LINK_OVERHEAD + LINK_IMGD_O_DATA + LINK_IMG_CHUNK) +
                   this->wire_ms_(LINK_OVERHEAD + 5) + FW_CHUNK_MCU_MS) +
         300u;
}

void Engine::release_frame_() {
  std::vector<uint8_t>().swap(this->frame_);  // give the 25 KB back to the heap
  this->frame_have_ = 0;
  this->frame_ready_ = false;
  this->frame_id_ = 0;
  this->frame_len_ = 0;
}

void Engine::release_fw_() {
  free(this->fw_);
  this->fw_ = nullptr;
  this->fw_size_ = 0;
  this->fw_have_ = 0;
  this->fw_ready_ = false;
}

void Engine::image_step_(uint32_t now) {
  if (this->out_.active)
    return;
  const uint8_t *slot = this->frame_.data();
  const uint8_t *payload = this->img_data_();
  const uint32_t length = this->img_len_();
  if (this->img_fw_) {
    if (this->img_ == IMG_BEGIN) {
      // Without HOLD the MCU ends the session 45 s after t0 (CAP): do not start
      // a transfer that cannot finish (FW_BEGIN already invalidates the frames).
      uint32_t used = now - this->boot_at_ + FW_LEAD_MS, need = this->fw_push_ms_();
      if (!this->holding_() && used + need + FW_CAP_MARGIN_MS > LINK_T_CAP_MS) {
        char why[96];
        snprintf(why, sizeof(why), "not sent: %" PRIu32 " ms into the session, the push needs ~%" PRIu32 " ms", used,
                 need);
        this->image_finished_(false, why, now);
        this->pump_(now);
        return;
      }
      uint8_t p[LINK_FW_BEGIN_LEN];
      link_put32(p + LINK_FWB_O_VERSION, this->fw_ver_);
      link_put32(p + LINK_FWB_O_LENGTH, this->fw_len_);
      link_put32(p + LINK_FWB_O_CRC32, this->fw_crc_);
      link_put32(p + LINK_FWB_O_LOAD, FWU_APP_BASE);
      this->logf_(3, "MCU firmware: FW_BEGIN, %" PRIu32 " bytes, ~%" PRIu32 " ms", this->fw_len_, need);
      this->request_(LINK_CMD_FW_BEGIN, p, sizeof(p), now, [this](const RxFrame *rsp, uint32_t t) {
        if (rsp->cmd != LINK_CMD_ACK) {
          this->image_finished_(false, describe_err(*rsp), t);
        } else if (rsp->len >= 2 && rsp->payload[LINK_ACK_O_DATA] == LINK_IMG_HAVE) {
          this->image_finished_(true, "the MCU already runs this firmware", t);
        } else {
          this->hello_.frame_id = 0;  // FW_BEGIN invalidated both frame slots
          this->img_ = IMG_DATA;
          this->img_off_ = 0;
          this->img_stall_ = 0;
        }
        this->pump_(t);
      });
      return;
    }
    if (this->img_ == IMG_END) {
      uint8_t p[LINK_FW_END_LEN];
      link_put32(p, this->fw_crc_);
      this->request_(LINK_CMD_FW_END, p, sizeof(p), now, [this](const RxFrame *rsp, uint32_t t) {
        if (rsp->cmd != LINK_CMD_ACK)
          this->image_finished_(false, describe_err(*rsp), t);
        else
          this->image_finished_(true, "staged; the MCU installs it after power-off", t);
        this->pump_(t);
      });
      return;
    }
  }
  switch (this->img_) {
    case IMG_BEGIN: {
      uint8_t p[LINK_IMG_BEGIN_LEN] = {};
      link_put64(p + LINK_IMGB_O_FRAME_ID, this->frame_id_);
      p[LINK_IMGB_O_BPP] = slot[5];
      p[LINK_IMGB_O_ENCODING] = slot[6];
      p[LINK_IMGB_O_FLAGS] = slot[7];
      link_put32(p + LINK_IMGB_O_LENGTH, length);
      link_put32(p + LINK_IMGB_O_CRC32, link_get32(slot + 24));
      this->request_(LINK_CMD_IMG_BEGIN, p, sizeof(p), now, [this](const RxFrame *rsp, uint32_t t) {
        if (rsp->cmd != LINK_CMD_ACK) {
          this->image_finished_(false, describe_err(*rsp), t);
        } else if (rsp->len >= 2 && rsp->payload[LINK_ACK_O_DATA] == LINK_IMG_HAVE) {
          this->hello_.frame_id = this->frame_id_;
          this->image_finished_(true, "MCU already has this frame", t);
        } else {
          this->img_ = IMG_DATA;
          this->img_off_ = 0;
          this->img_stall_ = 0;
        }
        this->pump_(t);
      });
      return;
    }
    case IMG_DATA: {
      uint32_t chunk = LINK_IMG_CHUNK;
      uint32_t peer = this->hello_.rx_max > 4 ? ((this->hello_.rx_max - 4u) & ~3u) : 4u;
      if (peer < chunk)
        chunk = peer;
      uint32_t n = length - this->img_off_;
      if (n > chunk)
        n = chunk;
      uint8_t *p = this->scratch_;  // LINK_IMGD_O_DATA + LINK_IMG_CHUNK <= LINK_MAX_TO_MCU
      link_put32(p + LINK_IMGD_O_OFFSET, this->img_off_);
      memcpy(p + LINK_IMGD_O_DATA, payload + this->img_off_, n);
      static_assert(LINK_IMGD_O_DATA + LINK_IMG_CHUNK <= sizeof(scratch_), "IMG_DATA scratch");
      this->request_(LINK_CMD_IMG_DATA, p, (uint16_t) (LINK_IMGD_O_DATA + n), now,
                     [this](const RxFrame *rsp, uint32_t t) {
                       uint32_t before = this->img_off_;
                       if (rsp->cmd == LINK_CMD_ERR) {
                         if (rsp->len >= LINK_ERR_LEN && rsp->payload[LINK_ERR_O_CODE] == LINK_E_OFFSET &&
                             link_get32(rsp->payload + LINK_ERR_O_DETAIL) <= this->img_len_()) {
                           this->img_off_ = link_get32(rsp->payload + LINK_ERR_O_DETAIL);  // resync (4.8)
                         } else {
                           this->image_finished_(false, describe_err(*rsp), t);
                           this->pump_(t);
                           return;
                         }
                       } else if (rsp->len < 5 || link_get32(rsp->payload + LINK_ACK_O_DATA) > this->img_len_()) {
                         this->image_finished_(false, "bad IMG_DATA ACK", t);
                         this->pump_(t);
                         return;
                       } else {
                         this->img_off_ = link_get32(rsp->payload + LINK_ACK_O_DATA);
                       }
                       this->img_stall_ = this->img_off_ == before ? this->img_stall_ + 1 : 0;
                       if (this->img_stall_ >= 3) {
                         this->image_finished_(false, "IMG_DATA makes no progress", t);
                       } else if (this->img_off_ == this->img_len_()) {
                         this->img_ = IMG_END;
                       }
                       this->pump_(t);
                     });
      return;
    }
    case IMG_END: {
      uint8_t p[LINK_IMG_END_LEN];
      link_put64(p, this->frame_id_);
      this->request_(LINK_CMD_IMG_END, p, sizeof(p), now, [this](const RxFrame *rsp, uint32_t t) {
        if (rsp->cmd != LINK_CMD_ACK) {
          this->image_finished_(false, describe_err(*rsp), t);
        } else {
          this->hello_.frame_id = this->frame_id_;
          this->image_finished_(true, "frame committed", t);
        }
        this->pump_(t);
      });
      return;
    }
    default:
      return;
  }
}

void Engine::image_finished_(bool ok, const std::string &what, uint32_t now) {
  (void) now;
  this->img_ = IMG_OVER;
  if (this->img_fw_) {
    // The CBU's log is the only place this shows at once (the radio is off);
    // HA sees the result as the next session's mcu_fw.
    this->logf_(ok ? 3 : 2, "MCU firmware %" PRIu32 ".%" PRIu32 ".%" PRIu32 ": %s", this->fw_ver_ >> 24,
                (this->fw_ver_ >> 16) & 0xFFu, this->fw_ver_ & 0xFFFFu, what.c_str());
    this->host_->on_firmware_result(ok, what);
    this->release_fw_();
  } else {
    if (!ok)
      this->logf_(2, "frame %08" PRIx32 "%08" PRIx32 ": %s", (uint32_t) (this->frame_id_ >> 32),
                  (uint32_t) this->frame_id_, what.c_str());
    this->host_->on_image_result(ok, what);
  }
  if (this->done_reply_after_image_) {
    this->done_reply_after_image_ = false;
    this->done_busy_ = false;
    this->done_pending_ = false;
    this->host_->respond(this->done_call_, ok, ok ? "" : what);
  }
}

// --- platform events --------------------------------------------------------------------

void Engine::wifi_connected(uint32_t now) {
  if (!this->wifi_up_) {
    this->wifi_up_ = true;
    this->wifi_up_at_ = now;
  }
  this->pump_(now);
}

void Engine::api_connected(bool up, int8_t rssi, uint32_t now) {
  this->rssi_ = rssi;
  if (up != this->api_up_) {
    this->api_up_ = up;
    this->api_changed_at_ = now;
    if (up && !this->api_ever_up_) {
      this->api_ever_up_ = true;
      this->api_first_up_at_ = now;
      this->last_ha_at_ = now;
    }
  }
  this->pump_(now);
}

void Engine::net_failed(uint8_t state, uint8_t detail, uint32_t now) {
  if (!this->net_fail_state_) {
    this->net_fail_state_ = state | LINK_NET_FAILED;
    this->net_fail_detail_ = detail;
  }
  this->pump_(now);
}

void Engine::ota_started(uint32_t now) {
  // 4.10: OTA sends NET OTA, then HOLD 900; th5_done no longer ends the session
  // and the NO_HA / API / soft-deadline timers stop (holding_()).
  if (!this->ota_started_ && this->phase_ < PH_SHUTDOWN) {
    this->ota_started_ = true;
    this->hold_pending_ = true;
    this->logf_(3, "OTA started: holding the session");
  }
  this->pump_(now);
}

void Engine::ha_activity(uint32_t now) {
  if (this->api_ever_up_)
    this->last_ha_at_ = now;
}

void Engine::radio_off(uint32_t now) {
  this->radio_off_ = true;
  this->pump_(now);
}

void Engine::action_config(uint32_t call_id, const ConfigRequest &cfg, uint32_t now) {
  this->ha_activity(now);
  if (this->link_dead_ || this->phase_ >= PH_SHUTDOWN) {
    this->host_->respond(call_id, false, "session closing");
    return;
  }
  if (this->config_job_) {
    this->host_->respond(call_id, false, "busy: the previous th5_config is still being applied");
    return;
  }
  if (cfg.offset_min < INT16_MIN || cfg.offset_min > INT16_MAX || cfg.next_offset_min < INT16_MIN ||
      cfg.next_offset_min > INT16_MAX) {
    this->host_->respond(call_id, false, "offset does not fit i16");
    return;
  }
  uint8_t tmp[CONFIG_TLV_MAX];
  std::string err;
  cfg.encode_tlv(tmp, sizeof(tmp), err);
  if (!err.empty()) {
    this->host_->respond(call_id, false, err);
    return;
  }
  this->config_ = cfg;
  this->config_job_ = true;
  this->config_stage_ = 0;
  this->config_call_ = call_id;
  this->config_at_ = now;
  this->config_seen_ = true;
  this->pump_(now);
}

void Engine::action_frame(uint32_t call_id, int32_t offset, const uint8_t *data, size_t len, uint32_t now) {
  this->ha_activity(now);
  if (this->link_dead_ || this->phase_ >= PH_SHUTDOWN || this->done_busy_) {
    this->host_->respond(call_id, false, "session closing");
    return;
  }
  if (this->img_ == IMG_BEGIN || this->img_ == IMG_DATA || this->img_ == IMG_END) {
    this->host_->respond(call_id, false, "busy: frame transfer to the MCU in progress");
    return;
  }
  if (this->fw_ready_) {
    // 6.6 step 4: a complete firmware package goes instead of the frame.
    this->host_->respond(call_id, false, "an MCU firmware package is buffered: no frame in this session");
    return;
  }
  if (offset < 0 || (size_t) offset > this->frame_have_ || (size_t) offset + len > SLOT_BYTES) {
    char buf[80];
    snprintf(buf, sizeof(buf), "offset %ld: expected 0..%u, total <= %u", (long) offset, (unsigned) this->frame_have_,
             (unsigned) SLOT_BYTES);
    this->host_->respond(call_id, false, buf);
    return;
  }
  if (this->fw_ != nullptr)
    this->release_fw_();  // an unfinished package upload is abandoned
  if (this->frame_.size() != SLOT_BYTES)
    this->frame_.assign(SLOT_BYTES, 0xFF);
  if (this->frame_ready_ && offset != 0) {
    // The buffered frame is complete and CRC-checked: it is immutable until a new
    // upload starts at offset 0. A byte-identical rewrite (HA retrying a chunk whose
    // response it lost) is accepted as a no-op; anything else is refused, so the
    // header fields sent in IMG_BEGIN always match the checked payload.
    if (memcmp(this->frame_.data() + offset, data, len) == 0) {
      this->host_->respond(call_id, true, "");
    } else {
      this->host_->respond(call_id, false, "frame already complete: restart the upload at offset 0");
    }
    return;
  }
  if (offset == 0) {  // a new upload starts
    this->frame_have_ = 0;
    this->frame_ready_ = false;
    if (this->img_ == IMG_OVER)
      this->img_ = IMG_NONE;
  }
  memcpy(this->frame_.data() + offset, data, len);
  if ((size_t) offset + len > this->frame_have_)
    this->frame_have_ = (size_t) offset + len;
  if (this->frame_have_ >= SLOT_HDR_BYTES) {
    const uint8_t *s = this->frame_.data();
    uint32_t length = link_get32(s + 20);
    // The MCU's IMG_BEGIN rules (4.7), checked here so HA gets the error from
    // th5_frame instead of a silent failure after th5_done.
    char bad[64] = "";
    if (link_get32(s) != SLOT_MAGIC || s[4] != SLOT_VERSION)
      snprintf(bad, sizeof(bad), "not a TH5F v1 slot image");
    else if (s[5] != 1 && s[5] != 2)
      snprintf(bad, sizeof(bad), "bad slot header: bpp %u (1 or 2)", s[5]);
    else if (s[6] != 0)
      snprintf(bad, sizeof(bad), "bad slot header: encoding %u (0 raw)", s[6]);
    else if (s[7] & ~SLOT_FLAGS_KNOWN)
      snprintf(bad, sizeof(bad), "bad slot header: flags 0x%02x (known 0x%02x)", s[7], SLOT_FLAGS_KNOWN);
    else if (length != SLOT_PLANE_BYTES * s[5])
      snprintf(bad, sizeof(bad), "bad slot header: length %" PRIu32 " != %" PRIu32 " x bpp", length,
               SLOT_PLANE_BYTES);
    else if (link_get64(s + 12) == 0)
      snprintf(bad, sizeof(bad), "bad slot header: frame_id 0");
    if (bad[0]) {
      this->frame_have_ = 0;
      this->host_->respond(call_id, false, bad);
      return;
    }
    if (!this->frame_ready_ && this->frame_have_ >= SLOT_HDR_BYTES + length) {
      uint32_t want = link_get32(s + 24);
      uint32_t got = crc32(s + SLOT_HDR_BYTES, length);
      if (got != want) {
        this->frame_have_ = 0;
        char buf[64];
        snprintf(buf, sizeof(buf), "payload CRC %08" PRIx32 " != header %08" PRIx32, got, want);
        this->host_->respond(call_id, false, buf);
        return;
      }
      this->frame_ready_ = true;
      this->frame_id_ = link_get64(s + 12);
      this->frame_len_ = length;
    }
  }
  this->host_->respond(call_id, true, "");
}

void Engine::action_firmware(uint32_t call_id, int32_t offset, const uint8_t *data, size_t len, uint32_t now) {
  this->ha_activity(now);
  char buf[96];
  if (this->link_dead_ || this->phase_ >= PH_SHUTDOWN || this->done_busy_) {
    this->host_->respond(call_id, false, "session closing");
    return;
  }
  if (this->img_ == IMG_BEGIN || this->img_ == IMG_DATA || this->img_ == IMG_END) {
    this->host_->respond(call_id, false, "busy: transfer to the MCU in progress");
    return;
  }
  if (!this->have_hello_ || this->hello_.boot_ver == 0) {
    // An app without the bootloader (or older than it) cannot take FW_BEGIN.
    this->host_->respond(call_id, false, "the MCU has no bootloader (boot_ver 0): install it over SWD");
    return;
  }
  size_t cap = this->fw_ != nullptr ? this->fw_size_ : FWU_PKG_MAX;
  size_t have = this->fw_ != nullptr ? this->fw_have_ : 0;
  if (offset < 0 || (size_t) offset > have || (size_t) offset + len > cap) {
    snprintf(buf, sizeof(buf), "offset %ld: expected 0..%u, total <= %u", (long) offset, (unsigned) have,
             (unsigned) cap);
    this->host_->respond(call_id, false, buf);
    return;
  }
  if (this->fw_ready_ && offset != 0) {
    // As th5_frame: a complete, checked package is immutable until offset 0.
    if (memcmp(this->fw_ + offset, data, len) == 0)
      this->host_->respond(call_id, true, "");
    else
      this->host_->respond(call_id, false, "firmware already complete: restart the upload at offset 0");
    return;
  }
  if (offset == 0) {
    // A new upload. The header (fwu.h) must come in this first call: its length
    // sizes the buffer, and a package this MCU cannot take is refused at once.
    if (len < FWU_HDR_BYTES) {
      this->host_->respond(call_id, false, "the first chunk must hold the 32-byte package header");
      return;
    }
    uint32_t length = link_get32(data + offsetof(fwu_hdr_t, length));
    uint32_t load = link_get32(data + offsetof(fwu_hdr_t, load));
    uint32_t min_boot = link_get32(data + offsetof(fwu_hdr_t, min_boot_ver));
    buf[0] = 0;
    if (link_get32(data) != FWU_MAGIC)
      snprintf(buf, sizeof(buf), "not a TH5U firmware package");
    else if (load != FWU_APP_BASE)
      snprintf(buf, sizeof(buf), "bad package header: load 0x%08" PRIx32 " (0x%08x)", load, (unsigned) FWU_APP_BASE);
    else if (length < FWU_MIN_IMAGE || length > FWU_MAX_IMAGE || length % 4u)
      snprintf(buf, sizeof(buf), "bad package header: length %" PRIu32 " (%u..%u, a multiple of 4)", length,
               (unsigned) FWU_MIN_IMAGE, (unsigned) FWU_MAX_IMAGE);
    else if (min_boot > this->hello_.boot_ver)
      snprintf(buf, sizeof(buf), "the package needs bootloader v%" PRIu32 ", the MCU has v%u", min_boot,
               this->hello_.boot_ver);
    else if (len > FWU_HDR_BYTES + length)
      snprintf(buf, sizeof(buf), "%u bytes, but the header says %u", (unsigned) len,
               (unsigned) (FWU_HDR_BYTES + length));
    if (buf[0]) {
      this->host_->respond(call_id, false, buf);
      return;
    }
    // Frame and firmware are never sent in one session (6.6 step 4), and the
    // BK7231N has no RAM for both: drop the frame, then allocate exactly the
    // package, keeping FW_HEAP_RESERVE free for Wi-Fi, LwIP and the API.
    this->release_frame_();
    this->release_fw_();
    if (this->img_ == IMG_OVER)
      this->img_ = IMG_NONE;
    size_t size = FWU_HDR_BYTES + length;
    size_t heap = this->host_->free_heap();
    if (heap < size + FW_HEAP_RESERVE) {
      snprintf(buf, sizeof(buf), "not enough RAM: the package needs %u B + %u B reserve, %u B free",
               (unsigned) size, (unsigned) FW_HEAP_RESERVE, (unsigned) heap);
      this->host_->respond(call_id, false, buf);
      return;
    }
    this->fw_ = static_cast<uint8_t *>(malloc(size));
    if (this->fw_ == nullptr) {
      this->host_->respond(call_id, false, "out of memory for the firmware package");
      return;
    }
    this->fw_size_ = size;
    this->fw_len_ = length;
    this->fw_crc_ = link_get32(data + offsetof(fwu_hdr_t, crc32));
    this->fw_ver_ = link_get32(data + offsetof(fwu_hdr_t, fw_version));
    this->logf_(3, "MCU firmware upload: %u B buffer, free heap %u -> %u B", (unsigned) size, (unsigned) heap,
                (unsigned) this->host_->free_heap());
  }
  memcpy(this->fw_ + offset, data, len);
  if ((size_t) offset + len > this->fw_have_)
    this->fw_have_ = (size_t) offset + len;
  if (!this->fw_ready_ && this->fw_have_ == this->fw_size_) {
    uint32_t got = crc32(this->fw_ + FWU_HDR_BYTES, this->fw_len_);
    if (got != this->fw_crc_) {
      this->release_fw_();
      snprintf(buf, sizeof(buf), "image CRC %08" PRIx32 " != header %08" PRIx32, got, this->fw_crc_);
      this->host_->respond(call_id, false, buf);
      return;
    }
    this->fw_ready_ = true;
    this->logf_(3, "MCU firmware %" PRIu32 ".%" PRIu32 ".%" PRIu32 " buffered: %" PRIu32 " bytes (MCU runs %08" PRIx32
                   "); pushed after th5_done",
                this->fw_ver_ >> 24, (this->fw_ver_ >> 16) & 0xFFu, this->fw_ver_ & 0xFFFFu, this->fw_len_,
                this->hello_.fw_version);
  }
  this->host_->respond(call_id, true, "");
}

void Engine::action_done(uint32_t call_id, uint32_t now) {
  this->ha_activity(now);
  if (this->link_dead_) {
    this->host_->respond(call_id, false, "link to the MCU failed");
    return;
  }
  if (this->phase_ >= PH_SHUTDOWN) {
    this->host_->respond(call_id, true, "already closing");
    return;
  }
  if (this->done_pending_) {
    this->host_->respond(call_id, false, "busy: th5_done already pending");
    return;
  }
  this->done_pending_ = true;
  this->done_busy_ = false;
  this->done_call_ = call_id;
  this->pump_(now);
}

}  // namespace th5link
