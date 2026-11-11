// Host shim for the th5_link engine: a line protocol on stdin/stdout, so the
// Python test (test_th5_link.py) can drive the C++ engine against the Python MCU
// model (tools/th5link/mcumodel.py) in virtual time.
//
//   in:  <cmd> <ms> [args...]          one command per line
//   out: zero or more event lines, then "next <ms>" (or "next -1"), then "."
//
// Commands: start | feed <hex> | tick | wifi | api <0|1> <rssi> | netfail <state> <detail> | haact
//           | ota | radiooff | config <call> <utc> <off> <next_utc> <next_off> <tags|->
//           | frame <call> <offset> <base64|-> | firmware <call> <offset> <base64|-> | done <call>
//           | crc32 <hex> | b64 <text>   (helpers; ms ignored)
// tags: tag:value pairs separated by commas, e.g. 1:1,2:60,8:2
// Options: --fast --wifi-timeout --api-timeout --ha-timeout --session-timeout (ms)
//          --heap <bytes>: what free_heap() reports (default: plenty)
// Events: tx <hex> | baud <n> | hello <session> <flags> | shutdown [urgent] | sync | forget
//         | image <0|1> <message> | fw <0|1> <message>
//         | respond <call> <0|1> <message> | finished <0|1> | log <level> <message>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "th5_engine.h"

using namespace th5link;

static std::string hex(const uint8_t *p, size_t n) {
  static const char *d = "0123456789abcdef";
  std::string s;
  for (size_t i = 0; i < n; i++) {
    s += d[p[i] >> 4];
    s += d[p[i] & 15];
  }
  return s;
}

static std::vector<uint8_t> unhex(const std::string &s) {
  std::vector<uint8_t> out;
  for (size_t i = 0; i + 1 < s.size(); i += 2)
    out.push_back((uint8_t) strtoul(s.substr(i, 2).c_str(), nullptr, 16));
  return out;
}

struct TestHost : Host {
  std::vector<std::string> ev;
  uint32_t rnd{0x1234ABCDu};
  size_t heap{200000};
  void uart_write(const uint8_t *d, size_t n) override { ev.push_back("tx " + hex(d, n)); }
  void uart_set_baud(uint32_t b) override { ev.push_back("baud " + std::to_string(b)); }
  void on_hello(const HelloInfo &h, const std::string &session) override {
    ev.push_back("hello " + session + " " + std::to_string(h.flags));
    ev.push_back("history " + hex(h.history.data(), h.history.size()));
  }
  void begin_shutdown(bool urgent) override { ev.push_back(urgent ? "shutdown urgent" : "shutdown"); }
  uint32_t boot_count() override { return 7; }
  void on_image_result(bool ok, const std::string &m) override {
    ev.push_back(std::string("image ") + (ok ? "1 " : "0 ") + m);
  }
  void on_firmware_result(bool ok, const std::string &m) override {
    ev.push_back(std::string("fw ") + (ok ? "1 " : "0 ") + m);
  }
  size_t free_heap() override { return heap; }
  void sync_prefs() override { ev.push_back("sync"); }
  void forget() override { ev.push_back("forget"); }
  void respond(uint32_t call, bool ok, const std::string &m) override {
    ev.push_back("respond " + std::to_string(call) + " " + (ok ? "1 " : "0 ") + m);
  }
  uint32_t random32() override { return rnd = rnd * 1664525u + 1013904223u; }
  void log(int level, const std::string &m) override { ev.push_back("log " + std::to_string(level) + " " + m); }
  void on_finished(bool ok) override { ev.push_back(std::string("finished ") + (ok ? "1" : "0")); }
};

