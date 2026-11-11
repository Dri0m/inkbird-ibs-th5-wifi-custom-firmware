#pragma once
// CBU side of the MCU <-> CBU link (docs/protocol.md), without ESPHome.
//
// The engine is the stop-and-wait endpoint (protocol.md 3) plus the session that
// th5_link runs (protocol.md 6), as an event-driven state machine. It mirrors
// tools/th5link/link.py (Endpoint, role "cbu") and tools/th5link/fakecbu.py
// (the session script). Everything platform-specific goes through Host: the
// ESPHome component (th5_link.cpp) implements it on the BK7231N, and
// esphome/test/th5_host.cpp implements it for the host test against the Python
// MCU model.
//
// Time is a free-running millisecond counter (millis()); all comparisons are
// wrap-safe.

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <string>
#include <vector>

#include "th5_proto.h"

namespace th5link {

// th5_link version announced in HELLO: major << 24 | minor << 16 | patch.
static constexpr uint32_t FW_VERSION = 0x00010000u;

// Frame slot image (firmware/src/frame.h): 32-byte header, then the payload.
// th5_frame carries a slot image; th5_done streams its payload to the MCU.
static constexpr uint32_t SLOT_MAGIC = 0x46354854u;  // "TH5F"
static constexpr uint8_t SLOT_VERSION = 1;
static constexpr size_t SLOT_HDR_BYTES = 32;
static constexpr size_t SLOT_BYTES = 0x6200;  // 25 088

static constexpr uint8_t PROBE_PAYLOAD[4] = {0x55, 0xAA, 0x00, 0xFF};
static constexpr uint32_t HOLD_MAINTENANCE_S = 900;
static constexpr uint32_t FORGET_WAIT_MS = 1000;   // FORGET_PENDING: wait this long for FORGET
static constexpr uint32_t BUSY_RETRY_MS = 100;      // ERR BUSY: resend with a new seq after this
static constexpr uint8_t BUSY_RETRIES = 5;
// Smallest MCU rx_max this CBU can work with: the largest fixed-size request
// (IMG_BEGIN, 20 B) and room for a CONFIG with every v1 tag. v1 MCUs announce 1024.
static constexpr uint16_t MIN_MCU_RX_MAX = 64;
// Largest CONFIG payload: every v1 tag, 2 B header + at most 2 B value.
static constexpr size_t CONFIG_TLV_MAX = LINK_CFG_TAG_COUNT * 4u;
// Slot header rules (firmware/src/frame.h, protocol.md 4.7), checked on th5_frame.
static constexpr uint32_t SLOT_PLANE_BYTES = 416u * 30u;  // 12 480
static constexpr uint8_t SLOT_FLAGS_KNOWN = 0x01;         // bit 0 BAND
// MCU firmware package (th5_firmware, design.md 4.7): fwu.h's 32-byte staging
// header, then the image. It fills at most the staging area (slots A + B).
static constexpr size_t FWU_PKG_MAX = FWU_HDR_BYTES + FWU_MAX_IMAGE;  // 50 176
// th5_firmware allocates the package only if this much heap stays free beside it
// (Wi-Fi, LwIP and the API connection keep working while HA uploads).
static constexpr size_t FW_HEAP_RESERVE = 20u * 1024u;
// Push budget: skip the firmware if the estimated transfer would end within this
// margin of the MCU's 45 s cap. The MCU's t0 is CBU power-on, before the CBU
// boots and ESPHome reaches Engine::start: FW_LEAD_MS covers that unseen time.
// An overrun is safe (nothing is committed) but wastes one of HA's attempts.
static constexpr uint32_t FW_CAP_MARGIN_MS = 3000;
static constexpr uint32_t FW_LEAD_MS = 2000;
static constexpr uint32_t FW_CHUNK_MCU_MS = 30;  // MCU per IMG_DATA: <= 3 erases + programming

/// HELLO_RSP (protocol.md 4.2), decoded.
struct HelloInfo {
  uint8_t proto{0};
  uint8_t reset_cause{0};
  uint32_t baud{0};
  uint16_t rx_max{0};
  uint32_t fw_version{0};
  uint64_t frame_id{0};
  uint32_t settings_crc{0};
  uint8_t units{0};
  uint8_t trigger{0};
  uint16_t flags{0};
  int16_t t_centi{LINK_T_NONE};
  uint16_t rh_centi{LINK_RH_NONE};
  uint16_t batt_mv{0};
  uint8_t batt_pct{LINK_PCT_NONE};
  uint8_t charge{LINK_CHG_UNKNOWN};
  uint32_t last_session_ms{0};
  uint8_t last_result{0};
  uint8_t fail_count{0};
  uint8_t boot_ver{0};  ///< 0 = no bootloader: th5_firmware is refused

