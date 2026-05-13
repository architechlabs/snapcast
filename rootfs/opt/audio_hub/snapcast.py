#!/usr/bin/env python3
import asyncio
import json
import logging
from pathlib import Path

from process import ManagedProcess, run_checked
from pulseaudio import FIFO, PULSE_ENV

LOG = logging.getLogger("snapcast")
CONFIG_PATH = Path("/tmp/audio-hub/snapserver.conf")


class SnapcastManager:
    def __init__(self, config: dict):
        self.config = config
        self.process: ManagedProcess | None = None
        self.tap_process: ManagedProcess | None = None
        self.tap_loopback_module: str | None = None
        self.bridge_lock = asyncio.Lock()
        self.tap_retry_after = 0.0
        self.tap_error = ""
        self.tap_name_applied = False
        self.ma_idle_since = 0.0
        self.music_loopback_muted = False
        self.bridge_status: dict = {
            "enabled": bool(config.get("music_assistant", {}).get("enabled", True)),
            "state": "stopped",
            "ma_stream": None,
            "final_stream": config.get("snapcast", {}).get("stream_name", "AudioHub"),
            "tap_client": None,
            "tap_group": None,
            "groups": [],
            "clients": [],
            "error": "",
        }

    async def start(self) -> None:
        await self.stop()
        cfg = self.config
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        source = (
            f"pipe://{FIFO}"
            f"?name={cfg['snapcast']['stream_name']}"
            f"&mode=read"
            f"&sampleformat={cfg['audio']['sample_rate']}:{bits_from_format(cfg['audio']['format'])}:{cfg['audio']['channels']}"
            f"&codec={cfg['snapcast']['codec']}"
            f"&chunk_ms={cfg['snapcast'].get('chunk_ms', 10)}"
            f"&buffer={cfg['snapcast']['buffer_ms']}"
        )
        CONFIG_PATH.write_text(
            "\n".join([
                "[server]",
                f"threads = -1",
                "mdns = false",
                f"buffer = {cfg['snapcast']['buffer_ms']}",
                "",
                "[http]",
                "enabled = true",
                f"bind_to_address = 0.0.0.0",
                f"port = {cfg['snapcast']['http_port']}",
                "",
                "[tcp-control]",
                "enabled = true",
                f"bind_to_address = 0.0.0.0",
                f"port = {cfg['snapcast']['jsonrpc_port']}",
                "",
                "[tcp-streaming]",
                f"bind_to_address = 0.0.0.0",
                f"port = {cfg['snapcast']['client_stream_port']}",
                "",
                "[stream]",
                f"source = {source}",
                f"buffer = {cfg['snapcast']['buffer_ms']}",
                "",
            ]),
            encoding="utf-8",
        )
        quiet = [
            "(Avahi) Failed to create client",
            "(AsioStream) No data since",
            "(AsioStream) Error reading message: End of file",
            "(Server) onResync",
            "(ControlServer) New connection from:",
            "(ControlServer) Removing",
            "(ControlSessionHTTP) ControlSessionHttp::on_read error: bad method",
            "(ControlSessionTCP) Error while reading from control socket: End of file",
            "(StreamServer) StreamServer::NewConnection:",
            "(StreamSessionTCP) unknown message type received:",
            "(StreamServer) onDisconnect:",
            '"data":"Stream not found"',
        ]
        command = [
            "snapserver",
            "--config",
            str(CONFIG_PATH),
            "--server.mdns=false",
            f"--buffer={cfg['snapcast']['buffer_ms']}",
            f"--streamBuffer={cfg['snapcast'].get('chunk_ms', 5)}",
            f"--stream.buffer={cfg['snapcast']['buffer_ms']}",
        ]
        self.process = ManagedProcess("snapserver", command, quiet_substrings=quiet)
        await self.process.start()
        await asyncio.sleep(0.4)
        if not self.process.running():
            LOG.warning("snapserver rejected --server.mdns=false; retrying with config-only mDNS disable")
            self.process = ManagedProcess(
                "snapserver",
                [
                    "snapserver",
                    "--config",
                    str(CONFIG_PATH),
                    f"--buffer={cfg['snapcast']['buffer_ms']}",
                    f"--streamBuffer={cfg['snapcast'].get('chunk_ms', 5)}",
                ],
                quiet_substrings=quiet,
            )
            await self.process.start()
        await asyncio.sleep(0.4)
        if not self.process.running():
            LOG.warning("snapserver rejected explicit low-latency CLI flags; retrying with config-only settings")
            self.process = ManagedProcess("snapserver", ["snapserver", "--config", str(CONFIG_PATH)], quiet_substrings=quiet)
            await self.process.start()

    async def stop(self) -> None:
        await self.stop_music_assistant_tap()
        if self.process:
            await self.process.stop()
            self.process = None

    def running(self) -> bool:
        return self.process is not None and self.process.running()

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        payload = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params or {}})
        host = "127.0.0.1"
        port = str(self.config["snapcast"]["jsonrpc_port"])
        command = ["bash", "-lc", f"printf '%s\\n' '{payload}' | nc -w 2 {host} {port}"]
        rc, out = await run_checked(command, timeout=5)
        if rc != 0 or not out.strip():
            return {"ok": False, "error": out.strip()}
        try:
            return json.loads(out.splitlines()[-1])
        except json.JSONDecodeError:
            return {"ok": False, "raw": out}

    async def server_status(self) -> dict:
        return await self.rpc("Server.GetStatus")

    async def ensure_music_assistant_bridge(self) -> dict:
        async with self.bridge_lock:
            return await self._ensure_music_assistant_bridge()

    async def _ensure_music_assistant_bridge(self) -> dict:
        ma_cfg = self.config.get("music_assistant", {})
        if not ma_cfg.get("enabled", True):
            await self.stop_music_assistant_tap()
            self.bridge_status = {**self.bridge_status, "enabled": False, "state": "disabled", "error": ""}
            return self.bridge_status
        status = await self.server_status()
        server = extract_server(status)
        if not server:
            await self.disable_tap_audio()
            self.bridge_status = {**self.bridge_status, "enabled": True, "state": "snapserver_unavailable", "error": status.get("error") or status.get("raw") or "Snapserver status unavailable"}
            return self.bridge_status

        final_stream = find_stream(server, self.config["snapcast"]["stream_name"])
        ma_stream = find_music_assistant_stream(server, ma_cfg.get("stream_prefix", "MusicAssistant"), final_stream)
        ma_stream_state = stream_state(server, ma_stream)
        final_stream_state = stream_state(server, final_stream)
        tap_started = await self.ensure_tap_process()
        await asyncio.sleep(0.25)
        status = await self.server_status()
        server = extract_server(status) or server
        final_stream = find_stream(server, self.config["snapcast"]["stream_name"])
        ma_stream = find_music_assistant_stream(server, ma_cfg.get("stream_prefix", "MusicAssistant"), final_stream)
        ma_stream_state = stream_state(server, ma_stream)
        final_stream_state = stream_state(server, final_stream)
        tap_client = find_tap_client(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap"))
        if tap_client and not self.tap_name_applied:
            await self.set_client_name(client_id(tap_client), ma_cfg.get("tap_client_name", "Audio Hub Mix Input"))
            self.tap_name_applied = True
        groups = summarize_groups(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap"))
        clients = summarize_clients(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap"))
        user_client_count = sum(1 for client in clients if not client.get("internal_tap"))
        if not tap_started:
            await self.disable_tap_audio()
            self.bridge_status = {
                "enabled": True,
                "state": "tap_retry_wait",
                "ma_stream": ma_stream,
                "ma_stream_state": ma_stream_state,
                "final_stream": final_stream,
                "final_stream_state": final_stream_state,
                "tap_client": client_id(tap_client),
                "tap_group": group_id(find_group_for_client(server, tap_client)),
                "user_client_count": user_client_count,
                "groups": groups,
                "clients": clients,
                "error": self.tap_error or "Audio Hub Mix Input snapclient is waiting before retrying",
            }
            return self.bridge_status
        if not ma_stream:
            await self.disable_tap_audio()
            await self.set_music_loopback_muted(True)
            self.bridge_status = {
                "enabled": True,
                "state": "waiting_for_snapcast_players" if user_client_count == 0 else "waiting_for_music",
                "ma_stream": None,
                "ma_stream_state": None,
                "final_stream": final_stream,
                "final_stream_state": final_stream_state,
                "tap_client": client_id(tap_client),
                "tap_group": group_id(find_group_for_client(server, tap_client)),
                "user_client_count": user_client_count,
                "groups": groups,
                "clients": clients,
                "error": "Music Assistant will show the virtual Audio Hub Mix Input client first. Add real Snapcast clients for speakers, then play to/group them in MA." if user_client_count == 0 else "",
            }
            return self.bridge_status

        tap_group = find_group_for_client(server, tap_client)
        if not tap_client or not tap_group:
            await self.disable_tap_audio()
            await self.set_music_loopback_muted(True)
            self.bridge_status = {
                "enabled": True,
                "state": "tap_connecting",
                "ma_stream": ma_stream,
                "ma_stream_state": ma_stream_state,
                "final_stream": final_stream,
                "final_stream_state": final_stream_state,
                "tap_client": None,
                "tap_group": None,
                "user_client_count": user_client_count,
                "groups": summarize_groups(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap")),
                "clients": summarize_clients(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap")),
                "error": "internal Snapclient tap has not appeared in Snapserver yet",
            }
            return self.bridge_status

        tap_stream = group_stream(tap_group)
        if tap_stream == final_stream:
            await self.disable_tap_audio()
            await self.set_music_loopback_muted(True)
            self.bridge_status = {
                "enabled": True,
                "state": "tap_on_final_stream_blocked",
                "ma_stream": ma_stream,
                "ma_stream_state": ma_stream_state,
                "final_stream": final_stream,
                "final_stream_state": final_stream_state,
                "tap_client": client_id(tap_client),
                "tap_group": group_id(tap_group),
                "tap_group_stream": tap_stream,
                "user_client_count": user_client_count,
                "groups": groups,
                "clients": clients,
                "error": "Audio Hub Mix Input is currently on the final AudioHub stream. Its loopback is blocked to prevent delayed feedback; route/play Music Assistant audio to Audio Hub Mix Input to enable music mixing.",
            }
            return self.bridge_status

        if tap_stream != ma_stream:
            if ma_cfg.get("manage_tap_group_stream", False):
                await self.set_group_stream(group_id(tap_group), ma_stream)
                await asyncio.sleep(0.15)
            else:
                await self.disable_tap_audio()
                await self.set_music_loopback_muted(True)
                self.bridge_status = {
                    "enabled": True,
                    "state": "waiting_for_music_assistant_routing",
                    "ma_stream": ma_stream,
                    "ma_stream_state": ma_stream_state,
                    "final_stream": final_stream,
                    "final_stream_state": final_stream_state,
                    "tap_client": client_id(tap_client),
                    "tap_group": group_id(tap_group),
                    "tap_group_stream": tap_stream,
                    "user_client_count": user_client_count,
                    "groups": groups,
                    "clients": clients,
                    "error": "Music Assistant has not routed Audio Hub Mix Input to its playback stream yet. Press play on Audio Hub Mix Input in Music Assistant.",
                }
                return self.bridge_status
        await self.ensure_tap_audio()
        await self.sync_music_loopback_state(ma_stream_state)
        await self.apply_music_volume()
        tap_group_has_speakers = group_has_clients_other_than(tap_group, client_id(tap_client))

        rerouted = []
        if ma_cfg.get("auto_route_players", True) and final_stream:
            status = await self.server_status()
            server = extract_server(status) or server
            for group in server.get("groups", []):
                if group_id(group) == group_id(tap_group):
                    continue
                if group_stream(group) == ma_stream:
                    await self.set_group_stream(group_id(group), final_stream)
                    rerouted.append(group_id(group))
            if rerouted:
                status = await self.server_status()
                server = extract_server(status) or server

        groups = summarize_groups(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap"))
        clients = summarize_clients(server, ma_cfg.get("tap_client_id", "audio-hub-ma-tap"))
        user_client_count = sum(1 for client in clients if not client.get("internal_tap"))
        ma_stream_state = stream_state(server, ma_stream)
        final_stream_state = stream_state(server, final_stream)

        self.bridge_status = {
            "enabled": True,
            "state": "mixing_music_no_output_players" if user_client_count == 0 else "mixing_music",
            "ma_stream": ma_stream,
            "ma_stream_state": ma_stream_state,
            "final_stream": final_stream,
            "final_stream_state": final_stream_state,
            "tap_client": client_id(tap_client),
            "tap_group": group_id(tap_group),
            "user_client_count": user_client_count,
            "tap_group_has_speakers": tap_group_has_speakers,
            "rerouted_groups": rerouted,
            "groups": groups,
            "clients": clients,
            "error": "Do not group Audio Hub Mix Input with speaker clients in Music Assistant; keep it as the source/tap player only." if tap_group_has_speakers else ("Music is being mixed, but no real Snapcast playback clients are connected." if user_client_count == 0 else ""),
        }
        return self.bridge_status

    async def ensure_tap_process(self) -> bool:
        if self.tap_process and self.tap_process.running():
            self.tap_error = ""
            return True
        now = asyncio.get_running_loop().time()
        if self.tap_process and self.tap_process.started:
            self.tap_error = "\n".join(self.tap_process.last_output[-6:]) or "Audio Hub Mix Input snapclient exited"
            self.tap_process = None
            self.tap_retry_after = max(self.tap_retry_after, now + 20)
        if now < self.tap_retry_after:
            return False
        ma_cfg = self.config.get("music_assistant", {})
        tap_id = ma_cfg.get("tap_client_id", "audio-hub-ma-tap")
        command = [
            "snapclient",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.config["snapcast"]["client_stream_port"]),
            "--hostID",
            tap_id,
            "--instance",
            "91",
            "--player",
            "pulse",
            "--soundcard",
            "ma_music_tap",
            "--sampleformat",
            f"{self.config['audio']['sample_rate']}:16:*",
            "--latency",
            "0",
        ]
        self.tap_process = ManagedProcess(
            "ma-snapcast-tap",
            command,
            env={**PULSE_ENV, "PULSE_PROP": "media.role=music application.name=ma_snapcast_tap"},
            quiet_substrings=[
                "daemon started",
                "metadata",
                "Options '--host' and '--port' are deprecated",
                "(Stream) abs(age > 500)",
                "pShortBuffer->full()",
                "pMiniBuffer->full()",
                "(Controller) diff to server",
                "(PulsePlayer) Stop",
                "(PulsePlayer) Disconnecting from pulse",
                "(PulsePlayer) Connecting to pulse",
                "(PulsePlayer) Start",
                "(Stream) No chunks available",
                "(Controller) Codec:",
                "(Controller) ServerSettings",
                "(Player) Player name:",
                "(Player) Mixer mode:",
                "(Player) Sampleformat:",
                "(PulsePlayer) Setting property",
                "(PulsePlayer) Using buffer_time:",
                "(Stream) Exception: Not enough frames available",
                "(Stream) outputBufferDacTime > bufferMs",
                "(PulsePlayer) undeflow",
                "(PulsePlayer) underflow",
                "(PulsePlayer) latency increased",
                "(PulsePlayer) No chunk received for 5000ms",
                "(PulsePlayer) Chunk available, reconnecting to pulse",
                "pBuffer->full()",
            ],
        )
        await self.tap_process.start()
        await asyncio.sleep(0.35)
        if not self.tap_process.running():
            self.tap_error = "\n".join(self.tap_process.last_output[-6:]) or "Audio Hub Mix Input snapclient exited immediately"
            self.tap_process = None
            self.tap_retry_after = asyncio.get_running_loop().time() + 20
            return False
        self.tap_error = ""
        self.tap_retry_after = 0.0
        return True

    async def ensure_tap_audio(self) -> None:
        if self.tap_loopback_module:
            return
        attempts = [
            [
                "load-module",
                "module-loopback",
                "source=ma_music_tap.monitor",
                "sink=snap_hub_mix",
                "latency_msec=5",
                "adjust_time=0",
                "source_output_properties=application.name=ma_music_tap_source",
                "sink_input_properties=application.name=ma_music_tap",
            ],
            [
                "load-module",
                "module-loopback",
                "source=ma_music_tap.monitor",
                "sink=snap_hub_mix",
                "latency_msec=5",
                "adjust_time=0",
            ],
            [
                "load-module",
                "module-loopback",
                "source=ma_music_tap.monitor",
                "sink=snap_hub_mix",
                "latency_msec=10",
            ],
        ]
        errors = []
        for args in attempts:
            rc, out = await run_checked(["pactl", *args], timeout=5, env=PULSE_ENV)
            if rc == 0 and out.strip().isdigit():
                self.tap_loopback_module = out.strip()
                return
            errors.append(out.strip())
        LOG.warning("could not enable MA music tap loopback: %s", " | ".join(error for error in errors if error))

    async def apply_music_volume(self) -> None:
        volume = self.config.get("music_assistant", {}).get("music_volume", 0.85)
        percent = f"{max(0, min(200, int(float(volume) * 100)))}%"
        if self.music_loopback_muted:
            return
        await self._for_music_sink_inputs(lambda sink_input: run_checked(["pactl", "set-sink-input-volume", sink_input, percent], timeout=5, env=PULSE_ENV))

    async def sync_music_loopback_state(self, ma_stream_state: str | None) -> None:
        now = asyncio.get_running_loop().time()
        if ma_stream_state == "playing":
            self.ma_idle_since = 0.0
            await self.set_music_loopback_muted(False)
            return
        if not ma_stream_state:
            return
        if self.ma_idle_since == 0.0:
            self.ma_idle_since = now
            return
        if now - self.ma_idle_since >= 0.15:
            await self.set_music_loopback_muted(True)

    async def set_music_loopback_muted(self, muted: bool) -> None:
        value = "1" if muted else "0"
        await self._for_music_sink_inputs(lambda sink_input: run_checked(["pactl", "set-sink-input-mute", sink_input, value], timeout=5, env=PULSE_ENV))
        self.music_loopback_muted = muted

    async def _for_music_sink_inputs(self, action) -> None:
        rc, out = await run_checked(["pactl", "list", "sink-inputs"], timeout=5, env=PULSE_ENV)
        if rc != 0:
            return
        current = None
        matched = False
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sink Input #"):
                if current is not None and matched:
                    await action(current)
                current = stripped.replace("Sink Input #", "").strip()
                matched = False
            elif "application.name" in stripped:
                matched = "ma_music_tap" in stripped
        if current is not None and matched:
            await action(current)

    async def disable_tap_audio(self) -> None:
        if self.tap_loopback_module:
            await run_checked(["pactl", "unload-module", self.tap_loopback_module], timeout=5, env=PULSE_ENV)
            self.tap_loopback_module = None

    async def stop_music_assistant_tap(self) -> None:
        await self.disable_tap_audio()
        if self.tap_process:
            await self.tap_process.stop()
            self.tap_process = None
        self.tap_name_applied = False

    async def set_group_stream(self, group: str | None, stream: str | None) -> dict:
        if not group or not stream:
            return {"ok": False, "error": "missing group or stream"}
        if str(stream).lower() in ("default", "none", "null"):
            return {"ok": False, "error": f"refusing invalid Snapcast stream {stream}"}
        status = await self.server_status()
        server = extract_server(status)
        if server and not find_stream(server, stream):
            return {"ok": False, "error": f"Snapcast stream not found: {stream}"}
        return await self.rpc("Group.SetStream", {"id": group, "stream_id": stream})

    async def set_group_mute(self, group: str | None, muted: bool) -> dict:
        if not group:
            return {"ok": False, "error": "missing group"}
        return await self.rpc("Group.SetMute", {"id": group, "mute": muted})

    async def set_client_volume(self, client: str | None, volume: int, muted: bool | None = None) -> dict:
        if not client:
            return {"ok": False, "error": "missing client"}
        params = {"id": client, "volume": {"percent": max(0, min(100, int(volume)))}}
        if muted is not None:
            params["volume"]["muted"] = bool(muted)
        return await self.rpc("Client.SetVolume", params)

    async def set_client_name(self, client: str | None, name: str | None) -> dict:
        if not client or not name:
            return {"ok": False, "error": "missing client or name"}
        return await self.rpc("Client.SetName", {"id": client, "name": name})


def extract_server(status: dict) -> dict | None:
    if not isinstance(status, dict):
        return None
    result = status.get("result", {})
    server = result.get("server") if isinstance(result, dict) else None
    return server if isinstance(server, dict) else None


def find_stream(server: dict, wanted: str | None) -> str | None:
    if not wanted:
        return None
    wanted_lower = wanted.lower()
    for stream in server.get("streams", []):
        stream_id = str(stream.get("id") or stream.get("name") or "")
        if stream_id.lower() == wanted_lower:
            return stream_id
    for stream in server.get("streams", []):
        stream_id = str(stream.get("id") or stream.get("name") or "")
        if wanted_lower in stream_id.lower():
            return stream_id
    return None


def find_stream_info(server: dict, wanted: str | None) -> dict | None:
    if not wanted:
        return None
    wanted_lower = wanted.lower()
    for stream in server.get("streams", []):
        stream_id = str(stream.get("id") or stream.get("name") or "")
        if stream_id.lower() == wanted_lower:
            return stream
    return None


def stream_state(server: dict, stream_id: str | None) -> str | None:
    stream = find_stream_info(server, stream_id)
    if not stream:
        return None
    status = stream.get("status")
    if isinstance(status, dict):
        value = status.get("state") or status.get("name") or status.get("status")
    else:
        value = status or stream.get("state")
    return str(value or "").lower() or None


def find_music_assistant_stream(server: dict, prefix: str, final_stream: str | None) -> str | None:
    prefix_key = stream_key(prefix or "MusicAssistant")
    candidates = []
    for stream in server.get("streams", []):
        stream_id = str(stream.get("id") or stream.get("name") or "")
        if stream_id == final_stream:
            continue
        if stream_id.lower() in ("default", "none", "null"):
            continue
        stream_key_value = stream_key(stream_id)
        if stream_key_value.startswith(prefix_key) or prefix_key in stream_key_value:
            candidates.append(stream_id)
    playing = [stream_id for stream_id in candidates if stream_state(server, stream_id) == "playing"]
    return (playing[-1] if playing else candidates[-1]) if candidates else None


def stream_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def find_tap_client(server: dict, tap_id: str) -> dict | None:
    tap_id = tap_id.lower()
    for client in all_clients(server):
        if tap_id in json.dumps(client, sort_keys=True).lower():
            return client
    return None


def find_group_for_client(server: dict, client: dict | None) -> dict | None:
    cid = client_id(client) if client else None
    if not cid:
        return None
    cid_lower = cid.lower()
    for group in server.get("groups", []):
        for item in group.get("clients", []):
            item_id = item.get("id") if isinstance(item, dict) else item
            item_text = json.dumps(item, sort_keys=True).lower() if isinstance(item, dict) else str(item).lower()
            if item_id == cid or cid_lower in item_text:
                return group
    return None


def group_has_clients_other_than(group: dict | None, tap_client_id: str | None) -> bool:
    if not isinstance(group, dict) or not tap_client_id:
        return False
    tap_lower = tap_client_id.lower()
    for item in group.get("clients", []):
        item_id = item.get("id") if isinstance(item, dict) else item
        item_text = json.dumps(item, sort_keys=True).lower() if isinstance(item, dict) else str(item).lower()
        if item_id and item_id != tap_client_id and tap_lower not in item_text:
            return True
    return False


def group_id(group: dict | None) -> str | None:
    return str(group.get("id")) if isinstance(group, dict) and group.get("id") is not None else None


def group_stream(group: dict | None) -> str | None:
    if not isinstance(group, dict):
        return None
    return str(group.get("stream_id") or group.get("stream") or "") or None


def client_id(client: dict | None) -> str | None:
    return str(client.get("id")) if isinstance(client, dict) and client.get("id") is not None else None


def all_clients(server: dict) -> list[dict]:
    result = []
    seen = set()

    def add(client):
        if not isinstance(client, dict):
            return
        key = client_id(client) or json.dumps(client, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        result.append(client)

    for client in server.get("clients", []):
        add(client)
    for group in server.get("groups", []):
        for client in group.get("clients", []):
            if isinstance(client, dict):
                add(client)
            elif client:
                add({"id": str(client)})
    return result


def summarize_groups(server: dict, tap_id: str = "audio-hub-ma-tap") -> list[dict]:
    result = []
    for group in server.get("groups", []):
        clients = group.get("clients", [])
        client_ids = [item.get("id") if isinstance(item, dict) else item for item in clients]
        internal_tap = any(tap_id.lower() in json.dumps(item, sort_keys=True).lower() for item in clients)
        result.append({
            "id": group_id(group),
            "name": group.get("name") or group_id(group),
            "stream": group_stream(group),
            "muted": group.get("muted", False),
            "clients": client_ids,
            "internal_tap": internal_tap,
        })
    return result


def summarize_clients(server: dict, tap_id: str = "audio-hub-ma-tap") -> list[dict]:
    result = []
    for client in all_clients(server):
        host = client.get("host", {}) if isinstance(client.get("host"), dict) else {}
        config = client.get("config", {}) if isinstance(client.get("config"), dict) else {}
        volume = config.get("volume", {}) if isinstance(config.get("volume"), dict) else {}
        internal_tap = tap_id.lower() in json.dumps(client, sort_keys=True).lower()
        result.append({
            "id": client_id(client),
            "name": config.get("name") or host.get("name") or client_id(client),
            "host_id": host.get("id"),
            "connected": client.get("connected", True),
            "volume": volume.get("percent"),
            "muted": volume.get("muted"),
            "internal_tap": internal_tap,
        })
    return result


def bits_from_format(fmt: str) -> int:
    if "24" in fmt:
        return 24
    if "32" in fmt or "float" in fmt:
        return 32
    return 16
