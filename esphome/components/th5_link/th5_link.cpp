#include "th5_link.h"

#include <cinttypes>
#include <cmath>

#include "esphome/core/application.h"
#include "esphome/core/alloc_helpers.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/components/json/json_util.h"
#include "esphome/components/wifi/wifi_component.h"
#ifdef USE_LIBRETINY
#include "esphome/components/uart/uart_component_libretiny.h"
#endif
#ifdef USE_BK72XX
extern "C" int wlan_sta_disconnect(void);  // BDK wlan_ui_pub.h
extern "C" int bk_wlan_stop_scan(void);
#endif

namespace esphome::th5_link {

static const char *const TAG = "th5_link";

using namespace th5link;

// --- actions ---------------------------------------------------------------------------
// String tables in the layout UserServiceStatic expects (api/user_services.h): the
// name, then each argument name; with action metadata compiled in, the name is
// followed by a description and each argument by (name, description, example).
#ifdef USE_API_USER_DEFINED_ACTION_METADATA
static const char *const CONFIG_STRINGS[] = {"th5_config", nullptr, "json", nullptr, nullptr};
static const char *const FRAME_STRINGS[] = {"th5_frame", nullptr, "offset", nullptr, nullptr, "data", nullptr, nullptr};
static const char *const FIRMWARE_STRINGS[] = {"th5_firmware", nullptr, "offset", nullptr, nullptr,
                                               "data",         nullptr, nullptr};
static const char *const DONE_STRINGS[] = {"th5_done", nullptr};
#else
static const char *const CONFIG_STRINGS[] = {"th5_config", "json"};
static const char *const FRAME_STRINGS[] = {"th5_frame", "offset", "data"};
static const char *const FIRMWARE_STRINGS[] = {"th5_firmware", "offset", "data"};
static const char *const DONE_STRINGS[] = {"th5_done"};
#endif

ConfigAction::ConfigAction(Th5Link *parent)
    : api::UserServiceBase<StringRef>(CONFIG_STRINGS, fnv1_hash("th5_config"), api::enums::SUPPORTS_RESPONSE_STATUS),
      parent_(parent) {}
void ConfigAction::execute(uint32_t call_id, bool /*return_response*/, StringRef json) {
  this->parent_->on_config_action(call_id, json);
}

FrameAction::FrameAction(Th5Link *parent, bool firmware)
    : api::UserServiceBase<int32_t, StringRef>(firmware ? FIRMWARE_STRINGS : FRAME_STRINGS,
                                               fnv1_hash(firmware ? "th5_firmware" : "th5_frame"),
                                               api::enums::SUPPORTS_RESPONSE_STATUS),
      parent_(parent),
      firmware_(firmware) {}
void FrameAction::execute(uint32_t call_id, bool /*return_response*/, int32_t offset, StringRef data) {
  this->parent_->on_data_action(call_id, this->firmware_, offset, data);
}

DoneAction::DoneAction(Th5Link *parent)
    : api::UserServiceBase<>(DONE_STRINGS, fnv1_hash("th5_done"), api::enums::SUPPORTS_RESPONSE_STATUS),
      parent_(parent) {}
void DoneAction::execute(uint32_t call_id, bool /*return_response*/) { this->parent_->on_done_action(call_id); }

// --- component -------------------------------------------------------------------------

Th5Link::Th5Link()
    : engine_(this, opts_),
      config_action_(this),
      frame_action_(this, false),
      firmware_action_(this, true),
      done_action_(this) {}

void Th5Link::setup() {
  api::global_api_server->register_user_service(&this->config_action_);
  api::global_api_server->register_user_service(&this->frame_action_);
  api::global_api_server->register_user_service(&this->firmware_action_);
  api::global_api_server->register_user_service(&this->done_action_);
  ota::get_global_ota_callback()->add_global_state_listener(this);
  // ESPHome's fast_connect cache (same key and format as WiFiComponent; this
  // runs before Wi-Fi setup). ESPHome saves it after each join, only on change.
  this->wifi_pin_pref_ =
      global_preferences->make_preference<wifi::SavedWifiFastConnectSettings>(App.get_config_version_hash() + 1, true);
  wifi::SavedWifiFastConnectSettings pin{};
  if (this->wifi_pin_pref_.load(&pin) && pin.ap_index == 0 && pin.channel >= 1 && pin.channel <= 14)
    this->wifi_join_state_ = JOIN_PINNED;
  // Boot counter for the session string (a backstop if random_uint32() repeats
  // across cold boots). Synced now, before HELLO, so a session that ends without
  // a sync (CAP, power cut) still counts.
  this->pref_ = global_preferences->make_preference<PersistState>(fnv1_hash("th5_link_state"));
  if (!this->pref_.load(&this->state_))
    this->state_ = PersistState{};
  this->state_.push_error[sizeof(this->state_.push_error) - 1] = 0;
  this->state_.boot_count++;
  this->save_state_();
  global_preferences->sync();
  this->engine_.set_options(this->opts_);  // set by codegen after construction
  this->high_freq_.start();  // tight loop while the session runs (98 ms probe, 200 ms ACKs)
  while (this->available() > 0)
    this->read();  // boot noise received before setup
  this->engine_.start(millis());
}

// OTA reboots via App.safe_reboot(), not our shutdown: send the same real DEAUTH
// first, or the AP may reject the next association (the MCU's HOLD session stays).
void Th5Link::on_powerdown() {
#ifdef USE_BK72XX
  if (WiFi.status() == WL_CONNECTED) {
    wlan_sta_disconnect();
    delay(DEAUTH_WAIT_MS);
  }
#endif
}

void Th5Link::dump_config() {
  ESP_LOGCONFIG(TAG,
                "th5_link:\n"
                "  Fast baud (500000): %s\n"
                "  Timeouts: Wi-Fi %" PRIu32 " ms, API %" PRIu32 " ms, HA %" PRIu32 " ms, session %" PRIu32 " ms\n"
                "  Boot count: %" PRIu32,
                YESNO(this->opts_.fast_baud), this->opts_.wifi_timeout_ms, this->opts_.api_timeout_ms,
                this->opts_.ha_timeout_ms, this->opts_.session_timeout_ms, this->state_.boot_count);
}

void Th5Link::loop() {
  uint32_t now = millis();
  uint8_t buf[128];
  size_t avail;
  while ((avail = this->available()) > 0) {
    size_t n = avail < sizeof(buf) ? avail : sizeof(buf);
    if (!this->read_array(buf, n))
      break;
    this->engine_.feed(buf, n, now);
  }
  this->engine_.tick(now);
  this->watch_network_(now);
  this->shutdown_step_(now);
}

// ap_index -1 is rejected by ESPHome's loader, so the next start joins by SSID;
// its next successful join stores the new BSSID/channel. Not flushed here: every
// shutdown, NET failure included, syncs before BYE, and if this session re-learns
// the same AP, LibreTiny skips the unchanged key (no flash write at all).
void Th5Link::clear_ap_pin_() {
  if (this->forgotten_)
    return;
  wifi::SavedWifiFastConnectSettings empty{};
  empty.ap_index = -1;
  this->wifi_pin_pref_.save(&empty);
}

void Th5Link::watch_network_(uint32_t now) {
  if (this->sd_state_ != SD_NONE || !this->engine_.active())
    return;
  auto *wifi = wifi::global_wifi_component;
  if (this->wifi_reenable_) {
    // One manager loop ran while DISABLED (drains the stop events). enable()
    // starts an SSID join, which the BK driver does not complete either after a
    // refused pinned join; abort it and let ESPHome scan and join by its results.
    this->wifi_reenable_ = false;
    wifi->enable();
#ifdef USE_BK72XX
    wlan_sta_disconnect();
    bk_wlan_stop_scan();
#endif
    wifi->start_scanning();
  }
  bool connected = wifi->is_connected();
  if (!this->wifi_seen_ && !connected && this->wifi_join_state_ == JOIN_PINNED && now >= PIN_TIMEOUT_MS) {
    // Stale pin (router changed channel or BSSID), or the AP refusing a quick
    // rejoin: the BK driver keeps retrying it past the session's Wi-Fi deadline.
    ESP_LOGW(TAG, "Learned AP not joined within %" PRIu32 " ms: clearing it and rescanning", PIN_TIMEOUT_MS);
    this->wifi_join_state_ = JOIN_FALLBACK;
    this->clear_ap_pin_();
    if (WiFi.scanComplete() != WIFI_SCAN_RUNNING) {  // ESPHome is not already scanning
      wifi->disable();
      this->wifi_reenable_ = true;
    }
    return;
  }
  if (!this->wifi_seen_ && connected) {
    this->wifi_seen_ = true;
    static const char *const JOIN[] = {"scanned", "pinned", "scanned (fallback)"};
    if (this->wifi_join_ != nullptr)
      this->wifi_join_->publish_state(JOIN[this->wifi_join_state_]);
    this->engine_.wifi_connected(now);
  }
  bool up = api::global_api_server->is_connected_with_state_subscription();
  if (up != this->api_up_) {
    this->api_up_ = up;
    int8_t rssi = this->wifi_seen_ ? wifi->wifi_rssi() : 0;
    if (rssi > 0 || rssi < -127)
      rssi = 0;
    this->engine_.api_connected(up, rssi, now);
  }
}

// Orderly shutdown (protocol.md 6.6, design.md 3 step 8): mark the boot good, send
// the API DisconnectRequest so HA records an expected disconnect (entities stay
// available with has_deep_sleep), then the radio goes off. deep_sleep.enter is
// not used: on BK72xx it never returns, and the frame is streamed afterwards.
//
// urgent (NET or link failure) or no API client: no waiting, the radio goes off at
// once (a connected client still gets the DisconnectRequest queued), well inside
// the MCU's 2 s NET-failure grace. Otherwise 50 ms for the last action reply and
// up to api_disconnect_timeout for the clients to close.
void Th5Link::begin_shutdown(bool urgent) {
  bool connected = api::global_api_server->is_connected();
  if (urgent || !connected) {
#ifdef TH5_LINK_SAFE_MODE
    if (this->safe_mode_ != nullptr && !this->forgotten_)
      this->safe_mode_->mark_successful();
#endif
    if (connected)
      api::global_api_server->on_shutdown();
    this->radio_off_();
    return;
  }
  this->sd_state_ = SD_DELAY;
  this->sd_at_ = millis() + 50;
}

// Leave the AP with a real deauthentication. ESPHome's disable() on BK72xx only
// fakes a beacon loss locally (bk_wlan_connection_loss) and stops the station, so
// the AP keeps the association and rejects a sync a minute later.
// wlan_sta_disconnect() makes the supplicant send DEAUTH (reason 3).
//
// DEAUTH, the blocking wait and disable() must stay in one call: if the Wi-Fi
// loop ran in between it would re-associate to the pinned AP, and disable() then
// blocked 1.3 s aborting that attempt. 500 ms rather than 100: with 100 ms, 3 of 7
// syncs 60 s apart needed the scan fallback; with 500 ms, 1 of 7.
void Th5Link::radio_off_() {
#ifdef USE_BK72XX
  if (wifi::global_wifi_component->is_connected()) {
    wlan_sta_disconnect();
    delay(DEAUTH_WAIT_MS);  // NOLINT: no loop() may run before disable()
  }
#endif
  wifi::global_wifi_component->disable();
  this->sd_state_ = SD_WIFI;
  this->sd_at_ = millis() + WIFI_OFF_WAIT_MS;
}

void Th5Link::shutdown_step_(uint32_t now) {
  switch (this->sd_state_) {
    case SD_DELAY:
      if ((int32_t) (now - this->sd_at_) < 0)
        return;
#ifdef TH5_LINK_SAFE_MODE
      if (this->safe_mode_ != nullptr && !this->forgotten_)
        this->safe_mode_->mark_successful();
#endif
      api::global_api_server->on_shutdown();
      this->sd_state_ = SD_API;
      this->sd_at_ = now + this->api_disconnect_timeout_ms_;
      return;
    case SD_API:
      if (api::global_api_server->is_connected() && (int32_t) (now - this->sd_at_) < 0)
        return;
      this->radio_off_();
      return;
    case SD_WIFI:
      if (wifi::global_wifi_component->is_connected() && (int32_t) (now - this->sd_at_) < 0)
        return;
      this->sd_state_ = SD_DONE;
      this->engine_.radio_off(now);
      return;
    default:
      return;
  }
}

// --- actions -> engine ------------------------------------------------------------------

static bool get_int(JsonObject root, const char *key, int32_t &out, bool &present, std::string &err) {
  JsonVariant v = root[key];
  present = !v.isNull();
  if (!present)
    return true;
  if (v.is<bool>()) {
    out = v.as<bool>() ? 1 : 0;
    return true;
  }
  if (!v.is<int32_t>()) {
    err = std::string(key) + ": not an integer";
    return false;
  }
  out = v.as<int32_t>();
  return true;
}

static bool get_u32(JsonObject root, const char *key, uint32_t &out, bool &present, std::string &err) {
  JsonVariant v = root[key];
  present = !v.isNull();
  if (!present)
    return true;
  if (!v.is<uint32_t>()) {
    err = std::string(key) + ": not an unsigned 32-bit integer";
    return false;
  }
  out = v.as<uint32_t>();
  return true;
}

// JSON contract: docs/integration-contract.md. Absent settings keep the MCU's value (CONFIG
// is a delta); unknown keys are ignored.
void Th5Link::on_config_action(uint32_t call_id, StringRef json) {
  ConfigRequest cfg;
  std::string err;
  bool have_utc = false;
  bool ok = json::parse_json(reinterpret_cast<const uint8_t *>(json.c_str()), json.size(), [&](JsonObject root) -> bool {
    bool present;
    int32_t v;
    JsonVariant ack = root["history_ack"];
    if (!ack.isNull() && !ack.is<bool>()) {
      err = "history_ack: not a boolean";
      return false;
    }
    cfg.history_ack = ack.is<bool>() && ack.as<bool>();
    if (!get_u32(root, "utc", cfg.utc, have_utc, err) || !get_int(root, "offset_min", cfg.offset_min, present, err) ||
        !get_u32(root, "next_change_utc", cfg.next_change_utc, present, err) ||
        !get_int(root, "next_offset_min", cfg.next_offset_min, present, err))
      return false;
    JsonVariant mode = root["mode"];
    if (mode.is<const char *>()) {
      std::string m = mode.as<const char *>();
      if (m != "picture" && m != "forecast") {
        err = "mode: picture or forecast";
        return false;
      }
      cfg.set(LINK_CFG_MODE, m == "forecast");
    } else {
      if (!get_int(root, "mode", v, present, err))
        return false;
      if (present)
        cfg.set(LINK_CFG_MODE, v);
    }
    static const struct {
      const char *key;
      uint8_t tag;
    } INTS[] = {{"sync_min", LINK_CFG_SYNC_MIN}, {"clock_min", LINK_CFG_CLOCK_MIN}, {"clock_24h", LINK_CFG_CLOCK_24H},
                {"full_n", LINK_CFG_FULL_N},     {"t_off", LINK_CFG_T_OFF},         {"rh_off", LINK_CFG_RH_OFF},
                {"flags", LINK_CFG_FLAGS}};
    for (const auto &k : INTS) {
      if (!get_int(root, k.key, v, present, err))
        return false;
      if (present)
        cfg.set(k.tag, v);
    }
    if (!cfg.has[LINK_CFG_FLAGS]) {
      static const struct {
        const char *key;
        uint8_t bit;
      } FLAGS[] = {{"badge", LINK_CFGF_BADGE},
                   {"maintenance", LINK_CFGF_MAINTENANCE},
                   {"monochrome", LINK_CFGF_BAND_MONO},
                   {"single_reset", LINK_CFGF_BAND_SINGLE_RESET}};
      int32_t flags = 0;
      bool any = false;
      for (const auto &f : FLAGS) {
        v = 0;
        if (!get_int(root, f.key, v, present, err))
          return false;
        any |= present;
        if (v)
          flags |= f.bit;
      }
      if (any)
        cfg.set(LINK_CFG_FLAGS, flags);
    }
    return true;
  });
  if (!ok || !err.empty() || !have_utc) {
    if (err.empty())
      err = ok ? "utc missing" : "invalid JSON";
    this->engine_.ha_activity(millis());  // HA is there: restart its 3 s window
    this->respond(call_id, false, err);
    return;
  }
  this->engine_.action_config(call_id, cfg, millis());
}

void Th5Link::on_data_action(uint32_t call_id, bool firmware, int32_t offset, StringRef data) {
  size_t cap = data.size() / 4 * 3 + 3;
  if (this->chunk_.size() < cap)
    this->chunk_.resize(cap);
  size_t n = 0;
  if (data.empty() || !th5link::base64_decode(data.c_str(), data.size(), this->chunk_.data(), this->chunk_.size(), n)) {
    this->engine_.ha_activity(millis());
    this->respond(call_id, false, "data: not base64");
    return;
  }
  if (firmware)
    this->engine_.action_firmware(call_id, offset, this->chunk_.data(), n, millis());
  else
    this->engine_.action_frame(call_id, offset, this->chunk_.data(), n, millis());
}

void Th5Link::on_done_action(uint32_t call_id) { this->engine_.action_done(call_id, millis()); }

void Th5Link::on_ota_global_state(ota::OTAState state, float /*progress*/, uint8_t /*error*/,
                                  ota::OTAComponent * /*comp*/) {
  if (state == ota::OTA_STARTED)
    this->engine_.ota_started(millis());
}

// --- th5link::Host ------------------------------------------------------------------------

void Th5Link::uart_write(const uint8_t *data, size_t len) { this->write_array(data, len); }

void Th5Link::uart_set_baud(uint32_t baud) {
  this->flush();  // nothing of ours may still be in the shifter
  this->parent_->set_baud_rate(baud);
#ifdef USE_LIBRETINY
  // ESPHome's LibreTiny UART has no load_settings(). SerialClass::configure() only
  // reprograms the divider (BK: 26 MHz / 52 = 500000 exactly); begin() would
  // end() the port and reallocate the RX ring.
  auto *lt = static_cast<uart::LibreTinyUARTComponent *>(this->parent_);
  static_cast<SerialClass *>(lt->get_hw_serial())->configure(baud, lt->get_config());
#endif
}

void Th5Link::on_hello(const HelloInfo &h, const std::string &session) {
  static const char *const CHARGE[] = {"discharging", "charging", "full", "unknown"};
  static const char *const RESULT[] = {"none", "ok", "no_hello", "tuya", "net_fail", "cap", "link", "abort"};
  static const char *const TRIGGER[] = {"timer", "button", "units", "boot", "charge", "bench", "retry"};
  static const char *const RESET[] = {"POR", "BOR", "LVD", "WDT", "PIN", "SOFT", "LOCKUP", "OTHER"};
  if (this->temperature_ != nullptr)
    this->temperature_->publish_state(h.t_centi == LINK_T_NONE ? NAN : h.t_centi / 100.0f);
  if (this->humidity_ != nullptr)
    this->humidity_->publish_state(h.rh_centi == LINK_RH_NONE ? NAN : h.rh_centi / 100.0f);
  if (this->battery_level_ != nullptr)
    this->battery_level_->publish_state(h.batt_pct == LINK_PCT_NONE ? NAN : (float) h.batt_pct);
  if (this->battery_voltage_ != nullptr)
    this->battery_voltage_->publish_state(h.batt_mv ? (float) h.batt_mv : NAN);
  if (this->last_session_ != nullptr)
    this->last_session_->publish_state(h.last_session_ms ? (float) h.last_session_ms : NAN);
  if (this->fail_count_ != nullptr)
    this->fail_count_->publish_state(h.fail_count);
  if (this->units_ != nullptr)
    this->units_->publish_state(h.units == 1 ? "F" : "C");
  if (this->charge_state_ != nullptr)
    this->charge_state_->publish_state(CHARGE[h.charge < 4 ? h.charge : 3]);
  if (this->last_result_ != nullptr) {
    // The MCU's result of the previous session, plus a push failure this CBU
    // stored then (th5_done answers OK before the push; protocol.md 6.6).
    std::string r = h.last_result < 8 ? RESULT[h.last_result] : "unknown";
    if (this->state_.push_error[0])
      r += std::string("; ") + this->state_.push_error;
    this->last_result_->publish_state(r);
  }
  if (this->trigger_ != nullptr)
    this->trigger_->publish_state(h.trigger < 7 ? TRIGGER[h.trigger] : "unknown");
  if (this->reset_cause_ != nullptr) {
    std::string s;
    for (int i = 0; i < 8; i++) {
      if (h.reset_cause & (1 << i)) {
        if (!s.empty())
          s += ",";
        s += RESET[i];
      }
    }
    this->reset_cause_->publish_state(s.empty() ? "none" : s);
  }
  if (this->mcu_version_ != nullptr) {
    char buf[24];
    snprintf(buf, sizeof(buf), "%u.%u.%u", (unsigned) (h.fw_version >> 24), (unsigned) ((h.fw_version >> 16) & 0xFF),
             (unsigned) (h.fw_version & 0xFFFF));
    this->mcu_version_->publish_state(buf);
  }
  if (this->history_ != nullptr) {
    std::string value;
    if (!h.history.empty()) {
      char identity[19];
      snprintf(identity, sizeof(identity), "%s/%08" PRIx32 ";", session.substr(0, 8).c_str(), this->state_.boot_count);
      value = std::string(identity) + base64_encode(h.history);
    }
    this->history_->publish_state(value);
  }
  // Last: HA starts the session when this value changes.
  if (this->session_ != nullptr)
    this->session_->publish_state(session);
  // The push failure is now reported (published states are resent to HA on
  // subscription); forget it unless this session fails its own push.
  if (this->state_.push_error[0]) {
    this->state_.push_error[0] = 0;
    this->save_state_();
  }
#ifdef TH5_LINK_SAFE_MODE
  // A valid HELLO_RSP means this firmware boots and talks to the MCU: count the
  // boot as good now, so sessions the MCU ends (CAP, NET grace) do not walk
  // toward safe mode.
  if (this->safe_mode_ != nullptr && !this->forgotten_)
    this->safe_mode_->mark_successful();
#endif
}

void Th5Link::save_state_() {
  if (!this->forgotten_)
    this->pref_.save(&this->state_);
}

// Push failures reach HA as last_result next session (the radio is off during the
// push), and at once in bench/maintenance. Saved here, synced before BYE.
void Th5Link::save_push_error_(const char *what, const std::string &reason) {
  snprintf(this->state_.push_error, sizeof(this->state_.push_error), "%s push failed: %s", what, reason.c_str());
  if (this->last_result_ != nullptr)
    this->last_result_->publish_state(this->state_.push_error);
  this->save_state_();
}

void Th5Link::on_image_result(bool ok, const std::string &what) {
  if (ok) {
    this->state_.push_error[0] = 0;
    this->save_state_();
  } else {
    this->save_push_error_("frame", what);
  }
}

void Th5Link::on_firmware_result(bool ok, const std::string &what) {
  if (!ok)
    this->save_push_error_("firmware", what);
}

size_t Th5Link::free_heap() { return RAMAllocator<uint8_t>().get_free_heap_size(); }

void Th5Link::sync_prefs() {
  if (!this->forgotten_)
    global_preferences->sync();
}

// ESPHome has no per-component "forget": erase the whole preference store, as
// factory_reset does. The LibreTiny backend deinitialises its FlashDB in reset(),
// so nothing may write preferences for the rest of this boot.
void Th5Link::forget() {
  global_preferences->reset();
  this->forgotten_ = true;
}

void Th5Link::respond(uint32_t call_id, bool ok, const std::string &message) {
  if (!ok)
    ESP_LOGW(TAG, "action failed: %s", message.c_str());
  if (call_id == 0)
    return;
  api::global_api_server->send_action_response(call_id, ok, StringRef(message));
  api::global_api_server->unregister_active_action_call(call_id);
}

uint32_t Th5Link::random32() { return random_uint32(); }

void Th5Link::log(int level, const std::string &message) {
  if (level <= 1)
    ESP_LOGE(TAG, "%s", message.c_str());
  else if (level == 2)
    ESP_LOGW(TAG, "%s", message.c_str());
  else
    ESP_LOGI(TAG, "%s", message.c_str());
}

void Th5Link::on_finished(bool /*ok*/) { this->high_freq_.stop(); }

}  // namespace esphome::th5_link
