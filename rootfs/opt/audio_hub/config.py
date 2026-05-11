#!/usr/bin/env python3
import copy
import json
from pathlib import Path
from typing import Any

OPTIONS_PATH = Path("/data/options.json")
RUNTIME_PATH = Path("/data/audio-hub/runtime_config.json")
FIXED_UI_PORT = 41888

DEFAULTS: dict[str, Any] = {
    "audio": {
        "sample_rate": 48000,
        "channels": 2,
        "format": "s16le",
        "latency_ms": 120,
        "buffer_ms": 400,
        "routing_mode": "mix",
    },
    "wired": {
        "enabled": True,
        "device": "auto",
        "profile": "line_in",
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
        "client_stream_port": 1704,
        "jsonrpc_port": 1705,
        "http_port": 1780,
        "codec": "pcm",
        "buffer_ms": 1000,
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
    config["wired"]["volume"] = clamp(float(config["wired"]["volume"]), 0.0, 2.0)
    config["network"]["volume"] = clamp(float(config["network"]["volume"]), 0.0, 2.0)
    config["wireless"]["volume"] = clamp(float(config["wireless"]["volume"]), 0.0, 2.0)
    for key in ("tcp_pcm_port", "rtp_port"):
        config["network"][key] = int(config["network"][key])
    for key in ("client_stream_port", "jsonrpc_port", "http_port", "buffer_ms"):
        config["snapcast"][key] = int(config["snapcast"][key])
    config["ui"]["port"] = FIXED_UI_PORT
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
