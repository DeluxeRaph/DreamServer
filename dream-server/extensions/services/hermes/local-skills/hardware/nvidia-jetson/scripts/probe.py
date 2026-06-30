#!/usr/bin/env python3
from __future__ import annotations

import json
import platform
import subprocess
import urllib.request


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, timeout=5).strip()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


out = {
    "container_arch": platform.machine(),
    "container_kernel": platform.platform(),
}

try:
    with urllib.request.urlopen(
        "http://dream-x402-gateway:4020/v1/health/runtime?probe=true",
        timeout=30,
    ) as response:
        out["dream_runtime"] = json.load(response)
except Exception as exc:
    out["dream_runtime_error"] = f"{type(exc).__name__}: {exc}"

out["memtotal"] = run(["grep", "MemTotal", "/proc/meminfo"])
out["device_tree_model"] = run(
    ["sh", "-lc", "cat /proc/device-tree/model 2>/dev/null | tr '\\0' '\\n' || true"]
)
out["nv_tegra_release"] = run(["sh", "-lc", "cat /etc/nv_tegra_release 2>/dev/null || true"])

print(json.dumps(out, indent=2))
