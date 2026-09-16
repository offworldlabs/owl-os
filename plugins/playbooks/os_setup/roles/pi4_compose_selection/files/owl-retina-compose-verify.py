#!/usr/bin/env python3
"""Fail closed unless effective Compose still selects the approved Pi 4 shape."""
import json
import re
import sys
from pathlib import Path


IMAGE = re.compile(r"(?:[a-zA-Z0-9][a-zA-Z0-9._:/-]*@)?sha256:[0-9a-f]{64}\Z")
RENDER = re.compile(r"/dev/dri/renderD[0-9]+\Z")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def device_ok(device):
    if isinstance(device, str):
        parts = device.split(":")
        return len(parts) >= 2 and RENDER.fullmatch(parts[0]) and parts[1] == "/dev/dri/renderD128"
    if isinstance(device, dict):
        return bool(RENDER.fullmatch(str(device.get("source", "")))) and device.get("target") == "/dev/dri/renderD128"
    return False


def verify_services(services, effective):
    require(isinstance(services, dict), "services must be an object")
    if effective:
        require("config-merger" in services, "config-merger missing from effective Compose")
    else:
        require(set(services) == {"blah2", "blah2_api"}, "literal override must contain only radar and API services")
    for name in ("blah2", "blah2_api"):
        service = services.get(name, {})
        require(isinstance(service, dict), f"{name} must be an object")
        require(bool(IMAGE.fullmatch(str(service.get("image", "")))), f"{name} does not select an immutable image")
        labels = service.get("labels", {})
        require(isinstance(labels, dict) and labels.get("io.offworldlabs.pi4-support") == "1", f"{name} lacks Pi 4 opt-in label")
        require(service.get("pull_policy") == "never", f"{name} may pull a different image")
    radar = services["blah2"]
    environment = radar.get("environment", {})
    require(isinstance(environment, dict), "radar environment must be an object")
    require(str(environment.get("OWL_GPU_FILTER_PERCENT")) == "50", "GPU percentage is not 50")
    require(environment.get("VK_ICD_FILENAMES") == "/usr/share/vulkan/icd.d/broadcom_icd.json", "Broadcom ICD selection missing")
    devices = radar.get("devices", [])
    require(isinstance(devices, list) and any(device_ok(x) for x in devices), "Pi 4 render device mapping missing")
    if effective:
        require(radar.get("container_name") == "blah2" and services["blah2_api"].get("container_name") == "blah2-api", "managed container names changed")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--override":
        # Paired blah2-arm renderer emits literal JSON (also valid YAML).
        compose = json.loads(Path(sys.argv[2]).read_text())
        verify_services(compose.get("services", {}), False)
    elif len(sys.argv) == 1:
        compose = json.load(sys.stdin)
        verify_services(compose.get("services", {}), True)
    else:
        raise ValueError("expected --override FILE or effective JSON on stdin")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Pi 4 Compose preflight: {exc}", file=sys.stderr)
        sys.exit(1)
