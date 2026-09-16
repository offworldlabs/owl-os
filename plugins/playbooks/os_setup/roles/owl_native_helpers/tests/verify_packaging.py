#!/usr/bin/env python3
"""Portable checks for the packaged OWL native helper sources and callers."""
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROLE = Path(__file__).resolve().parent.parent
OS_SETUP = ROLE.parents[1]
RADAR_ROLE = OS_SETUP / "roles" / "radar_packages"
WIFI_ROLE = OS_SETUP / "roles" / "wifi_reconnect"

EXPECTED = {
    WIFI_ROLE / "files" / "wifi-reconnect": "5115ee07984baed5eedea8d8e53919119c6ad508c5e9fd18afa81a88bde0eb4d",
    RADAR_ROLE / "files" / "blah2_rspduo_restart.bash": "063e03a05ee8e0bfa8e0ae691c01e52c29ddfe6fd17d5550a00f0fbf4f1fa2a6",
    ROLE / "files" / "nm-wifi-healthy.cpp": "a7176e671da50b9ee6f02d4f52cc800c89d6691195f61e4f583233e019a50792",
    ROLE / "files" / "docker_metadata.c": "d375bdde93ed82a05d450dc841d1cdef221198c45da903eb96babd89ef2f7a43",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(value, message):
    if not value:
        raise AssertionError(message)


def recipe_binary(source, recipe):
    """A fixture stand-in for a compiler whose output depends on its recipe."""
    return hashlib.sha256(recipe + b"\0" + source).digest()


def content_aware_atomic_copy(source, destination):
    """Model Ansible copy's content comparison plus requested file mode."""
    content_changed = not destination.exists() or destination.read_bytes() != source
    if content_changed:
        temporary = destination.with_name(destination.name + ".new")
        temporary.write_bytes(source)
        os.replace(temporary, destination)
    before_mode = stat.S_IMODE(destination.stat().st_mode)
    os.chmod(destination, 0o755)
    return content_changed, before_mode != 0o755


def packaging_convergence_fixture():
    """An interrupted source update must converge when the role resumes."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        installed = root / "owl-helper"
        recipe_v1, recipe_v2 = b"cc -O2 -lfirst", b"cc -O2 -lsecond"
        source_v1, source_v2 = b"source version one", b"source version two"
        content_aware_atomic_copy(recipe_binary(source_v1, recipe_v1), installed)
        os.chmod(installed, 0o600)

        # Source copy can complete before an interrupted run reaches compilation.
        require(installed.read_bytes() == recipe_binary(source_v1, recipe_v1),
                "fixture did not preserve pre-interruption executable")

        # An unconditional resumed build sees the new source even though a
        # destination already exists, then the copy repairs content and mode.
        changed, metadata_changed = content_aware_atomic_copy(recipe_binary(source_v2, recipe_v1), installed)
        require(changed and metadata_changed and installed.read_bytes() == recipe_binary(source_v2, recipe_v1),
                "resumed build did not replace stale executable and mode")
        changed, metadata_changed = content_aware_atomic_copy(recipe_binary(source_v2, recipe_v1), installed)
        require(not changed and not metadata_changed, "unchanged copy was not idempotent")

        # Link/build-recipe changes also alter the resulting installed bytes.
        changed, _ = content_aware_atomic_copy(recipe_binary(source_v2, recipe_v2), installed)
        require(changed and installed.read_bytes() == recipe_binary(source_v2, recipe_v2),
                "build recipe change did not reach installed executable")


def main():
    for path, expected in EXPECTED.items():
        require(digest(path) == expected, f"selected fixture changed: {path}")
        require(not path.read_bytes().startswith(b"\x7fELF"), f"prebuilt executable committed: {path}")

    wifi_tasks = (WIFI_ROLE / "tasks" / "main.yml").read_text()
    radar_tasks = (RADAR_ROLE / "tasks" / "main.yml").read_text()
    native_tasks = (ROLE / "tasks" / "main.yml").read_text()
    playbook = (OS_SETUP / "main.yml").read_text()
    wifi_script = (WIFI_ROLE / "files" / "wifi-reconnect").read_text()
    watchdog = (RADAR_ROLE / "files" / "blah2_rspduo_restart.bash").read_text()

    require("src: wifi-reconnect" in wifi_tasks and "dest: /usr/local/sbin/wifi-reconnect" in wifi_tasks,
            "WiFi installed script does not use the selected fixture")
    require("src: blah2_rspduo_restart.bash" in radar_tasks and
            "dest: /opt/blah2/script/blah2_rspduo_restart.bash" in radar_tasks,
            "watchdog installed script does not use the selected fixture")
    require("- role: owl_native_helpers" in playbook, "native helper role is not in the OS build")
    for package in ("build-essential", "libsystemd0", "libsystemd-dev", "libcurl4", "libcurl4-openssl-dev",
                    "libjson-c5", "libjson-c-dev"):
        require(f"- {package}" in native_tasks, f"missing explicit dependency: {package}")
    for source in ("nm-wifi-healthy.cpp", "WifiPolicy.h", "docker_metadata.c"):
        require(source in native_tasks, f"source is not installed before build: {source}")
    for executable in ("owl-nm-wifi-healthy", "owl-docker-metadata"):
        require(f"/usr/local/libexec/{executable}" in native_tasks, f"missing installed helper: {executable}")
    require(".owl-nm-wifi-healthy.new" in native_tasks and ".owl-docker-metadata.new" in native_tasks,
            "helper builds do not stage a replacement")
    require("owl_native_helper_sources" not in native_tasks and "owl_native_helper_executables" not in native_tasks,
            "helper build still depends on transient task state")
    require("\n  when:" not in native_tasks and native_tasks.count("changed_when: false") == 2,
            "helper compiles must run unconditionally without reporting a build change")
    require("force: yes" not in native_tasks,
            "helper install must retain Ansible copy's content-aware idempotence")
    require("remote_src: yes" in native_tasks and "owner: root" in native_tasks and "mode: '0755'" in native_tasks,
            "helper installs are not root-owned executables")
    packaging_convergence_fixture()

    require("[ -x /usr/local/libexec/owl-nm-wifi-healthy ] && timeout 3 /usr/local/libexec/owl-nm-wifi-healthy" in wifi_script,
            "WiFi helper does not call the optional read-only probe")
    require("fast_devices=$(timeout 2 nmcli" in wifi_script and "if ethernet_connected; then" in wifi_script,
            "WiFi fallback recovery path was removed")
    require("timeout 2 /usr/local/libexec/owl-docker-metadata" in watchdog,
            "watchdog does not call the canonical Docker helper")
    require("HELPER_STATUS" in watchdog and "docker inspect -f '{{.RestartCount}} {{.State.StartedAt}}'" in watchdog,
            "watchdog CLI fallback was removed")
    require("/api/map-health" in watchdog and "/api/map | head -c23" in watchdog,
            "watchdog API fallback was removed")

    forbidden = ("/Users/", "/var/tmp/owl-", "owl-field-", "BEGIN PRIVATE", "password=", "token=")
    for path in EXPECTED:
        content = path.read_text()
        require(not any(value in content for value in forbidden), f"private path or credential in {path}")

    subprocess.run(["bash", "-n", str(WIFI_ROLE / "files" / "wifi-reconnect")], check=True)
    subprocess.run(["bash", "-n", str(RADAR_ROLE / "files" / "blah2_rspduo_restart.bash")], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        binary = Path(temporary) / "wifi-policy"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        str(ROLE / "tests" / "test_wifi_policy.cpp"), "-I", str(ROLE / "files"),
                        "-o", str(binary)], check=True)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        require('"pass":true' in result.stdout and '"cases":45' in result.stdout,
                "WiFi policy fixtures did not pass")

    fixture_env = dict(os.environ)
    fixture_env.setdefault("OWL_WIFI_TEST_WORKERS", "16")
    fixture_env.setdefault("OWL_WATCHDOG_TEST_WORKERS", "16")
    subprocess.run([sys.executable, str(ROLE / "tests" / "test_wifi_recovery.py")], check=True, env=fixture_env)
    subprocess.run([sys.executable, str(ROLE / "tests" / "test_watchdog_recovery.py")], check=True, env=fixture_env)

    docker_headers = (Path("/usr/include/curl/curl.h"), Path("/usr/include/json-c/json.h"))
    if all(path.exists() for path in docker_headers):
        subprocess.run([sys.executable, str(ROLE / "tests" / "test_docker_metadata.py")], check=True, env=fixture_env)
    else:
        print("Docker metadata protocol fixtures skipped: libcurl/json-c development headers unavailable")

    print("packaging fixtures passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
