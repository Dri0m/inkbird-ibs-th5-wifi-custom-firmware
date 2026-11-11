#pragma once
// ESPHome glue for the th5_link engine: UART, sensors, the HA actions, the
// learned-AP fallback and the orderly shutdown. The protocol lives in
// th5_engine.{h,cpp}.

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/core/preferences.h"
#include "esphome/core/string_ref.h"
#include "esphome/components/uart/uart.h"
#include "esphome/components/api/api_server.h"
#include "esphome/components/api/user_services.h"
#include "esphome/components/ota/ota_backend.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"
#ifdef TH5_LINK_SAFE_MODE
#include "esphome/components/safe_mode/safe_mode.h"
#endif

#include "th5_engine.h"

namespace esphome::th5_link {

class Th5Link;

// The HA actions (design.md 3 step 7, 4.7), registered from C++ with
// supports_response: status.
class ConfigAction final : public api::UserServiceBase<StringRef> {
 public:
  ConfigAction(Th5Link *parent);

 protected:
  void execute(uint32_t call_id, bool return_response, StringRef json) override;
  Th5Link *parent_;
};

class FrameAction final : public api::UserServiceBase<int32_t, StringRef> {
 public:
  FrameAction(Th5Link *parent, bool firmware);

 protected:
  void execute(uint32_t call_id, bool return_response, int32_t offset, StringRef data) override;
  Th5Link *parent_;
  bool firmware_;
};

class DoneAction final : public api::UserServiceBase<> {
 public:
  DoneAction(Th5Link *parent);

 protected:
  void execute(uint32_t call_id, bool return_response) override;
  Th5Link *parent_;
};

class Th5Link : public Component, public uart::UARTDevice, public th5link::Host, public ota::OTAGlobalStateListener {
 public:
  Th5Link();
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_powerdown() override;
  // After the UART bus, before Wi-Fi: HELLO goes out as early as possible.
  float get_setup_priority() const override { return setup_priority::DATA; }

  void set_fast_baud(bool v) { this->opts_.fast_baud = v; }
  void set_wifi_timeout(uint32_t ms) { this->opts_.wifi_timeout_ms = ms; }
  void set_api_timeout(uint32_t ms) { this->opts_.api_timeout_ms = ms; }
  void set_ha_timeout(uint32_t ms) { this->opts_.ha_timeout_ms = ms; }
  void set_api_disconnect_timeout(uint32_t ms) { this->api_disconnect_timeout_ms_ = ms; }
  void set_session_timeout(uint32_t ms) { this->opts_.session_timeout_ms = ms; }
#ifdef TH5_LINK_SAFE_MODE
  void set_safe_mode(safe_mode::SafeModeComponent *s) { this->safe_mode_ = s; }
#endif
  void set_temperature_sensor(sensor::Sensor *s) { this->temperature_ = s; }
  void set_humidity_sensor(sensor::Sensor *s) { this->humidity_ = s; }
  void set_battery_level_sensor(sensor::Sensor *s) { this->battery_level_ = s; }
  void set_battery_voltage_sensor(sensor::Sensor *s) { this->battery_voltage_ = s; }
  void set_last_session_sensor(sensor::Sensor *s) { this->last_session_ = s; }
  void set_fail_count_sensor(sensor::Sensor *s) { this->fail_count_ = s; }
  void set_history_text_sensor(text_sensor::TextSensor *s) { this->history_ = s; }
  void set_wifi_join_text_sensor(text_sensor::TextSensor *s) { this->wifi_join_ = s; }
  void set_session_text_sensor(text_sensor::TextSensor *s) { this->session_ = s; }
  void set_units_text_sensor(text_sensor::TextSensor *s) { this->units_ = s; }
  void set_charge_state_text_sensor(text_sensor::TextSensor *s) { this->charge_state_ = s; }
  void set_reset_cause_text_sensor(text_sensor::TextSensor *s) { this->reset_cause_ = s; }
  void set_last_result_text_sensor(text_sensor::TextSensor *s) { this->last_result_ = s; }
  void set_trigger_text_sensor(text_sensor::TextSensor *s) { this->trigger_ = s; }
  void set_mcu_version_text_sensor(text_sensor::TextSensor *s) { this->mcu_version_ = s; }

