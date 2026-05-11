#!/usr/bin/env python3
import asyncio
import logging
import signal
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

    async def run(self) -> None:
        await self.start_pipeline()
        await self.entities.start()
        await self.start_web()
        asyncio.create_task(self.health_loop())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        await self.stopping.wait()
        await self.shutdown()

    async def start_pipeline(self) -> None:
        async with self.restart_lock:
            self.devices = await list_audio_devices()
            await self.pulse.start(self.devices)
            await self.snapcast.start()

    async def restart_pipeline(self) -> dict[str, Any]:
        async with self.restart_lock:
            await self.snapcast.stop()
            await self.pulse.stop()
            self.config = load_config()
            self.pulse = PulseAudioManager(self.config)
            self.snapcast = SnapcastManager(self.config)
            self.entities.config = self.config
            self.devices = await list_audio_devices()
            await self.pulse.start(self.devices)
            await self.snapcast.start()
        return {"ok": True}

    async def start_web(self) -> None:
        app = create_app(self.status, self.patch_config, self.restart_pipeline, self.reload_devices, self.remove_entities)
        self.web_runner = web.AppRunner(app)
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

    async def ensure_processes(self) -> None:
        if self.restart_lock.locked():
            return
        if not self.snapcast.running() or self.pulse.health() != "running":
            LOG.warning("audio pipeline degraded; restarting")
            await self.restart_pipeline()

    async def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.config = save_runtime_patch(self.config, patch)
        await self.restart_pipeline()
        return await self.status()

    async def reload_devices(self) -> dict[str, Any]:
        self.devices = await list_audio_devices()
        self.status_cache = await collect(self.config, self.pulse, self.snapcast, self.entities)
        return {"ok": True, "devices": self.devices}

    async def remove_entities(self) -> dict[str, Any]:
        return await self.entities.remove_discovery()

    async def handle_entity_command(self, message: dict[str, Any]) -> None:
        topic = message["topic"].rsplit("/", 1)[-1]
        payload = message["payload"].strip()
        patch: dict[str, Any] | None = None
        if topic == "wired_enabled":
            patch = {"wired": {"enabled": payload.upper() == "ON"}}
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
