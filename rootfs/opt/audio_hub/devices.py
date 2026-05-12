#!/usr/bin/env python3
import asyncio
import logging
from pathlib import Path

from process import run_checked

LOG = logging.getLogger("devices")


async def list_audio_devices() -> dict:
    cards = []
    capture = []
    playback = []
    arecord_raw = ""
    aplay_raw = ""

    rc, out = await run_checked(["arecord", "-l"], timeout=8)
    arecord_raw = out
    if rc == 0:
        capture = parse_arecord_cards(out)
    else:
        LOG.warning("arecord -l failed: %s", out.strip())

    rc, out = await run_checked(["aplay", "-l"], timeout=8)
    aplay_raw = out
    if rc == 0:
        playback = parse_arecord_cards(out)

    proc_cards = Path("/proc/asound/cards")
    if proc_cards.exists():
        cards = [line.rstrip() for line in proc_cards.read_text(errors="replace").splitlines() if line.strip()]

    rc, names = await run_checked(["arecord", "-L"], timeout=8)
    named_sources = [line.strip() for line in names.splitlines() if line and not line.startswith(" ")] if rc == 0 else []

    model = read_text_first([
        "/proc/device-tree/model",
        "/sys/firmware/devicetree/base/model",
    ])
    notes = device_notes(model, capture, playback)

    return {
        "cards": cards,
        "model": model,
        "capture": capture,
        "playback": playback,
        "named_capture_sources": named_sources,
        "selected_capture": select_capture_device(capture, named_sources),
        "has_capture": bool(capture),
        "input_capability": "capture_available" if capture else "no_capture_device",
        "notes": notes,
        "recommended_hardware": recommended_hardware(capture),
        "raw": {
            "arecord_l": arecord_raw.strip(),
            "aplay_l": aplay_raw.strip(),
        },
    }


def parse_arecord_cards(output: str) -> list[dict]:
    devices = []
    for line in output.splitlines():
        if not line.startswith("card "):
            continue
        left, _, desc = line.partition(":")
        parts = left.replace("card ", "").split(",")
        if len(parts) < 2:
            continue
        card = parts[0].strip()
        device = parts[1].replace("device", "").strip()
        devices.append({
            "card": card,
            "device": device,
            "alsa": f"hw:{card},{device}",
            "description": desc.strip(),
        })
    return devices


def select_capture_device(capture: list[dict], named_sources: list[str]) -> str | None:
    if capture:
        return capture[0]["alsa"]
    return None


def read_text_first(paths: list[str]) -> str:
    for path in paths:
        candidate = Path(path)
        if candidate.exists():
            return candidate.read_text(errors="replace").replace("\x00", "").strip()
    return "unknown"


def device_notes(model: str, capture: list[dict], playback: list[dict]) -> list[str]:
    notes = []
    model_lower = model.lower()
    if "raspberry pi" in model_lower and not capture:
        notes.append("The Raspberry Pi built-in 3.5 mm jack is output-only. It cannot detect or record a microphone, AUX, or line-in signal.")
    if playback and not capture:
        notes.append("Playback hardware is present, but Linux reports no capture-capable ALSA device.")
    if capture:
        notes.append(f"Capture device detected: {capture[0]['alsa']} ({capture[0]['description']}).")
    return notes


def recommended_hardware(capture: list[dict]) -> list[str]:
    if capture:
        return []
    return [
        "Use a USB audio interface with a microphone/line input.",
        "Use a USB microphone.",
        "Use an I2S/ADC HAT such as Raspberry Pi Codec Zero when HAOS exposes it as an ALSA capture device.",
    ]


async def wait_for_capture_device(seconds: int = 30) -> str | None:
    for _ in range(seconds):
        devices = await list_audio_devices()
        selected = devices.get("selected_capture")
        if selected:
            return selected
        await asyncio.sleep(1)
    return None
