#!/usr/bin/env python3
import copy
import json
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")
RUNTIME_PATH = Path("/data/audio-hub/runtime_config.json")
FIXED_UI_PORT = 41888
SNAPCAST_PORT_MIGRATIONS = {
    "client_stream_port": (1704, 41704),
    "jsonrpc_port": (1705, 41705),
    "http_port": (1780, 41780),
}

DEFAULTS: dict[str, Any] = {
    "audio": {
        "sample_rate": 48000,
        "channels": 2,
        "format": "s16le",
        "latency_ms": 20,
        "buffer_ms": 80,
        "keepalive_silence": True,
        "routing_mode": "mix",
    },
    "wired": {
        "enabled": True,
        "device": "auto",
        "profile": "line_in",
        "capture_backend": "auto",
        "latency_ms": 8,
        "volume": 0.9,
        "mute": False,
    },
    "network": {
        "enabled": True,
        "tcp_pcm_enabled": True,
        "tcp_pcm_port": 5555,
        "rtp_enabled": False,
        "rtp_port": 5556,
        "volume": 0.85,
    },
    "wireless": {
        "bluetooth_enabled": False,
        "bluetooth_pairable": False,
        "volume": 0.85,
    },
    "snapcast": {
        "stream_name": "AudioHub",
        "client_stream_port": 41704,
        "jsonrpc_port": 41705,
        "http_port": 41780,
        "codec": "pcm",
        "buffer_ms": 120,
        "chunk_ms": 5,
    },
    "music_assistant": {
        "enabled": True,
        "stream_prefix": "MusicAssistant",
        "tap_client_id": "audio-hub-ma-tap",
        "tap_client_name": "Audio Hub Mix Input",
        "auto_route_players": True,
        "manage_tap_group_stream": False,
        "music_volume": 0.85,
        "mic_injection_enabled": True,
        "ducking_enabled": False,
        "ducking_level": 0.35,
        "low_latency_mode": True,
    },
    "entities": {
        "mqtt_enabled": True,
        "mqtt_discovery_prefix": "homeassistant",
        "mqtt_base_topic": "snapcast_audio_hub",
        "ha_rest_enabled": True,
    },
    "ui": {
        "port": 41888,
    },
    "diagnostics": {
        "log_level": "info",
        "health_interval_sec": 10,
        "startup_device_settle_sec": 4,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize(config: dict[str, Any]) -> dict[str, Any]:
    config["audio"]["sample_rate"] = int(config["audio"]["sample_rate"])
    config["audio"]["channels"] = int(config["audio"]["channels"])
    config["audio"]["latency_ms"] = int(config["audio"]["latency_ms"])
    config["audio"]["buffer_ms"] = int(config["audio"]["buffer_ms"])
    config["audio"]["keepalive_silence"] = bool(config["audio"].get("keepalive_silence", False))
    config["wired"]["volume"] = clamp(float(config["wired"]["volume"]), 0.0, 2.0)
    config["wired"]["latency_ms"] = int(config["wired"].get("latency_ms", 15))
    config["wired"]["latency_ms"] = max(8, min(config["wired"]["latency_ms"], 80))
    if config["wired"].get("capture_backend") not in ("auto", "direct_alsa", "haos_pulse"):
        config["wired"]["capture_backend"] = "auto"
    config["network"]["volume"] = clamp(float(config["network"]["volume"]), 0.0, 2.0)
    config["wireless"]["volume"] = clamp(float(config["wireless"]["volume"]), 0.0, 2.0)
    config["music_assistant"]["music_volume"] = clamp(float(config["music_assistant"].get("music_volume", 0.85)), 0.0, 2.0)
    config["music_assistant"]["ducking_level"] = clamp(float(config["music_assistant"].get("ducking_level", 0.35)), 0.0, 1.0)
    config["music_assistant"]["low_latency_mode"] = bool(config["music_assistant"].get("low_latency_mode", True))
    config["music_assistant"]["stream_prefix"] = str(config["music_assistant"].get("stream_prefix") or "MusicAssistant")
    config["music_assistant"]["tap_client_id"] = str(config["music_assistant"].get("tap_client_id") or "audio-hub-ma-tap")
    config["music_assistant"]["tap_client_name"] = str(config["music_assistant"].get("tap_client_name") or "Audio Hub Mix Input")
    config["music_assistant"]["manage_tap_group_stream"] = bool(config["music_assistant"].get("manage_tap_group_stream", False))
    if config["music_assistant"].get("enabled", True):
        config["audio"]["keepalive_silence"] = True
    for key in ("tcp_pcm_port", "rtp_port"):
        config["network"][key] = int(config["network"][key])
    for key in ("client_stream_port", "jsonrpc_port", "http_port", "buffer_ms"):
        config["snapcast"][key] = int(config["snapcast"][key])
    config["snapcast"]["chunk_ms"] = int(config["snapcast"].get("chunk_ms", 10))
    if config["music_assistant"]["low_latency_mode"]:
        config["audio"]["latency_ms"] = max(10, min(config["audio"]["latency_ms"], 20))
        config["audio"]["buffer_ms"] = max(60, min(config["audio"]["buffer_ms"], 100))
        config["snapcast"]["buffer_ms"] = max(80, min(config["snapcast"]["buffer_ms"], 120))
        config["snapcast"]["chunk_ms"] = max(5, min(config["snapcast"]["chunk_ms"], 10))
    for key, (old_port, new_port) in SNAPCAST_PORT_MIGRATIONS.items():
        if config["snapcast"][key] == old_port:
            config["snapcast"][key] = new_port
    config["ui"]["port"] = FIXED_UI_PORT
    config["diagnostics"]["startup_device_settle_sec"] = int(config["diagnostics"].get("startup_device_settle_sec", 4))
    return config


def load_config() -> dict[str, Any]:
    options = {}
    runtime = {}
    if OPTIONS_PATH.exists():
        options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    if RUNTIME_PATH.exists():
        runtime = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    return normalize(deep_merge(deep_merge(DEFAULTS, options), runtime))


def save_runtime_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = normalize(deep_merge(current, patch))
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    return merged