  std::vector<uint8_t> history;  ///< validated count/flags + hourly records; empty on old/malformed tail
  static bool parse(const uint8_t *p, size_t n, HelloInfo &out);
};

/// What th5_config carries (design.md 3 step 7.1, protocol.md 4.5/4.6).
/// Tags not marked present are not sent (CONFIG is a delta).
struct ConfigRequest {
  bool history_ack{false};  // local delivery receipt, not a CONFIG TLV
  uint32_t utc{0};
  int32_t offset_min{0};
  uint32_t next_change_utc{0};
  int32_t next_offset_min{0};
  bool has[LINK_CFG_TAG_COUNT + 1]{};
  int32_t value[LINK_CFG_TAG_COUNT + 1]{};

  void set(uint8_t tag, int32_t v) {
    this->has[tag] = true;
    this->value[tag] = v;
  }
  /// Encode the CONFIG TLV payload. Returns 0 and sets err if a value does not
  /// fit its wire type (range checks proper are the MCU's job, 4.6).
  size_t encode_tlv(uint8_t *out, size_t cap, std::string &err) const;
  /// TIME payload for a given utc.
  void encode_time(uint32_t utc_now, uint8_t out[LINK_TIME_LEN]) const;
  bool has_flags() const { return this->has[LINK_CFG_FLAGS]; }
  bool maintenance() const { return this->has_flags() && (this->value[LINK_CFG_FLAGS] & LINK_CFGF_MAINTENANCE); }
};

/// zlib CRC-32 (slot payload, settings_crc).
uint32_t crc32(const uint8_t *p, size_t n, uint32_t crc = 0);
/// Strict base64 decode (RFC 4648, padding optional, no whitespace). Returns
/// false on a bad character or if the output would exceed cap.
bool base64_decode(const char *in, size_t len, uint8_t *out, size_t cap, size_t &out_len);
/// "nonce;units;frame_id;settings_crc;boot;mcu_fw;boot_ver" (protocol.md 6.3): the
/// boot counter (8 hex digits) is a backstop if the RNG repeats across cold boots;
/// mcu_fw is HELLO_RSP fw_version (8 hex digits), boot_ver 2 hex digits.
std::string session_string(uint32_t nonce, const HelloInfo &h, uint32_t boot);
const char *err_name(uint8_t code);
const char *cmd_name(uint8_t cmd);

/// A frame received from the MCU. The payload is a private copy.
struct RxFrame {
  uint8_t ver{0};
  uint8_t seq{0};
  uint8_t cmd{0};
  uint16_t len{0};
  uint8_t payload[LINK_MAX_TO_CBU]{};
};

/// Byte stream -> frames, with resync (protocol.md 3.4); mirrors proto.StreamParser.
class Parser {
 public:
  explicit Parser(uint16_t max_payload) : max_payload_(max_payload) {}
  /// Add one byte received at time now. Drain next() after every push.
  void push(uint8_t b, uint32_t now);
  /// Extract the next complete frame from the buffer, if any (dropping garbage).
  bool next(RxFrame &out);
  /// Inter-byte timeout: a partial frame that saw no byte for > 20 ms loses its A5.
  bool expire(uint32_t now);
  void reset();
  bool partial() const { return this->len_ != 0; }
  uint32_t last_byte() const { return this->last_byte_; }
  uint32_t discarded() const { return this->discarded_; }

