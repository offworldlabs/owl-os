// Read-only eligibility probe. Any uncertainty lets the existing helper recover.
#include "WifiPolicy.h"
#include <systemd/sd-bus.h>
#include <unistd.h>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace {
constexpr const char* NM = "org.freedesktop.NetworkManager";
constexpr const char* ROOT = "/org/freedesktop/NetworkManager";
constexpr const char* DEV = "org.freedesktop.NetworkManager.Device";
constexpr const char* AC = "org.freedesktop.NetworkManager.Connection.Active";
constexpr const char* SYSTEMD = "org.freedesktop.systemd1";
struct Bus {
  sd_bus* value = nullptr;
  ~Bus() { sd_bus_unref(value); }
};
struct Message {
  sd_bus_message* value = nullptr;
  ~Message() { sd_bus_message_unref(value); }
};
bool stringProperty(sd_bus* bus, const char* service, const std::string& path,
                    const char* interface, const char* property, const char* type, std::string& value) {
  Message reply;
  if (sd_bus_get_property(bus, service, path.c_str(), interface, property, nullptr, &reply.value, type) < 0) return false;
  const char* raw = nullptr;
  if (sd_bus_message_read(reply.value, type, &raw) <= 0 || !raw) return false;
  value = raw;
  return true;
}
bool numberProperty(sd_bus* bus, const std::string& path, const char* interface,
                    const char* property, unsigned& value) {
  uint32_t raw = 0;
  if (sd_bus_get_property_trivial(bus, NM, path.c_str(), interface, property, nullptr, 'u', &raw) < 0) return false;
  value = raw;
  return true;
}
bool deviceState(sd_bus* bus, owl::Snapshot& value) {
  int managed = 0;
  if (!stringProperty(bus, NM, value.device, DEV, "Interface", "s", value.iface) ||
      !numberProperty(bus, value.device, DEV, "DeviceType", value.type) ||
      !numberProperty(bus, value.device, DEV, "State", value.state) ||
      !stringProperty(bus, NM, value.device, DEV, "ActiveConnection", "o", value.active) ||
      sd_bus_get_property_trivial(bus, NM, value.device.c_str(), DEV, "Managed", nullptr, 'b', &managed) < 0) return false;
  value.managed = managed;
  return value.ok = true;
}
bool activeState(sd_bus* bus, owl::Snapshot& value) {
  if (!stringProperty(bus, NM, value.active, AC, "Id", "s", value.id) ||
      !stringProperty(bus, NM, value.active, AC, "Type", "s", value.ac_type) ||
      !numberProperty(bus, value.active, AC, "State", value.ac_state)) return false;
  Message reply;
  if (sd_bus_get_property(bus, NM, value.active.c_str(), AC, "Devices", nullptr, &reply.value, "ao") < 0 ||
      sd_bus_message_enter_container(reply.value, 'a', "o") <= 0) return false;
  const char* path = nullptr;
  int result;
  while ((result = sd_bus_message_read(reply.value, "o", &path)) > 0) {
    if (!path || value.devices.size() >= 2) return false;
    value.devices.emplace_back(path);
  }
  return result == 0 && sd_bus_message_exit_container(reply.value) >= 0;
}
bool portalState(sd_bus* bus, owl::Snapshot& value) {
  Message reply;
  if (sd_bus_call_method(bus, SYSTEMD, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
      "GetUnit", nullptr, &reply.value, "s", "wifi-connect.service") < 0) return false;
  const char* path = nullptr;
  if (sd_bus_message_read(reply.value, "o", &path) <= 0 || !path) return false;
  return value.systemd_read = stringProperty(bus, SYSTEMD, path, "org.freedesktop.systemd1.Unit",
                                             "ActiveState", "s", value.service);
}
}
int main() {
  // The caller treats timeout like every other failure and uses nmcli recovery.
  alarm(2);
  try {
    std::vector<std::string> interfaces;
    for (const auto& item : std::filesystem::directory_iterator("/sys/class/net")) {
      if (std::filesystem::is_directory(item.path() / "wireless")) interfaces.push_back(item.path().filename().string());
    }
    if (interfaces.size() != 1 || !owl::interfaceName(interfaces[0])) return 1;
    Bus bus;
    if (sd_bus_open_system(&bus.value) < 0 || sd_bus_set_method_call_timeout(bus.value, 100000) < 0) return 1;
    Message reply;
    if (sd_bus_call_method(bus.value, NM, ROOT, NM, "GetDeviceByIpIface", nullptr,
                           &reply.value, "s", interfaces[0].c_str()) < 0) return 1;
    const char* path = nullptr;
    if (sd_bus_message_read(reply.value, "o", &path) <= 0 || !path) return 1;
    owl::Snapshot before;
    before.device = path;
    if (!owl::numberedPath(before.device, std::string(ROOT) + "/Devices/") || !deviceState(bus.value, before) ||
        before.iface != interfaces[0] || before.type != 2 || before.state != 100 || !before.managed ||
        !owl::numberedPath(before.active, std::string(ROOT) + "/ActiveConnection/") || !activeState(bus.value, before)) return 1;
    auto after = before;
    if (!deviceState(bus.value, after) || !portalState(bus.value, before)) return 1;
    return owl::healthy(interfaces, before, after) ? 0 : 1;
  } catch (...) {
    return 1;
  }
}
