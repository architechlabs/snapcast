#!/usr/bin/env python3
import asyncio
import json
import logging
import os
from collections.abc import Callable, Awaitable
from typing import Any

import paho.mqtt.client as mqtt

from process import run_checked

LOG = logging.getLogger("entities")

CommandHandler = Callable[[dict[str, Any]], Awaitable[None]]


class EntityManager:
    def __init__(self, config: dict, command_handler: CommandHandler):
        self.config = config
        self.command_handler = command_handler
        self.client: mqtt.Client | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.last_status: dict[str, Any] = {}
        self.last_ha_error = ""

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self.config["entities"]["mqtt_enabled"]:
            await self._start_mqtt()

    async def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None

    def health(self) -> str:
        if self.client:
            return "mqtt"
        if self.config["entities"].get("ha_rest_enabled"):
            return "ha_rest"
        return "disabled"

    async def _start_mqtt(self) -> None:
        host = os.environ.get("MQTT_HOST")
        if not host:
            LOG.info("MQTT service not injected; MQTT discovery is disabled")
            return
        port = int(os.environ.get("MQTT_PORT", "1883"))
        username = os.environ.get("MQTT_USERNAME")
        password = os.environ.get("MQTT_PASSWORD")
        client = mqtt.Client(client_id="snapcast_audio_hub", protocol=mqtt.MQTTv311)
        if username:
            client.username_pw_set(username, password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(host, port, 30)
        client.loop_start()
        self.client = client

    def _on_connect(self, client, userdata, flags, rc):
        LOG.info("MQTT connected with rc=%s", rc)
        base = self.config["entities"]["mqtt_base_topic"]
        client.subscribe(f"{base}/cmd/#")
        self.publish_discovery()

    def _on_message(self, client, userdata, message):
        if not self.loop:
            return
        topic = message.topic
        payload = message.payload.decode(errors="replace")
        self.loop.call_soon_threadsafe(asyncio.create_task, self.command_handler({"topic": topic, "payload": payload}))

    def publish_discovery(self) -> None:
        if not self.client:
            return
        prefix = self.config["entities"]["mqtt_discovery_prefix"]
        base = self.config["entities"]["mqtt_base_topic"]
        device = {
            "identifiers": ["snapcast_audio_hub"],
            "name": "Snapcast Audio Hub",
            "manufacturer": "Local Add-on",
            "model": "HAOS Audio Hub",
        }
        entities = [
            ("sensor", "pipeline", {"name": "Pipeline", "value_template": "{{ value_json.health.pipeline }}"}),
            ("sensor", "active_source", {"name": "Active Source", "value_template": "{{ value_json.health.active_source }}"}),
            ("sensor", "wired_input", {"name": "Wired Input", "value_template": "{{ value_json.health.wired_input }}"}),
            ("sensor", "snapcast", {"name": "Snapcast", "value_template": "{{ value_json.health.snapcast }}"}),
            ("sensor", "music_assistant_bridge", {"name": "Music Assistant Bridge", "value_template": "{{ value_json.health.music_assistant }}"}),
            ("switch", "wired_enabled", {"name": "Wired Input", "state_topic": f"{base}/state", "value_template": "{{ 'ON' if value_json.config.wired.enabled else 'OFF' }}", "command_topic": f"{base}/cmd/wired_enabled"}),
            ("switch", "ma_bridge_enabled", {"name": "MA Music Bridge", "state_topic": f"{base}/state", "value_template": "{{ 'ON' if value_json.config.music_assistant.enabled else 'OFF' }}", "command_topic": f"{base}/cmd/ma_bridge_enabled"}),
            ("switch", "ma_auto_route", {"name": "MA Auto Route Players", "state_topic": f"{base}/state", "value_template": "{{ 'ON' if value_json.config.music_assistant.auto_route_players else 'OFF' }}", "command_topic": f"{base}/cmd/ma_auto_route"}),
            ("switch", "ducking_enabled", {"name": "Mic Ducking", "state_topic": f"{base}/state", "value_template": "{{ 'ON' if value_json.config.music_assistant.ducking_enabled else 'OFF' }}", "command_topic": f"{base}/cmd/ducking_enabled"}),
            ("switch", "network_enabled", {"name": "Network Input", "state_topic": f"{base}/state", "value_template": "{{ 'ON' if value_json.config.network.enabled else 'OFF' }}", "command_topic": f"{base}/cmd/network_enabled"}),
            ("switch", "bluetooth_enabled", {"name": "Bluetooth Input", "state_topic": f"{base}/state", "value_template": "{{ 'ON' if value_json.config.wireless.bluetooth_enabled else 'OFF' }}", "command_topic": f"{base}/cmd/bluetooth_enabled"}),
            ("select", "routing_mode", {"name": "Routing Mode", "state_topic": f"{base}/state", "value_template": "{{ value_json.config.audio.routing_mode }}", "command_topic": f"{base}/cmd/routing_mode", "options": ["mix", "wired_only", "network_only", "bluetooth_only", "fallback_duck", "source_switch"]}),
            ("number", "latency_ms", {"name": "Latency", "state_topic": f"{base}/state", "value_template": "{{ value_json.config.audio.latency_ms }}", "command_topic": f"{base}/cmd/latency_ms", "min": 10, "max": 1000, "step": 5, "mode": "slider", "unit_of_measurement": "ms"}),
            ("number", "wired_volume", {"name": "Wired Volume", "state_topic": f"{base}/state", "value_template": "{{ (value_json.config.wired.volume * 100) | round(0) }}", "command_topic": f"{base}/cmd/wired_volume", "min": 0, "max": 200, "step": 1, "mode": "slider", "unit_of_measurement": "%"}),
            ("number", "music_volume", {"name": "Music Volume", "state_topic": f"{base}/state", "value_template": "{{ (value_json.config.music_assistant.music_volume * 100) | round(0) }}", "command_topic": f"{base}/cmd/music_volume", "min": 0, "max": 200, "step": 1, "mode": "slider", "unit_of_measurement": "%"}),
            ("number", "network_volume", {"name": "Network Volume", "state_topic": f"{base}/state", "value_template": "{{ (value_json.config.network.volume * 100) | round(0) }}", "command_topic": f"{base}/cmd/network_volume", "min": 0, "max": 200, "step": 1, "mode": "slider", "unit_of_measurement": "%"}),
            ("button", "restart_pipeline", {"name": "Restart Pipeline", "command_topic": f"{base}/cmd/restart_pipeline"}),
            ("button", "remove_entities", {"name": "Remove Audio Hub Entities", "command_topic": f"{base}/cmd/remove_entities"}),
        ]
        for platform, object_id, payload in entities:
            payload = {
                **payload,
                "unique_id": f"snapcast_audio_hub_{object_id}",
                "object_id": f"snapcast_audio_hub_{object_id}",
                "state_topic": payload.get("state_topic", f"{base}/state"),
                "availability_topic": f"{base}/availability",
                "device": device,
            }
            topic = f"{prefix}/{platform}/snapcast_audio_hub/{object_id}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)
        self.client.publish(f"{base}/availability", "online", retain=True)

    async def remove_discovery(self) -> dict[str, Any]:
        if not self.client:
            return {"ok": False, "reason": "mqtt unavailable"}
        prefix = self.config["entities"]["mqtt_discovery_prefix"]
        base = self.config["entities"]["mqtt_base_topic"]
        topics = [
            ("sensor", "pipeline"),
            ("sensor", "active_source"),
            ("sensor", "wired_input"),
            ("sensor", "snapcast"),
            ("sensor", "music_assistant_bridge"),
            ("switch", "wired_enabled"),
            ("switch", "ma_bridge_enabled"),
            ("switch", "ma_auto_route"),
            ("switch", "ducking_enabled"),
            ("switch", "network_enabled"),
            ("switch", "bluetooth_enabled"),
            ("select", "routing_mode"),
            ("number", "latency_ms"),
            ("number", "wired_volume"),
            ("number", "music_volume"),
            ("number", "network_volume"),
            ("button", "restart_pipeline"),
            ("button", "remove_entities"),
        ]
        for platform, object_id in topics:
            self.client.publish(f"{prefix}/{platform}/snapcast_audio_hub/{object_id}/config", "", retain=True)
        self.client.publish(f"{base}/availability", "offline", retain=True)
        return {"ok": True}

    async def list_media_players(self) -> list[dict[str, Any]]:
        self.last_ha_error = ""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            self.last_ha_error = "Home Assistant API token is unavailable"
            return []
        headers = ["-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json"]
        rc, out = await run_checked(["curl", "-fsS", *headers, "http://supervisor/core/api/states"], timeout=8)
        if rc != 0:
            self.last_ha_error = out.strip() or "Home Assistant states API request failed"
            return []
        try:
            states = json.loads(out)
        except json.JSONDecodeError:
            self.last_ha_error = "Home Assistant states API returned invalid JSON"
            return []
        players = []
        for item in states if isinstance(states, list) else []:
            entity_id = str(item.get("entity_id") or "")
            if not entity_id.startswith("media_player."):
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            players.append({
                "entity_id": entity_id,
                "name": attributes.get("friendly_name") or entity_id,
                "state": item.get("state") or "unknown",
                "integration": attributes.get("device_class") or attributes.get("source") or "",
                "supported_features": attributes.get("supported_features", 0),
            })
        return players

    async def play_media_player(self, entity_id: str | None, media_url: str | None, content_type: str = "music") -> dict[str, Any]:
        if not entity_id:
            return {"ok": False, "error": "missing media_player entity_id"}
        if not media_url:
            return {"ok": False, "error": "missing media URL"}
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return {"ok": False, "error": "Home Assistant API token is unavailable"}
        body = json.dumps({
            "entity_id": entity_id,
            "media_content_id": media_url,
            "media_content_type": content_type,
        })
        headers = ["-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json"]
        rc, out = await run_checked(["curl", "-fsS", "-X", "POST", *headers, "-d", body, "http://supervisor/core/api/services/media_player/play_media"], timeout=10)
        if rc != 0:
            return {"ok": False, "error": out.strip() or "media_player.play_media failed"}
        return {"ok": True, "entity_id": entity_id, "media_url": media_url}

    async def stop_media_player(self, entity_id: str | None) -> dict[str, Any]:
        if not entity_id:
            return {"ok": False, "error": "missing media_player entity_id"}
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return {"ok": False, "error": "Home Assistant API token is unavailable"}
        body = json.dumps({"entity_id": entity_id})
        headers = ["-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json"]
        rc, out = await run_checked(["curl", "-fsS", "-X", "POST", *headers, "-d", body, "http://supervisor/core/api/services/media_player/media_stop"], timeout=10)
        if rc != 0:
            return {"ok": False, "error": out.strip() or "media_player.media_stop failed"}
        return {"ok": True, "entity_id": entity_id}

    async def publish_status(self, status: dict[str, Any]) -> None:
        self.last_status = status
        base = self.config["entities"]["mqtt_base_topic"]
        if self.client:
            self.client.publish(f"{base}/state", json.dumps(status), retain=True)
        if self.config["entities"].get("ha_rest_enabled"):
            await self._publish_ha_rest(status)

    async def _publish_ha_rest(self, status: dict[str, Any]) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return
        api = "http://supervisor/core/api/states"
        headers = ["-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json"]
        states = {
            "sensor.snapcast_audio_hub_pipeline": status["health"]["pipeline"],
            "sensor.snapcast_audio_hub_active_source": status["health"]["active_source"],
            "sensor.snapcast_audio_hub_wired_input": status["health"]["wired_input"],
            "sensor.snapcast_audio_hub_snapcast": status["health"]["snapcast"],
            "sensor.snapcast_audio_hub_music_assistant_bridge": status["health"]["music_assistant"],
        }
        for entity_id, state in states.items():
            body = json.dumps({"state": state, "attributes": {"friendly_name": entity_id.replace("_", " ").title(), "status": status}})
            await run_checked(["curl", "-fsS", "-X", "POST", *headers, "-d", body, f"{api}/{entity_id}"], timeout=5)
