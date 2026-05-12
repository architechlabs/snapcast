#!/usr/bin/env python3
import asyncio
import logging
import signal
import time
from typing import Any

from aiohttp import web

from config import load_config, save_runtime_patch
from devices import list_audio_devices
from diagnostics import collect
from entities import EntityManager
from logging_utils import setup_logging
from pulseaudio import PulseAudioManager
from snapcast import SnapcastManager
from web import create_app

LOG = logging.getLogger("main")


class AudioHub:
    def __init__(self):
        self.config = load_config()
        setup_logging(self.config["diagnostics"]["log_level"])
        self.devices: dict[str, Any] = {}
        self.pulse = PulseAudioManager(self.config)
        self.snapcast = SnapcastManager(self.config)
        self.entities = EntityManager(self.config, self.handle_entity_command)
        self.web_runner: web.AppRunner | None = None
        self.status_cache: dict[str, Any] = {}
        self.restart_lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.last_capture_retry = 0.0

    async def run(self) -> None:
        await self.start_pipeline()
        await self.entities.start()
        await self.start_web()
        asyncio.create_task(self.health_loop())
        asyncio.create_task(self.snapcast_bridge_loop())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        await self.stopping.wait()
        await self.shutdown()

    async def start_pipeline(self) -> None:
        async with self.restart_lock:
            await self.wait_for_device_settle()
            self.devices = await list_audio_devices()
            await self.pulse.start(self.devices)
            await self.snapcast.start()
            self.last_capture_retry = time.monotonic()

    async def restart_pipeline(self) -> dict[str, Any]:
        async with self.restart_lock:
            await self.snapcast.stop()
            await self.pulse.stop()
            self.config = load_config()
            self.pulse = PulseAudioManager(self.config)
            self.snapcast = SnapcastManager(self.config)
            self.entities.config = self.config
            await self.wait_for_device_settle()
            self.devices = await list_audio_devices()
            await self.pulse.start(self.devices)
            await self.snapcast.start()
            self.last_capture_retry = time.monotonic()
        return {"ok": True}

    async def wait_for_device_settle(self) -> None:
        settle = int(self.config["diagnostics"].get("startup_device_settle_sec", 0))
        if settle > 0:
            await asyncio.sleep(settle)

    async def start_web(self) -> None:
        app = create_app(self.status, self.patch_config, self.restart_pipeline, self.retry_wired_input, self.reload_devices, self.remove_entities, self.input_level, self.monitor_clip, self.snapcast_action)
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
        self.web_runner = web.AppRunner(app, access_log=None)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "0.0.0.0", self.config["ui"]["port"])
        await site.start()
        LOG.info("web UI listening on %s", self.config["ui"]["port"])

    async def status(self) -> dict[str, Any]:
        if not self.status_cache:
            self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
        return self.status_cache

    async def health_loop(self) -> None:
        interval = self.config["diagnostics"]["health_interval_sec"]
        while not self.stopping.is_set():
            try:
                self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
                await self.entities.publish_status(self.status_cache)
                await self.ensure_processes()
            except Exception:
                LOG.exception("health loop failed")
            await asyncio.sleep(interval)

    async def snapcast_bridge_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                if self.snapcast.running() and self.pulse.health() == "running":
                    await self.snapcast.ensure_music_assistant_bridge()
            except Exception:
                LOG.exception("Music Assistant Snapcast bridge loop failed")
            await asyncio.sleep(2)

    async def ensure_processes(self) -> None:
        if self.restart_lock.locked():
            return
        if not self.snapcast.running() or self.pulse.health() != "running":
            LOG.warning("audio pipeline degraded; restarting")
            await self.restart_pipeline()
            return
        current_devices = self.status_cache.get("devices", {}) if self.status_cache else {}
        bridge = self.pulse.processes.get("wired-alsa-bridge")
        bridge_stopped = self.pulse.wired_capture_mode == "ffmpeg_alsa_bridge" and bridge and not bridge.running()
        needs_capture_retry = self.config["wired"]["enabled"] and current_devices.get("selected_capture") and (not self.pulse.wired_source_loaded or bridge_stopped)
        retry_interval = 120 if self.pulse.wired_busy else 45
        if needs_capture_retry and time.monotonic() - self.last_capture_retry > retry_interval:
            self.last_capture_retry = time.monotonic()
            LOG.info("capture device is present but not attached; retrying wired input without restarting Snapcast")
            await self.retry_wired_input()

    async def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        old_config = self.config
        self.config = save_runtime_patch(self.config, patch)
        if self.requires_pipeline_restart(old_config, self.config, patch):
            await self.restart_pipeline()
        else:
            self.pulse.config = self.config
            await self.apply_live_patch(patch)
            self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
        return await self.status()

    def requires_pipeline_restart(self, old_config: dict[str, Any], new_config: dict[str, Any], patch: dict[str, Any]) -> bool:
        restart_keys = {
            ("audio", "sample_rate"),
            ("audio", "channels"),
            ("audio", "format"),
            ("audio", "latency_ms"),
            ("audio", "routing_mode"),
            ("wired", "enabled"),
            ("wired", "device"),
            ("wired", "profile"),
            ("network", "enabled"),
            ("network", "tcp_pcm_enabled"),
            ("network", "tcp_pcm_port"),
            ("network", "rtp_enabled"),
            ("network", "rtp_port"),
            ("wireless", "bluetooth_enabled"),
            ("wireless", "bluetooth_pairable"),
            ("music_assistant", "ducking_enabled"),
            ("music_assistant", "ducking_level"),
        }
        for section, values in patch.items():
            if not isinstance(values, dict):
                return True
            for key in values:
                if (section, key) in restart_keys and old_config.get(section, {}).get(key) != new_config.get(section, {}).get(key):
                    return True
        return False

    async def apply_live_patch(self, patch: dict[str, Any]) -> None:
        if "wired" in patch and "volume" in patch["wired"]:
            await self.pulse.set_volume("wired", self.config["wired"]["volume"])
        if "music_assistant" in patch and "music_volume" in patch["music_assistant"]:
            await self.pulse.set_volume("music", self.config["music_assistant"]["music_volume"])
        if "network" in patch and "volume" in patch["network"]:
            await self.pulse.set_volume("network", self.config["network"]["volume"])
        if "wireless" in patch and "volume" in patch["wireless"]:
            await self.pulse.set_volume("bluetooth", self.config["wireless"]["volume"])

    async def reload_devices(self) -> dict[str, Any]:
        self.devices = await list_audio_devices()
        self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
        return {"ok": True, "devices": self.devices}

    async def retry_wired_input(self) -> dict[str, Any]:
        async with self.restart_lock:
            self.devices = await list_audio_devices()
            attached = await self.pulse.retry_wired_input(self.devices)
            self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
        return {"ok": attached, "attached": attached, "devices": self.devices, "error": self.pulse.wired_error}

    async def input_level(self) -> dict[str, Any]:
        return await self.pulse.input_level()

    async def monitor_clip(self) -> bytes:
        return await self.pulse.monitor_clip(3.0)

    async def snapcast_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if action == "use_final_stream":
            result = await self.snapcast.set_group_stream(payload.get("group_id"), self.config["snapcast"]["stream_name"])
        elif action == "use_ma_stream":
            result = await self.snapcast.set_group_stream(payload.get("group_id"), self.snapcast.bridge_status.get("ma_stream"))
        elif action == "mute_group":
            result = await self.snapcast.set_group_mute(payload.get("group_id"), bool(payload.get("muted", True)))
        elif action == "client_volume":
            result = await self.snapcast.set_client_volume(payload.get("client_id"), int(payload.get("volume", 50)), payload.get("muted"))
        else:
            result = {"ok": False, "error": f"unknown action {action}"}
        self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
        return result

    async def remove_entities(self) -> dict[str, Any]:
        return await self.entities.remove_discovery()

    async def handle_entity_command(self, message: dict[str, Any]) -> None:
        topic = message["topic"].rsplit("/", 1)[-1]
        payload = message["payload"].strip()
        patch: dict[str, Any] | None = None
        if topic == "wired_enabled":
            patch = {"wired": {"enabled": payload.upper() == "ON"}}
        elif topic == "ma_bridge_enabled":
            patch = {"music_assistant": {"enabled": payload.upper() == "ON"}}
        elif topic == "ma_auto_route":
            patch = {"music_assistant": {"auto_route_players": payload.upper() == "ON"}}
        elif topic == "ducking_enabled":
            patch = {"music_assistant": {"ducking_enabled": payload.upper() == "ON"}}
        elif topic == "network_enabled":
            patch = {"network": {"enabled": payload.upper() == "ON"}}
        elif topic == "bluetooth_enabled":
            patch = {"wireless": {"bluetooth_enabled": payload.upper() == "ON"}}
        elif topic == "routing_mode":
            patch = {"audio": {"routing_mode": payload}}
        elif topic == "latency_ms":
            patch = {"audio": {"latency_ms": int(float(payload))}}
        elif topic == "wired_volume":
            patch = {"wired": {"volume": float(payload) / 100.0}}
        elif topic == "music_volume":
            patch = {"music_assistant": {"music_volume": float(payload) / 100.0}}
        elif topic == "network_volume":
            patch = {"network": {"volume": float(payload) / 100.0}}
        elif topic == "restart_pipeline":
            await self.restart_pipeline()
            return
        elif topic == "remove_entities":
            await self.remove_entities()
            return
        if patch:
            await self.patch_config(patch)

    async def shutdown(self) -> None:
        LOG.info("shutting down")
        await self.entities.stop()
        await self.snapcast.stop()
        await self.pulse.stop()
        if self.web_runner:
            await self.web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(AudioHub().run())