 protected:
  void drop_(size_t n);
  uint16_t max_payload_;
  uint8_t buf_[LINK_OVERHEAD + LINK_MAX_TO_CBU]{};
  size_t len_{0};
  uint32_t last_byte_{0};
  uint32_t discarded_{0};
};

/// Everything the engine needs from its platform.
class Host {
 public:
  virtual ~Host() = default;
  /// Put bytes on the line (may block while the UART FIFO drains).
  virtual void uart_write(const uint8_t *data, size_t len) = 0;
  /// Switch the line rate. Called with nothing left to transmit.
  virtual void uart_set_baud(uint32_t baud) = 0;
  /// A valid HELLO_RSP arrived: publish the sensors and the session text sensor.
  virtual void on_hello(const HelloInfo &h, const std::string &session) = 0;
  /// Start the orderly shutdown: safe_mode.mark_successful, API disconnect,
  /// Wi-Fi off. Call Engine::radio_off() when the radio is off. urgent: a NET
  /// failure or link failure (the MCU's 2 s grace runs): skip the API wait.
  virtual void begin_shutdown(bool urgent) = 0;
  /// Write pending preferences to flash (global_preferences->sync()).
  virtual void sync_prefs() = 0;
  /// FORGET: clear the saved Wi-Fi and API preferences and sync flash.
  virtual void forget() = 0;
  /// Answer an HA action call (supports_response: status).
  virtual void respond(uint32_t call_id, bool ok, const std::string &message) = 0;
  virtual uint32_t random32() = 0;
  /// Persisted boot counter, appended to the session string.
  virtual uint32_t boot_count() { return 0; }
  /// The frame push to the MCU ended (ok: committed or already there). Persist a
  /// failure so the next session can report it (th5_done answers before the push).
  virtual void on_image_result(bool /*ok*/, const std::string & /*what*/) {}
  /// The MCU firmware push ended (ok: staged for the bootloader, or already
  /// running). The next session's mcu_fw is HA's confirmation.
  virtual void on_firmware_result(bool /*ok*/, const std::string & /*what*/) {}
  /// Free heap in bytes, for th5_firmware's allocation check and the log.
  virtual size_t free_heap() { return SIZE_MAX; }
  /// level: 1 error, 2 warning, 3 info, 4 debug, 5 verbose.
  virtual void log(int level, const std::string &message) = 0;
  /// The session is over (BYE answered, or the link gave up). The engine is idle.
  virtual void on_finished(bool /*ok*/) {}
};

struct Options {
  /// Follow the MCU to 500 kbaud after HELLO_RSP (protocol.md 2.3). Off: the CBU
  /// stays at 115200 and lets the MCU's probe window lapse (it reverts by itself).
  bool fast_baud{false};
  uint32_t wifi_timeout_ms{10000};  ///< boot -> Wi-Fi up, else NET FAIL_WIFI
  uint32_t api_timeout_ms{10000};   ///< Wi-Fi up -> API client, else NET FAIL_API
  uint32_t ha_timeout_ms{3000};     ///< API up or last HA call -> th5_config, else BYE NO_HA
  /// Soft session deadline from start(): HA has not finished (th5_done) by then:
  /// push a complete buffered frame, then BYE NO_HA. 0 = off. Not while holding.
  uint32_t session_timeout_ms{25000};
};

class Engine {
 public:
  Engine(Host *host, const Options &opts) : host_(host), opts_(opts), parser_(LINK_MAX_TO_CBU) {}
  ~Engine() { free(this->fw_); }
  Engine(const Engine &) = delete;
  Engine &operator=(const Engine &) = delete;

  /// Before start() only.
  void set_options(const Options &opts) { this->opts_ = opts; }

  // --- driven by the platform ---------------------------------------------------
  void start(uint32_t now);
  void feed(const uint8_t *data, size_t len, uint32_t now);
  void tick(uint32_t now);
  /// Earliest time at which tick() has something to do (for virtual-time tests).
  /// Returns false if nothing is scheduled.
  bool next_deadline(uint32_t now, uint32_t &when) const;

  // --- network events ---------------------------------------------------------------
  void wifi_connected(uint32_t now);
  void api_connected(bool up, int8_t rssi, uint32_t now);
  /// A network failure the platform detected itself (e.g. no credentials).
  void net_failed(uint8_t state, uint8_t detail, uint32_t now);
  void ota_started(uint32_t now);
  /// Any HA action call arrived (accepted or rejected): restarts the ha_timeout.
  void ha_activity(uint32_t now);
  /// The shutdown requested by Host::begin_shutdown() has finished.
  void radio_off(uint32_t now);

