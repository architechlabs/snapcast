#!/usr/bin/env python3
import asyncio
import logging
import re
from pathlib import Path

from process import run_checked

LOG = logging.getLogger("devices")


async def list_audio_devices() -> dict:
    cards = []
    capture = []
    playback = []
    pcm_capture = []
    dev_snd = list_dev_snd()
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
    card_ids = {}
    if proc_cards.exists():
        proc_cards_text = proc_cards.read_text(errors="replace")
        cards = [line.rstrip() for line in proc_cards_text.splitlines() if line.strip()]
        card_ids = parse_proc_card_ids(proc_cards_text)

    proc_pcm = Path("/proc/asound/pcm")
    if proc_pcm.exists():
        pcm_capture = parse_proc_pcm(proc_pcm.read_text(errors="replace"))
        capture = merge_devices(capture, pcm_capture)
    capture = enrich_card_ids(capture, card_ids)

    rc, names = await run_checked(["arecord", "-L"], timeout=8)
    named_sources = [line.strip() for line in names.splitlines() if line and not line.startswith(" ")] if rc == 0 else []
    capture = annotate_capture_candidates(capture, named_sources)

    model = read_text_first([
        "/proc/device-tree/model",
        "/sys/firmware/devicetree/base/model",
    ])
    notes = device_notes(model, capture, playback)
    permissions = permission_notes(dev_snd, capture)

    return {
        "cards": cards,
        "model": model,
        "dev_snd": dev_snd,
        "capture": capture,
        "pcm_capture": pcm_capture,
        "playback": playback,
        "named_capture_sources": named_sources,
        "selected_capture": select_capture_device(capture, named_sources),
        "has_capture": bool(capture),
        "input_capability": "capture_available" if capture else "no_capture_device",
        "notes": notes + permissions,
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
        match = re.match(r"card\s+(\d+):\s*([^,]+).*device\s+(\d+):\s*(.*)", line)
        if not match:
            continue
        card, card_name, device, desc = match.groups()
        devices.append({
            "card": card,
            "card_name": card_name.strip(),
            "device": device,
            "alsa": f"hw:{card},{device}",
            "plughw": f"plughw:{card},{device}",
            "description": desc.strip(),
            "source": "arecord",
        })
    return devices


def parse_proc_pcm(output: str) -> list[dict]:
    devices = []
    for line in output.splitlines():
        if "capture" not in line.lower():
            continue
        match = re.match(r"(\d+)-(\d+):\s*(.*?)\s*:\s*(.*?)\s*:\s*capture", line, re.IGNORECASE)
        if not match:
            continue
        card, device, card_name, desc = match.groups()
        devices.append({
            "card": card,
            "card_name": card_name.strip(),
            "device": device,
            "alsa": f"hw:{card},{device}",
            "plughw": f"plughw:{card},{device}",
            "description": desc.strip(),
            "source": "proc_asound_pcm",
        })
    return devices


def merge_devices(primary: list[dict], fallback: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for device in primary + fallback:
        key = (device.get("card"), device.get("device"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(device)
    return merged


def parse_proc_card_ids(output: str) -> dict[str, str]:
    ids = {}
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+\[([^\]]+)\]\s*:", line)
        if match:
            ids[match.group(1)] = match.group(2).strip()
    return ids


def enrich_card_ids(capture: list[dict], card_ids: dict[str, str]) -> list[dict]:
    enriched = []
    for device in capture:
        item = dict(device)
        card = str(item.get("card", ""))
        item["card_id"] = card_ids.get(card) or safe_card_id(str(item.get("card_name", "")))
        enriched.append(item)
    return enriched


def select_capture_device(capture: list[dict], named_sources: list[str]) -> str | None:
    if capture:
        return capture[0].get("plughw") or capture[0]["alsa"]
    return None


def annotate_capture_candidates(capture: list[dict], named_sources: list[str]) -> list[dict]:
    annotated = []
    for device in capture:
        enriched = dict(device)
        enriched["candidates"] = capture_candidates_for_device(enriched, named_sources)
        annotated.append(enriched)
    return annotated


def capture_candidates_for_device(device: dict, named_sources: list[str]) -> list[str]:
    card = str(device.get("card", "")).strip()
    dev = str(device.get("device", "0")).strip()
    card_id = str(device.get("card_id", "")).strip()
    candidates = [
        device.get("plughw"),
        f"plughw:{card},{dev}" if card else None,
        f"dsnoop:CARD={card_id},DEV={dev}" if card_id else None,
        f"dsnoop:CARD={card},DEV={dev}" if card else None,
        f"default:CARD={card_id}" if card_id else None,
        f"sysdefault:CARD={card_id}" if card_id else None,
        device.get("alsa"),
        f"hw:{card},{dev}" if card else None,
    ]

    if card_id:
        for source in named_sources:
            if source_has_card(source, card_id, card) and capture_source_is_usable(source) and source not in candidates:
                candidates.append(source)
    seen = set()
    result = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def safe_card_id(card_name: str) -> str:
    base = card_name.split("[", 1)[0].strip() or card_name.strip()
    base = base.split(" ", 1)[0].strip()
    if re.match(r"^[A-Za-z0-9_-]+$", base):
        return base
    return ""


def source_has_card(source: str, card_id: str, card_number: str) -> bool:
    return f"CARD={card_id}" in source or f"CARD={card_number}" in source


def capture_source_is_usable(source: str) -> bool:
    if " " in source:
        return False
    return source.startswith((
        "plughw:",
        "dsnoop:",
        "default:",
        "sysdefault:",
        "usbstream:",
    ))


def list_dev_snd() -> list[dict]:
    root = Path("/dev/snd")
    if not root.exists():
        return []
    entries = []
    for item in sorted(root.iterdir()):
        try:
            stat = item.stat()
            mode = oct(stat.st_mode & 0o777)
        except OSError:
            mode = "unknown"
        entries.append({"name": item.name, "path": str(item), "mode": mode})
    return entries


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
        notes.append(f"Capture device detected: {capture[0].get('plughw') or capture[0]['alsa']} ({capture[0]['description']}).")
    return notes


def permission_notes(dev_snd: list[dict], capture: list[dict]) -> list[str]:
    notes = []
    if not dev_snd:
        notes.append("The add-on cannot see /dev/snd. Rebuild with full hardware access enabled and restart Home Assistant Supervisor if needed.")
    elif not any(entry["name"].startswith("pcmC") and "c" in entry["name"] for entry in dev_snd) and not capture:
        notes.append("/dev/snd is visible, but no capture PCM node such as pcmC1D0c is exposed to the add-on.")
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
