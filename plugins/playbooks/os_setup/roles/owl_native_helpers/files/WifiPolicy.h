#pragma once
#include <algorithm>
#include <string>
#include <vector>

namespace owl {
inline bool interfaceName(const std::string& name) {
  return !name.empty() && name.size() <= 15 && std::all_of(name.begin(), name.end(), [](unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
      (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.';
  });
}
inline bool numberedPath(const std::string& path, const std::string& prefix) {
  return path.compare(0, prefix.size(), prefix) == 0 && path.size() > prefix.size() &&
    std::all_of(path.begin() + prefix.size(), path.end(), [](unsigned char c) { return c >= '0' && c <= '9'; });
}
struct Snapshot {
  bool ok = false, managed = false, systemd_read = false;
  unsigned type = 0, state = 0, ac_state = 0;
  std::string iface, device, active, id, ac_type, service;
  std::vector<std::string> devices;
};
inline bool healthy(const std::vector<std::string>& interfaces, const Snapshot& a, const Snapshot& b) {
  if (interfaces.size() != 1 || !interfaceName(interfaces[0]) || a.iface != interfaces[0]) return false;
  if (!a.ok || !b.ok || !a.systemd_read || a.service.empty() || a.service == "active") return false;
  if (!numberedPath(a.device, "/org/freedesktop/NetworkManager/Devices/") ||
      !numberedPath(a.active, "/org/freedesktop/NetworkManager/ActiveConnection/")) return false;
  if (a.id.empty() || a.id == "node-setup" || a.id == "--") return false;
  if (a.type != 2 || a.state != 100 || !a.managed || a.ac_state != 2 || a.ac_type != "802-11-wireless") return false;
  if (a.devices.size() != 1 || a.devices[0] != a.device) return false;
  return a.iface == b.iface && a.device == b.device && a.active == b.active &&
    a.type == b.type && a.state == b.state && a.managed == b.managed;
}
}