  // --- HA actions (each answered through Host::respond) ------------------------------
  void action_config(uint32_t call_id, const ConfigRequest &cfg, uint32_t now);
  void action_frame(uint32_t call_id, int32_t offset, const uint8_t *data, size_t len, uint32_t now);
  void action_done(uint32_t call_id, uint32_t now);
  /// A byte range of an MCU firmware package (.fwu). Offset 0 starts a new upload
  /// and must hold the 32-byte header; the package replaces any buffered frame.
  void action_firmware(uint32_t call_id, int32_t offset, const uint8_t *data, size_t len, uint32_t now);

  // --- state, for the platform -------------------------------------------------------
  bool active() const { return this->phase_ != PH_IDLE && this->phase_ != PH_FINISHED; }
  bool finished() const { return this->phase_ == PH_FINISHED; }
  uint32_t baud() const { return this->baud_; }
  bool have_hello() const { return this->have_hello_; }
  const HelloInfo &hello() const { return this->hello_; }
  bool holding() const { return this->holding_(); }
  const char *phase_name() const;

  struct Stats {
    uint32_t tx_frames{0}, rx_frames{0}, retries{0}, reverts{0}, dup_requests{0}, collisions{0};
  } stats;

 protected:
  using Done = std::function<void(const RxFrame *rsp, uint32_t now)>;

  enum Phase : uint8_t {
    PH_IDLE,       // before start()
    PH_HELLO,      // HELLO outstanding (repeated until answered)
    PH_PROBE,      // HELLO_RSP said 500 k: gap, then the probe PING
    PH_SLOW_WAIT,  // HELLO_RSP said 500 k but fast_baud is off: let the MCU's probe lapse
    PH_LINKED,     // the session proper
    PH_SHUTDOWN,   // radio going off, then IMG_*, then BYE
    PH_CLOSING,    // BYE outstanding
    PH_FINISHED,
  };
  enum ImgPhase : uint8_t { IMG_NONE, IMG_BEGIN, IMG_DATA, IMG_END, IMG_OVER };

  // link layer (link.py Endpoint)
  bool request_(uint8_t cmd, const uint8_t *payload, uint16_t len, uint32_t now, Done done, bool probe = false);
  void emit_(const uint8_t *raw, size_t n, uint32_t now);
  uint32_t wire_ms_(size_t n) const;
  void set_baud_(uint32_t baud, uint32_t now);
  void revert_(uint32_t now);
  void on_frame_(const RxFrame &f, uint32_t now);
  void on_response_(const RxFrame &f, uint32_t now);
  void on_request_(const RxFrame &f, uint32_t now);
  void poll_link_(uint32_t now);

  // session (fakecbu.py _script)
  void drain_(uint32_t now);
  void pump_(uint32_t now);
  void send_hello_(uint32_t now);
  void on_hello_rsp_(const RxFrame *rsp, uint32_t now);
  void enter_linked_(uint32_t now);
  void finish_(bool ok);
  /// Stay on: no NO_HA / API / soft-deadline timers, th5_done does not end the session.
  bool holding_() const { return this->bench_ || this->maintenance_ || this->ota_started_; }
  void linked_step_(uint32_t now);
  void config_step_(uint32_t now);
  void send_net_(uint8_t state, uint8_t detail, int8_t rssi, uint32_t now, std::function<void(uint32_t)> then = nullptr);
  void start_shutdown_(uint8_t reason, uint32_t now);
  void shutdown_step_(uint32_t now);
  bool image_wanted_() const;
  void start_image_();
  uint32_t img_len_() const { return this->img_fw_ ? this->fw_len_ : this->frame_len_; }
  const uint8_t *img_data_() const {
    return this->img_fw_ ? this->fw_ + FWU_HDR_BYTES : this->frame_.data() + SLOT_HDR_BYTES;
  }
  uint32_t fw_push_ms_() const;
  void release_frame_();
  void release_fw_();
  void image_step_(uint32_t now);
  void image_finished_(bool ok, const std::string &what, uint32_t now);
  void link_failed_(uint8_t cmd, uint32_t now);
  void oversize_(uint8_t cmd, uint32_t now);
  void fail_pending_calls_(const char *why);
  void logf_(int level, const char *fmt, ...);
  static bool due_(uint32_t now, uint32_t t) { return (int32_t) (now - t) >= 0; }

