#!/usr/bin/env python3
import asyncio
from pathlib import Path

from devices import list_audio_devices
from process import run_checked
from pulseaudio import PULSE_ENV
from wireless import bluetooth_status


async def collect(config: dict, pulse, snapcast, entities) -> dict:
    devices_task = asyncio.create_task(list_audio_devices())
    bt_task = asyncio.create_task(bluetooth_status())
    pulse_info_task = asyncio.create_task(run_checked(["pactl", "info"], timeout=5, env=PULSE_ENV))

    devices = await devices_task
    bt = await bt_task
    pulse_rc, pulse_out = await pulse_info_task

    snap_status = "running" if snapcast.running() else "stopped"
    pulse_status = pulse.health() if pulse else "stopped"
    active_source = infer_active_source(config)
    health = {
        "pipeline": "running" if snap_status == "running" and pulse_status == "running" else "degraded",
        "snapcast": snap_status,
        "pulse": pulse_status if pulse_rc == 0 else "unavailable",
        "active_source": active_source,
        "wired_input": wired_input_state(devices, pulse),
        "input_message": input_message(devices),
        "capture_mode": getattr(pulse, "wired_capture_mode", "none") if pulse else "none",
        "capture_error": getattr(pulse, "wired_error", "") if pulse else "",
        "haos_audio_error": getattr(pulse, "host_pulse_error", "") if pulse else "",
        "network_input": "enabled" if config["network"]["enabled"] else "disabled",
        "bluetooth_input": "enabled" if config["wireless"]["bluetooth_enabled"] else "disabled",
        "entities": entities.health() if entities else "disabled",
    }
    return {
        "summary": f"{health['pipeline']} / {active_source}",
        "config": config,
        "health": health,
        "devices": devices,
        "bluetooth": bt,
        "pulse_info": pulse_out if pulse_rc == 0 else "",
        "fifo_exists": Path("/tmp/audio-hub/snapcast.pcm").exists(),
    }


def infer_active_source(config: dict) -> str:
    mode = config["audio"]["routing_mode"]
    if mode == "mix":
        enabled = []
        if config["wired"]["enabled"]:
            enabled.append("wired")
        if config["network"]["enabled"]:
            enabled.append("network")
        if config["wireless"]["bluetooth_enabled"]:
            enabled.append("bluetooth")
        return "+".join(enabled) if enabled else "none"
    return mode


def input_message(devices: dict) -> str:
    notes = devices.get("notes") or []
    if notes:
        return notes[0]
    if devices.get("selected_capture"):
        return f"Using {devices['selected_capture']}"
    return "No capture input detected."


def wired_input_state(devices: dict, pulse) -> str:
    if not devices.get("selected_capture"):
        return devices.get("input_capability", "missing")
    if getattr(pulse, "wired_source_loaded", False):
        return "attached"
    if getattr(pulse, "wired_busy", False):
        return "device_busy"
    return "detected_not_attached"
