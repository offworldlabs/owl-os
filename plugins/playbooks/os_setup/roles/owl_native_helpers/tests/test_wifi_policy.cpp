#include "WifiPolicy.h"
#include <iostream>
#include <stdexcept>

int main() {
  unsigned cases = 0;
  const auto check = [&](bool value) { ++cases; if (!value) throw std::runtime_error("Policy case failed"); };
  owl::Snapshot s;
  s.ok = s.managed = s.systemd_read = true;
  s.iface = "wlan0";
  s.device = "/org/freedesktop/NetworkManager/Devices/3";
  s.active = "/org/freedesktop/NetworkManager/ActiveConnection/4";
  s.id = "home"; s.type = 2; s.state = 100; s.ac_state = 2;
  s.ac_type = "802-11-wireless"; s.devices = {s.device}; s.service = "inactive";
  const auto good = [&](const owl::Snapshot& a, const owl::Snapshot& b) { return owl::healthy({"wlan0"}, a, b); };
  check(good(s, s));
  check(!owl::healthy({}, s, s)); check(!owl::healthy({"wlan0", "wlan1"}, s, s));
  check(!owl::healthy({"wlan1"}, s, s)); check(!owl::healthy({"wlan0;bad"}, s, s));
  check(!owl::interfaceName("")); check(!owl::interfaceName(std::string(16, 'a')));
  check(owl::interfaceName("wlp2s0.1"));
  for (const auto& id : {"", "node-setup", "--"}) { auto x = s; x.id = id; check(!good(x, x)); }
  for (const auto& id : {"a b", "a:b", "a\\b", "a\"b", "node-setup-other", "café"}) {
    auto x = s; x.id = id; check(good(x, x));
  }
  auto x = s; x.ok = false; check(!good(x, s)); check(!good(s, x));
  x = s; x.systemd_read = false; check(!good(x, x));
  x = s; x.service = "active"; check(!good(x, x));
  x = s; x.service = ""; check(!good(x, x));
  for (const auto& state : {"activating", "failed", "deactivating", "inactive"}) {
    x = s; x.service = state; check(good(x, x));
  }
  x = s; x.state = 30; check(!good(x, x)); check(!good(s, x));
  x = s; x.type = 1; check(!good(x, x)); check(!good(s, x));
  x = s; x.managed = false; check(!good(x, x)); check(!good(s, x));
  x = s; x.iface = "wlan1"; check(!good(x, x)); check(!good(s, x));
  x = s; x.active = "/"; check(!good(x, x)); check(!good(s, x));
  x = s; x.active += "1"; check(!good(s, x));
  x = s; x.device += "1"; check(!good(s, x));
  x = s; x.device = "/tmp/3"; check(!good(x, x));
  x = s; x.active += "../"; check(!good(x, x));
  x = s; x.ac_state = 1; check(!good(x, x));
  x = s; x.ac_type = "bridge"; check(!good(x, x));
  x = s; x.devices.clear(); check(!good(x, x));
  x = s; x.devices.push_back(s.device); check(!good(x, x));
  x = s; x.devices[0] += "1"; check(!good(x, x));
  std::cout << "{\"pass\":true,\"cases\":" << cases << "}\n";
}