  // From the actions.
  void on_config_action(uint32_t call_id, StringRef json);
  void on_data_action(uint32_t call_id, bool firmware, int32_t offset, StringRef data);
  void on_done_action(uint32_t call_id);

  // th5link::Host
  void uart_write(const uint8_t *data, size_t len) override;
  void uart_set_baud(uint32_t baud) override;
  void on_hello(const th5link::HelloInfo &h, const std::string &session) override;
  void begin_shutdown(bool urgent) override;
  void sync_prefs() override;
  void forget() override;
  void respond(uint32_t call_id, bool ok, const std::string &message) override;
  uint32_t random32() override;
  uint32_t boot_count() override { return this->state_.boot_count; }
  void on_image_result(bool ok, const std::string &what) override;
  void on_firmware_result(bool ok, const std::string &what) override;
  size_t free_heap() override;
  void log(int level, const std::string &message) override;
  void on_finished(bool ok) override;

  void on_ota_global_state(ota::OTAState state, float progress, uint8_t error, ota::OTAComponent *comp) override;

 protected:
  void watch_network_(uint32_t now);
  void clear_ap_pin_();
  void radio_off_();
  void shutdown_step_(uint32_t now);
  void save_push_error_(const char *what, const std::string &reason);

  th5link::Options opts_;
  th5link::Engine engine_;
  HighFrequencyLoopRequester high_freq_;
  ConfigAction config_action_;
  FrameAction frame_action_;
  FrameAction firmware_action_;
  DoneAction done_action_;
  std::vector<uint8_t> chunk_;

  // Learned AP: ESPHome's own fast_connect cache (BSSID + channel). A pin that
  // has not joined within PIN_TIMEOUT_MS of boot is cleared (and flushed), and
  // the station restarts unpinned, so a stale pin costs one slower session.
  static constexpr uint32_t PIN_TIMEOUT_MS = 3000;
  enum WifiJoin : uint8_t { JOIN_SCAN, JOIN_PINNED, JOIN_FALLBACK };
  WifiJoin wifi_join_state_{JOIN_SCAN};
  bool wifi_reenable_{false};
  ESPPreferenceObject wifi_pin_pref_;
  bool wifi_seen_{false};
  bool api_up_{false};
  bool forgotten_{false};

  // Persisted across boots (one preference, synced with the others before BYE).
  struct PersistState {
    uint32_t boot_count;
    char push_error[60];  // the last frame or firmware push failure, reported next session
  } __attribute__((packed));
  PersistState state_{};
  ESPPreferenceObject pref_;
  void save_state_();

  enum ShutdownState : uint8_t { SD_NONE, SD_DELAY, SD_API, SD_WIFI, SD_DONE };
  ShutdownState sd_state_{SD_NONE};
  uint32_t sd_at_{0};
  uint32_t api_disconnect_timeout_ms_{500};
  static constexpr uint32_t WIFI_OFF_WAIT_MS = 300;
  static constexpr uint32_t DEAUTH_WAIT_MS = 500;

#ifdef TH5_LINK_SAFE_MODE
  safe_mode::SafeModeComponent *safe_mode_{nullptr};
#endif
  sensor::Sensor *temperature_{nullptr};
  sensor::Sensor *humidity_{nullptr};
  sensor::Sensor *battery_level_{nullptr};
  sensor::Sensor *battery_voltage_{nullptr};
  sensor::Sensor *last_session_{nullptr};
  sensor::Sensor *fail_count_{nullptr};
  text_sensor::TextSensor *history_{nullptr};
  text_sensor::TextSensor *wifi_join_{nullptr};
  text_sensor::TextSensor *session_{nullptr};
  text_sensor::TextSensor *units_{nullptr};
  text_sensor::TextSensor *charge_state_{nullptr};
  text_sensor::TextSensor *reset_cause_{nullptr};
  text_sensor::TextSensor *last_result_{nullptr};
  text_sensor::TextSensor *trigger_{nullptr};
  text_sensor::TextSensor *mcu_version_{nullptr};
};

}  // namespace esphome::th5_link
