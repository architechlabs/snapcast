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

    rc, out = await run_checked(["arecord", "-l"], timeout=8)
    if rc == 0:
        capture = parse_arecord_cards(out)
    else:
        LOG.warning("arecord -l failed: %s", out.strip())

    rc, out = await run_checked(["aplay", "-l"], timeout=8)
    if rc == 0:
        playback = parse_arecord_cards(out)

    proc_cards = Path("/proc/asound/cards")
    if proc_cards.exists():
        cards = [line.rstrip() for line in proc_cards.read_text(errors="replace").splitlines() if line.strip()]

    rc, names = await run_checked(["arecord", "-L"], timeout=8)
    named_sources = [line.strip() for line in names.splitlines() if line and not line.startswith(" ")] if rc == 0 else []

    return {
        "cards": cards,
        "capture": capture,
        "playback": playback,
        "named_capture_sources": named_sources,
        "selected_capture": select_capture_device(capture, named_sources),
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
    for preferred in ("default", "pulse"):
        if preferred in named_sources:
            return preferred
    return None


async def wait_for_capture_device(seconds: int = 30) -> str | None:
    for _ in range(seconds):
        devices = await list_audio_devices()
        selected = devices.get("selected_capture")
        if selected:
            return selected
        await asyncio.sleep(1)
    return None