int main(int argc, char **argv) {
  Options o;
  size_t heap = 200000;
  for (int i = 1; i + 1 < argc; i += 2) {
    std::string k = argv[i];
    uint32_t v = (uint32_t) strtoul(argv[i + 1], nullptr, 10);
    if (k == "--fast")
      o.fast_baud = v != 0;
    else if (k == "--wifi-timeout")
      o.wifi_timeout_ms = v;
    else if (k == "--api-timeout")
      o.api_timeout_ms = v;
    else if (k == "--ha-timeout")
      o.ha_timeout_ms = v;
    else if (k == "--session-timeout")
      o.session_timeout_ms = v;
    else if (k == "--heap")
      heap = v;
  }
  TestHost host;
  host.heap = heap;
  Engine eng(&host, o);
  std::string line;
  uint32_t now = 0;
  while (std::getline(std::cin, line)) {
    std::istringstream in(line);
    std::string cmd;
    long long ms = 0;
    in >> cmd >> ms;
    now = (uint32_t) ms;
    if (cmd == "start") {
      eng.start(now);
    } else if (cmd == "feed") {
      std::string h;
      in >> h;
      auto b = unhex(h);
      eng.feed(b.data(), b.size(), now);
    } else if (cmd == "tick") {
      eng.tick(now);
    } else if (cmd == "wifi") {
      eng.wifi_connected(now);
    } else if (cmd == "api") {
      int up = 0, rssi = 0;
      in >> up >> rssi;
      eng.api_connected(up != 0, (int8_t) rssi, now);
    } else if (cmd == "netfail") {
      int st = 0, det = 0;
      in >> st >> det;
      eng.net_failed((uint8_t) st, (uint8_t) det, now);
    } else if (cmd == "haact") {
      eng.ha_activity(now);
    } else if (cmd == "ota") {
      eng.ota_started(now);
    } else if (cmd == "radiooff") {
      eng.radio_off(now);
    } else if (cmd == "config") {
      ConfigRequest c;
      uint32_t call;
      long long utc, off, nutc, noff;
      std::string tags;
      in >> call >> utc >> off >> nutc >> noff >> tags;
      c.utc = (uint32_t) utc;
      c.offset_min = (int32_t) off;
      c.next_change_utc = (uint32_t) nutc;
      c.next_offset_min = (int32_t) noff;
      if (tags != "-") {
        std::istringstream ts(tags);
        std::string kv;
        while (std::getline(ts, kv, ',')) {
          size_t colon = kv.find(':');
          c.set((uint8_t) atoi(kv.substr(0, colon).c_str()), (int32_t) atol(kv.substr(colon + 1).c_str()));
        }
      }
      int ack = 0;
      in >> ack;
      c.history_ack = ack != 0;
      eng.action_config(call, c, now);
    } else if (cmd == "frame" || cmd == "firmware") {
      uint32_t call;
      long long off;
      std::string b64;
      in >> call >> off >> b64;
      std::vector<uint8_t> buf(b64.size() / 4 * 3 + 3);
      size_t n = 0;
      if (b64 == "-" || !base64_decode(b64.c_str(), b64.size(), buf.data(), buf.size(), n))
        host.respond(call, false, "data: not base64");
      else if (cmd == "frame")
        eng.action_frame(call, (int32_t) off, buf.data(), n, now);
      else
        eng.action_firmware(call, (int32_t) off, buf.data(), n, now);
    } else if (cmd == "done") {
      uint32_t call;
      in >> call;
      eng.action_done(call, now);
    } else if (cmd == "crc32") {
      std::string h;
      in >> h;
      auto b = unhex(h);
      char buf[16];
      snprintf(buf, sizeof(buf), "%08x", (unsigned) crc32(b.data(), b.size()));
      host.ev.push_back(std::string("crc32 ") + buf);
    } else if (cmd == "b64") {
      std::string t;
      in >> t;
      uint8_t buf[256];
      size_t n = 0;
      bool ok = base64_decode(t.c_str(), t.size(), buf, sizeof(buf), n);
      host.ev.push_back(ok ? "b64 " + hex(buf, n) : "b64 ERR");
    } else {
      std::cerr << "unknown command: " << line << "\n";
      return 2;
    }
    for (auto &e : host.ev)
      std::cout << e << "\n";
    host.ev.clear();
    uint32_t when = 0;
    if (eng.next_deadline(now, when))
      std::cout << "next " << when << "\n";
    else
      std::cout << "next -1\n";
    std::cout << ".\n" << std::flush;
  }
  return 0;
}