  Host *host_;
  Options opts_;
  Parser parser_;

  // link state
  uint32_t baud_{LINK_BAUD_SLOW};
  bool sticky_slow_{false};
  uint8_t tx_seq_{0};
  bool have_valid_rx_{false};
  uint32_t last_valid_rx_{0};
  bool fast_{false};
  uint32_t fast_since_{0};
  struct {
    bool active{false};
    uint8_t seq{0}, cmd{0};
    uint8_t raw[LINK_RX_BUF]{};
    uint16_t raw_len{0};
    uint32_t timeout{0};
    bool probe{false};
    uint8_t attempts{0};
    uint8_t busy_left{BUSY_RETRIES};
    bool deadline_set{false};
    uint32_t deadline{0};
    bool resend_pending{false};
    uint32_t resend_at{0};
    Done done;
  } out_;
  // a request repeated after ERR BUSY
  bool busy_pending_{false};
  uint32_t busy_at_{0};
  struct {
    bool valid{false};
    uint8_t seq{0}, cmd{0};
    uint16_t crc{0};
    uint8_t rsp[LINK_OVERHEAD + 1 + LINK_PING_MAX]{};
    uint16_t rsp_len{0};
  } dedup_;

  // session state
  Phase phase_{PH_IDLE};
  uint32_t boot_at_{0};
  uint32_t soft_base_{0};  // soft session deadline counts from here
  bool soft_deadline_hit_{false};
  uint32_t last_ha_at_{0};  // API first up, or the last HA action call
  uint32_t wait_until_{0};
  HelloInfo hello_;
  bool have_hello_{false};
  bool bench_{false};
  bool forget_flag_{false};
  uint32_t forget_deadline_{0};
  bool forget_received_{false};

  bool net_connecting_sent_{false};
  bool net_wifi_up_sent_{false};
  bool wifi_up_{false};
  uint32_t wifi_up_at_{0};
  bool api_up_{false};
  bool api_ever_up_{false};
  uint32_t api_first_up_at_{0};
  uint32_t api_changed_at_{0};
  int8_t rssi_{0};
  bool api_up_sent_{false};
  uint8_t net_fail_state_{0};
  uint8_t net_fail_detail_{0};
  bool net_fail_sent_{false};
  bool ota_started_{false};
  bool ota_net_sent_{false};

  bool config_seen_{false};
  bool config_job_{false};
  uint8_t config_stage_{0};
  uint32_t config_call_{0};
  uint32_t config_at_{0};
  ConfigRequest config_;
  bool ha_config_net_sent_{false};
  bool maintenance_{false};
  bool hold_pending_{false};
  bool hold_granted_{false};

  bool done_pending_{false};  // th5_done received, not answered yet
  bool done_busy_{false};     // ... and being handled (bench/maintenance image push)
  uint32_t done_call_{0};
  bool done_reply_after_image_{false};
  bool probe_sent_{false};

  // frame buffer (th5_frame)
  std::vector<uint8_t> frame_;
  size_t frame_have_{0};
  bool frame_ready_{false};
  bool oversize_failed_{false};
  uint64_t frame_id_{0};
  uint32_t frame_len_{0};

  // firmware package (th5_firmware): malloc'd on its first chunk, sized from the
  // header, freed after the push. It replaces the frame buffer (never both).
  uint8_t *fw_{nullptr};
  size_t fw_size_{0};  // FWU_HDR_BYTES + length
  size_t fw_have_{0};
  bool fw_ready_{false};
  uint32_t fw_len_{0}, fw_crc_{0}, fw_ver_{0};

  uint8_t scratch_[LINK_MAX_TO_MCU]{};  // IMG_DATA payload (kept off the stack)

  // image transfer: a frame (IMG_BEGIN/IMG_END) or the firmware (FW_BEGIN/FW_END)
  ImgPhase img_{IMG_NONE};
  bool img_fw_{false};
  uint32_t img_off_{0};
  uint8_t img_stall_{0};

  // shutdown
  uint8_t bye_reason_{LINK_BYE_DONE};
  bool history_ack_{false};
  bool shutdown_begun_{false};
  bool radio_off_{false};
  bool link_dead_{false};
};

}  // namespace th5link
