#!/usr/bin/env python3
import logging

from process import run_checked

LOG = logging.getLogger("wireless")


async def bluetooth_status() -> dict:
    rc, out = await run_checked(["bluetoothctl", "show"], timeout=5)
    if rc != 0:
        return {"available": False, "message": out.strip()}
    status = {"available": True}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        status[key.strip().lower().replace(" ", "_")] = value.strip()
    devices_rc, devices_out = await run_checked(["bluetoothctl", "devices", "Connected"], timeout=5)
    status["connected_devices"] = devices_out.strip().splitlines() if devices_rc == 0 else []
    return status

