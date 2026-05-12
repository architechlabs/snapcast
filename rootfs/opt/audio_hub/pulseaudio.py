#!/usr/bin/env python3
import asyncio
import logging
import os
import shlex
import struct
from pathlib import Path

from process import ManagedProcess, run_binary, run_checked

LOG = logging.getLogger("pulse")
FIFO = Path("/tmp/audio-hub/snapcast.pcm")
RTP_SDP = Path("/tmp/audio-hub/rtp.sdp")
PULSE_DIR = Path("/tmp/audio-hub/pulse")
PULSE_SOCKET = PULSE_DIR / "native"
PULSE_ENV = {
    "PULSE_RUNTIME_PATH": str(PULSE_DIR),
    "PULSE_SERVER": f"unix:{PULSE_SOCKET}",
    "PULSE_STATE_PATH": "/data/pulse",
    "PULSE_COOKIE": "/data/pulse/cookie",
    "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket",
}
HOST_PULSE_ENV = {
    key: value
    for key, value in {
        "PULSE_SERVER": os.environ.get("AUDIO_HUB_HOST_PULSE_SERVER", ""),
        "PULSE_COOKIE": os.environ.get("AUDIO_HUB_HOST_PULSE_COOKIE", ""),
        "PULSE_RUNTIME_PATH": os.environ.get("AUDIO_HUB_HOST_PULSE_RUNTIME_PATH", ""),
    }.items()
    if value
}


class PulseAudioManager:
    def __init__(self, config: dict):
        self.config = config
        self.processes: dict[str, ManagedProcess] = {}
        self.modules: list[str] = []
        self.wired_source_loaded = False
        self.wired_device: str | None = None
        self.wired_capture_mode = "none"
        self.wired_error = ""
        self.wired_busy = False
        self.wired_attempted_devices: list[str] = []

    async def start(self, devices: dict) -> None:
        await self.stop()
        FIFO.parent.mkdir(parents=True, exist_ok=True)
        PULSE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in (PULSE_SOCKET, PULSE_DIR / "pid"):
            if stale.exists():
                stale.unlink()
        if FIFO.exists():
            FIFO.unlink()
        os.mkfifo(FIFO, 0o666)

        pulse = ManagedProcess(
            "pulseaudio",
            [
                "pulseaudio",
                "--daemonize=no",
                "--system=true",
                "--disable-shm=yes",
                "--disallow-module-loading=false",
                "-n",
                "--exit-idle-time=-1",
                "--disallow-exit=true",
                "--log-target=stderr",
                "--log-level=error",
                "-L",
                f"module-native-protocol-unix socket={PULSE_SOCKET} auth-anonymous=1 auth-cookie-enabled=0",
            ],
            env=PULSE_ENV,
            quiet_substrings=[
                "Running in system mode",
                "Please make sure that you actually do want to do that",
                "WhatIsWrongWithSystemWide",
                "disallow-module-loading",
            ],
        )
        self.processes["pulseaudio"] = pulse
        await pulse.start()
        await self._wait_ready()
        await self.configure(devices)

    async def configure(self, devices: dict) -> None:
        cfg = self.config
        rate = cfg["audio"]["sample_rate"]
        channels = cfg["audio"]["channels"]
        latency = cfg["audio"]["latency_ms"]
        routing = cfg["audio"]["routing_mode"]

        await self._pactl("load-module", "module-null-sink", "sink_name=snap_hub_mix", "sink_properties=device.description=Snapcast_Audio_Hub_Mix", f"rate={rate}", f"channels={channels}")
        await self._pactl("set-default-sink", "snap_hub_mix")

        wired_enabled, network_enabled, bluetooth_enabled = self._enabled_routes(routing)

        if routing == "fallback_duck":
            try:
                await self._pactl("load-module", "module-role-ducking", "trigger_roles=phone", "ducking_roles=music,video", "volume=35%")
            except RuntimeError as err:
                LOG.warning("PulseAudio role ducking is not available; fallback_duck will run as a conservative mix: %s", err)

        if wired_enabled:
            await self._setup_wired_source(devices, latency)
        if network_enabled:
            await self._setup_network_sources()
        if bluetooth_enabled:
            await self._setup_bluetooth()

        await self._apply_volumes()
        await self._start_silence_keepalive(rate, channels)

        parec = ManagedProcess(
            "mix-monitor-to-snapfifo",
            [
                "bash",
                "-lc",
                f"exec parec -d snap_hub_mix.monitor --raw --format={cfg['audio']['format']} --rate={rate} --channels={channels} > {FIFO}",
            ],
            env=PULSE_ENV,
        )
        self.processes["parec"] = parec
        await parec.start()

    async def _start_silence_keepalive(self, rate: int, channels: int) -> None:
        layout = "mono" if channels == 1 else "stereo"
        silence = ManagedProcess(
            "silence-keepalive",
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout={layout}:sample_rate={rate}",
                "-f",
                "pulse",
                "snap_hub_mix",
            ],
            env={**PULSE_ENV, "PULSE_PROP": "media.role=music application.name=silence_keepalive"},
        )
        self.processes["silence-keepalive"] = silence
        await silence.start()

    async def _wait_ready(self) -> None:
        for _ in range(50):
            rc, _ = await run_checked(["pactl", "info"], timeout=3, env=PULSE_ENV)
            if rc == 0:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("PulseAudio did not become ready")

    async def _pactl(self, *args: str) -> str:
        rc, out = await run_checked(["pactl", *args], timeout=10, env=PULSE_ENV)
        if rc != 0:
            raise RuntimeError(f"pactl {' '.join(args)} failed: {out}")
        if args and args[0] == "load-module":
            module_id = out.strip()
            if module_id.isdigit():
                self.modules.append(module_id)
        return out.strip()

    def _enabled_routes(self, routing: str) -> tuple[bool, bool, bool]:
        wired = bool(self.config["wired"]["enabled"])
        network = bool(self.config["network"]["enabled"])
        bluetooth = bool(self.config["wireless"]["bluetooth_enabled"])
        if routing == "wired_only":
            return wired, False, False
        if routing == "network_only":
            return False, network, False
        if routing == "bluetooth_only":
            return False, False, bluetooth
        if routing == "source_switch":
            if wired:
                return True, False, False
            if network:
                return False, True, False
            return False, False, bluetooth
        return wired, network, bluetooth

    async def _setup_wired_source(self, devices: dict, latency: int) -> None:
        self.wired_source_loaded = False
        self.wired_device = None
        self.wired_capture_mode = "none"
        self.wired_error = ""
        self.wired_busy = False
        self.wired_attempted_devices = []
        device = self.config["wired"]["device"]
        if device == "auto":
            device = devices.get("selected_capture")
        else:
            device = normalize_capture_device(device, devices)
        if not device:
            LOG.info("wired input is enabled, but no USB/ALSA capture device is present yet")
            return
        source_name = "wired_input"
        candidates = capture_device_candidates(device, devices)
        self.wired_attempted_devices = candidates
        errors = []

        if await self._start_host_pulse_bridge(devices):
            self.wired_source_loaded = True
            self.wired_device = "haos_pulse_source"
            self.wired_capture_mode = "haos_pulse_bridge"
            self.wired_busy = False
            return

        for candidate in candidates:
            if await self._start_ffmpeg_alsa_bridge(candidate):
                self.wired_source_loaded = True
                self.wired_device = candidate
                self.wired_capture_mode = "ffmpeg_alsa_bridge"
                self.wired_busy = False
                return
            if self.wired_error:
                errors.append(f"{candidate}: {self.wired_error}")

        busy_errors = [error for error in errors if is_busy_error(error)]
        if busy_errors:
            self.wired_busy = True
            self.wired_capture_mode = "busy"
            self.wired_error = concise_error(
                "Capture device is busy. Another service is holding the microphone, so the add-on cannot open it yet.",
                busy_errors,
            )
            LOG.warning("%s", self.wired_error)
            return

        for candidate in candidates[:3]:
            if await self._try_pulse_alsa_source(candidate, source_name, latency):
                self.wired_source_loaded = True
                self.wired_device = candidate
                self.wired_capture_mode = "pulseaudio_source"
                self.wired_error = ""
                self.wired_busy = False
                return

        self.wired_busy = any(is_busy_error(error) for error in errors)
        if errors:
            self.wired_error = concise_error("Wired input could not be attached.", errors)
        LOG.warning("wired input %s could not be attached by FFmpeg or PulseAudio", device)

    async def retry_wired_input(self, devices: dict) -> bool:
        if not self.config["wired"]["enabled"]:
            return False
        bridge = self.processes.get("wired-alsa-bridge")
        if self.wired_source_loaded and self.wired_capture_mode == "ffmpeg_alsa_bridge" and bridge and bridge.running():
            return True
        if self.wired_source_loaded and self.wired_capture_mode == "pulseaudio_source":
            return True
        await self._stop_wired_capture()
        await self._setup_wired_source(devices, self.config["audio"]["latency_ms"])
        await self._apply_volumes()
        return self.wired_source_loaded

    async def _stop_wired_capture(self) -> None:
        bridge = self.processes.pop("wired-alsa-bridge", None)
        if bridge:
            await bridge.stop()
        host_bridge = self.processes.pop("host-pulse-capture-bridge", None)
        if host_bridge:
            await host_bridge.stop()
        await self._unload_source_if_exists("wired_input")
        self.wired_source_loaded = False
        self.wired_device = None
        self.wired_capture_mode = "none"
        self.wired_error = ""
        self.wired_busy = False

    async def _try_pulse_alsa_source(self, device: str, source_name: str, latency: int) -> bool:
        rate = str(self.config["audio"]["sample_rate"])
        attempts = [
            [f"device={device}", f"source_name={source_name}", f"source_properties=device.description=Wired_Input_{device}"],
            [f"device={device}", f"source_name={source_name}", f"rate={rate}", "channels=1", f"source_properties=device.description=Wired_Input_{device}"],
            [f"device={device}", f"source_name={source_name}", "rate=44100", "channels=1", f"source_properties=device.description=Wired_Input_{device}"],
        ]
        if device.startswith("plughw:"):
            hw_device = "hw:" + device.split(":", 1)[1]
            attempts.append([f"device={hw_device}", f"source_name={source_name}", f"source_properties=device.description=Wired_Input_{hw_device}"])

        last_error = None
        for args in attempts:
            try:
                await self._pactl("load-module", "module-alsa-source", *args)
                loopback_args = ["load-module", "module-loopback", f"source={source_name}", "sink=snap_hub_mix", f"latency_msec={latency}"]
                if self.config["audio"]["routing_mode"] == "fallback_duck":
                    loopback_args.append("sink_input_properties=media.role=phone")
                await self._pactl(*loopback_args)
                LOG.info("wired input attached through PulseAudio source using %s", " ".join(args))
                return True
            except RuntimeError as err:
                last_error = err
                await self._unload_source_if_exists(source_name)
        LOG.warning("PulseAudio could not open wired input %s; falling back to FFmpeg ALSA bridge: %s", device, last_error)
        return False

    async def _unload_source_if_exists(self, source_name: str) -> None:
        rc, out = await run_checked(["pactl", "list", "sources", "short"], timeout=5, env=PULSE_ENV)
        if rc != 0:
            return
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == source_name:
                await run_checked(["pactl", "unload-module", parts[0]], timeout=5, env=PULSE_ENV)

    async def _start_ffmpeg_alsa_bridge(self, device: str) -> bool:
        cfg = self.config
        bridge = ManagedProcess(
            "wired-alsa-bridge",
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-f",
                "alsa",
                "-thread_queue_size",
                "1024",
                "-i",
                device,
                "-ar",
                str(cfg["audio"]["sample_rate"]),
                "-ac",
                str(cfg["audio"]["channels"]),
                "-f",
                "pulse",
                "snap_hub_mix",
            ],
            env={**PULSE_ENV, "PULSE_PROP": "media.role=phone application.name=wired_alsa_bridge"},
        )
        self.processes["wired-alsa-bridge"] = bridge
        await bridge.start()
        await asyncio.sleep(0.8)
        if not bridge.running():
            self.wired_error = "\n".join(bridge.last_output[-6:]) or "FFmpeg ALSA bridge exited immediately"
            LOG.warning("FFmpeg ALSA bridge failed for %s: %s", device, self.wired_error)
            busy = await self._busy_diagnostics(device)
            if busy:
                self.wired_error = f"{self.wired_error}\n{busy}"
            return False
        self.wired_error = ""
        LOG.info("wired input attached through FFmpeg ALSA bridge using %s", device)
        return True

    async def _start_host_pulse_bridge(self, devices: dict) -> bool:
        source = await self._select_host_pulse_source(devices)
        if not source:
            return False
        cfg = self.config
        bridge = ManagedProcess(
            "host-pulse-capture-bridge",
            ["bash", "-lc", host_pulse_bridge_command(source, cfg["audio"]["sample_rate"], cfg["audio"]["channels"], cfg["audio"]["format"])],
            env={**HOST_PULSE_ENV, **PULSE_ENV},
            quiet_substrings=["Failed to create secure directory"],
        )
        self.processes["host-pulse-capture-bridge"] = bridge
        await bridge.start()
        await asyncio.sleep(0.8)
        if not bridge.running():
            self.wired_error = "\n".join(bridge.last_output[-6:]) or "HAOS PulseAudio capture bridge exited immediately"
            LOG.warning("HAOS PulseAudio capture bridge failed for %s: %s", source, self.wired_error)
            return False
        self.wired_error = ""
        LOG.info("wired input attached through HAOS PulseAudio source %s", source)
        return True

    async def _select_host_pulse_source(self, devices: dict) -> str | None:
        if not host_pulse_enabled():
            return None
        rc, out = await run_checked(["pactl", "list", "sources", "short"], timeout=4, env=HOST_PULSE_ENV)
        if rc != 0:
            return None
        sources = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            if ".monitor" in name or name.endswith(".monitor"):
                continue
            sources.append(name)
        if not sources:
            return None
        tokens = host_pulse_match_tokens(devices)
        for source in sources:
            lowered = source.lower()
            if any(token and token in lowered for token in tokens):
                return source
        return sources[0] if len(sources) == 1 else None

    async def _busy_diagnostics(self, device: str) -> str:
        match = parse_hw_device(device)
        if not match:
            return ""
        card, pcm = match
        node = f"/dev/snd/pcmC{card}D{pcm}c"
        if not Path(node).exists():
            return ""
        diagnostics = []
        rc, out = await run_checked(["fuser", "-v", node], timeout=3)
        if rc == 0 and out.strip():
            diagnostics.append(f"Busy holder from fuser for {node}: {out.strip()}")
        rc, out = await run_checked(["lsof", node], timeout=3)
        if rc == 0 and out.strip():
            diagnostics.append(f"Open file holder from lsof for {node}: {out.strip()}")
        return "\n".join(diagnostics)

    async def _setup_network_sources(self) -> None:
        cfg = self.config
        rate = cfg["audio"]["sample_rate"]
        channels = cfg["audio"]["channels"]
        if cfg["network"]["tcp_pcm_enabled"]:
            port = cfg["network"]["tcp_pcm_port"]
            tcp = ManagedProcess(
                "tcp-pcm-bridge",
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-f",
                    cfg["audio"]["format"],
                    "-ar",
                    str(rate),
                    "-ac",
                    str(channels),
                    "-listen",
                    "1",
                    "-i",
                    f"tcp://0.0.0.0:{port}?listen=1",
                    "-f",
                    "pulse",
                    "snap_hub_mix",
                ],
                env={**PULSE_ENV, "PULSE_PROP": "media.role=music application.name=tcp_pcm_bridge"},
            )
            self.processes["tcp-pcm-bridge"] = tcp
            await tcp.start()

        if cfg["network"]["rtp_enabled"]:
            port = cfg["network"]["rtp_port"]
            RTP_SDP.write_text(
                Path("/etc/audio_hub/rtp-template.sdp").read_text().format(port=port, rate=rate, channels=channels),
                encoding="utf-8",
            )
            rtp = ManagedProcess(
                "rtp-bridge",
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-protocol_whitelist",
                    "file,udp,rtp",
                    "-i",
                    str(RTP_SDP),
                    "-f",
                    "pulse",
                    "snap_hub_mix",
                ],
                env={**PULSE_ENV, "PULSE_PROP": "media.role=music application.name=rtp_bridge"},
            )
            self.processes["rtp-bridge"] = rtp
            await rtp.start()

    async def _setup_bluetooth(self) -> None:
        if self.config["wireless"].get("bluetooth_pairable"):
            bt = ManagedProcess("bluetoothd", ["bluetoothd", "-n"])
            self.processes["bluetoothd"] = bt
            await bt.start()
            await asyncio.sleep(1)
            await run_checked(["bluetoothctl", "power", "on"], timeout=5)
            await run_checked(["bluetoothctl", "agent", "NoInputNoOutput"], timeout=5)
            await run_checked(["bluetoothctl", "default-agent"], timeout=5)
            await run_checked(["bluetoothctl", "discoverable", "on"], timeout=5)
            await run_checked(["bluetoothctl", "pairable", "on"], timeout=5)
        try:
            await self._pactl("load-module", "module-bluetooth-policy")
            await self._pactl("load-module", "module-bluetooth-discover")
        except RuntimeError as err:
            LOG.warning("Bluetooth audio is not available in this HAOS/container environment: %s", err)

    async def _apply_volumes(self) -> None:
        await self.set_volume("wired", self.config["wired"]["volume"])
        await self.set_volume("network", self.config["network"]["volume"])
        await self.set_volume("bluetooth", self.config["wireless"]["volume"])
        if self.config["wired"].get("mute"):
            await run_checked(["pactl", "set-source-mute", "wired_input", "1"], timeout=5, env=PULSE_ENV)

    async def set_volume(self, target: str, volume: float) -> None:
        percent = f"{max(0, min(200, int(volume * 100)))}%"
        if target == "wired":
            if self.wired_capture_mode == "pulseaudio_source":
                await run_checked(["pactl", "set-source-volume", "wired_input", percent], timeout=5, env=PULSE_ENV)
            elif self.wired_capture_mode in ("ffmpeg_alsa_bridge", "haos_pulse_bridge"):
                await self._set_sink_input_volume(["wired_alsa_bridge", "host_pulse_bridge"], percent)
        elif target in ("network", "bluetooth"):
            names = ["tcp_pcm_bridge", "rtp_bridge"] if target == "network" else ["bluetooth"]
            await self._set_sink_input_volume(names, percent)

    async def _set_sink_input_volume(self, app_names: list[str], percent: str) -> None:
        rc, out = await run_checked(["pactl", "list", "sink-inputs"], timeout=5, env=PULSE_ENV)
        if rc != 0:
            return
        current = None
        matched = False
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sink Input #"):
                if current is not None and matched:
                    await run_checked(["pactl", "set-sink-input-volume", current, percent], timeout=5, env=PULSE_ENV)
                current = stripped.replace("Sink Input #", "").strip()
                matched = False
            elif "application.name" in stripped:
                matched = any(name in stripped for name in app_names)
        if current is not None and matched:
            await run_checked(["pactl", "set-sink-input-volume", current, percent], timeout=5, env=PULSE_ENV)

    async def stop(self) -> None:
        for process in reversed(list(self.processes.values())):
            await process.stop()
        self.processes.clear()
        self.modules.clear()
        self.wired_source_loaded = False
        self.wired_device = None
        self.wired_capture_mode = "none"
        self.wired_error = ""
        self.wired_busy = False
        self.wired_attempted_devices = []

    def health(self) -> str:
        pulse = self.processes.get("pulseaudio")
        parec = self.processes.get("parec")
        if pulse and pulse.running() and parec and parec.running():
            return "running"
        if pulse and pulse.running():
            return "partial"
        return "stopped"

    async def input_level(self) -> dict:
        if not self.wired_source_loaded or not self.wired_device:
            state = "capture_busy" if self.wired_busy else "no_wired_input"
            return {"ok": False, "state": state, "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": self.wired_error, "attempted": self.wired_attempted_devices}
        bridge = self.processes.get("wired-alsa-bridge")
        if self.wired_capture_mode == "ffmpeg_alsa_bridge" and bridge and not bridge.running():
            self.wired_source_loaded = False
            self.wired_error = "\n".join(bridge.last_output[-6:]) or "FFmpeg ALSA bridge is not running"
            return {"ok": False, "state": "capture_bridge_stopped", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": self.wired_error}
        if self.wired_capture_mode == "pulseaudio_source":
            command = [
                "parec",
                "-d",
                "wired_input",
                "--raw",
                "--format=s16le",
                "--rate=16000",
                "--channels=1",
            ]
            rc, raw, err = await run_binary(command, timeout=1, env=PULSE_ENV, max_bytes=64000)
        else:
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "pulse",
                "-i",
                "snap_hub_mix.monitor",
                "-t",
                "0.6",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "-",
            ]
            rc, raw, err = await run_binary(command, timeout=3, env=PULSE_ENV, max_bytes=64000)
        if not raw:
            error = err.decode(errors="replace")[:400] or self.wired_error or "no PCM bytes received from mixer monitor"
            return {"ok": False, "state": "capture_unavailable", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": error}
        level, peak = pcm16_level(raw)
        return {"ok": rc in (0, 124), "state": "signal" if level > 0.015 else "quiet", "level": level, "peak": peak, "mode": self.wired_capture_mode}

    async def monitor_clip(self, seconds: float = 3.0) -> bytes:
        duration = max(1.0, min(10.0, seconds))
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "pulse",
            "-i",
            "snap_hub_mix.monitor",
            "-t",
            str(duration),
            "-ac",
            "2",
            "-ar",
            "48000",
            "-f",
            "wav",
            "-",
        ]
        rc, raw, err = await run_binary(command, timeout=int(duration) + 4, env=PULSE_ENV, max_bytes=2_000_000)
        if rc not in (0, 124) or not raw:
            raise RuntimeError(err.decode(errors="replace")[:400] or "failed to record monitor clip")
        return raw


def normalize_capture_device(device: str, devices: dict) -> str:
    if device in ("", "auto"):
        return devices.get("selected_capture")
    for candidate in devices.get("capture", []):
        values = {
            candidate.get("alsa"),
            candidate.get("plughw"),
            f"hw:{candidate.get('card')},{candidate.get('device')}",
            f"plughw:{candidate.get('card')},{candidate.get('device')}",
        }
        if device in values:
            return candidate.get("plughw") or candidate.get("alsa") or device
    return device


def capture_device_candidates(device: str, devices: dict) -> list[str]:
    selected = normalize_capture_device(device, devices)
    candidates = [selected]
    for capture in devices.get("capture", []):
        values = {
            capture.get("alsa"),
            capture.get("plughw"),
            f"hw:{capture.get('card')},{capture.get('device')}",
            f"plughw:{capture.get('card')},{capture.get('device')}",
        }
        if selected in values or device in values:
            candidates.extend(capture.get("candidates", []))
            break
    return dedupe([candidate for candidate in candidates if candidate])


def dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_hw_device(device: str) -> tuple[str, str] | None:
    if device.startswith(("hw:", "plughw:")):
        parts = device.split(":", 1)[1].split(",", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return parts[0], parts[1]
    return None


def is_busy_error(error: str) -> bool:
    lower = error.lower()
    return "resource busy" in lower or "device or resource busy" in lower or "busy holder" in lower


def host_pulse_enabled() -> bool:
    server = HOST_PULSE_ENV.get("PULSE_SERVER", "")
    return bool(server and server != PULSE_ENV["PULSE_SERVER"] and str(PULSE_SOCKET) not in server)


def host_pulse_match_tokens(devices: dict) -> list[str]:
    tokens = []
    for capture in devices.get("capture", []):
        for key in ("card_id", "card_name", "description"):
            value = str(capture.get(key, "")).lower()
            for token in value.replace("[", " ").replace("]", " ").replace("_", " ").split():
                if len(token) >= 3 and token not in tokens:
                    tokens.append(token)
    tokens.extend(["usb", "input", "microphone", "mic"])
    return tokens


def host_pulse_bridge_command(source: str, rate: int, channels: int, audio_format: str) -> str:
    host_server = shlex.quote(HOST_PULSE_ENV.get("PULSE_SERVER", ""))
    host_cookie = shlex.quote(HOST_PULSE_ENV.get("PULSE_COOKIE", ""))
    host_runtime = shlex.quote(HOST_PULSE_ENV.get("PULSE_RUNTIME_PATH", ""))
    local_server = shlex.quote(PULSE_ENV["PULSE_SERVER"])
    source_arg = shlex.quote(source)
    cookie_part = f" PULSE_COOKIE={host_cookie}" if host_cookie != "''" else ""
    runtime_part = f" PULSE_RUNTIME_PATH={host_runtime}" if host_runtime != "''" else ""
    return (
        f"PULSE_SERVER={host_server}{cookie_part}{runtime_part} "
        f"parec -d {source_arg} --raw --format={audio_format} --rate={int(rate)} --channels={int(channels)} "
        f"| PULSE_SERVER={local_server} PULSE_PROP=application.name=host_pulse_bridge "
        f"pacat --raw --format={audio_format} --rate={int(rate)} --channels={int(channels)} -d snap_hub_mix"
    )


def concise_error(prefix: str, errors: list[str]) -> str:
    important = []
    for error in errors:
        short = " ".join(error.split())
        short = short.replace("Error opening input files: I/O error", "").strip()
        if len(short) > 280:
            short = short[:277].rstrip() + "..."
        if short and short not in important:
            important.append(short)
        if len(important) >= 3:
            break
    return f"{prefix} Tried: {' | '.join(important)}"


def pcm16_level(raw: bytes) -> tuple[float, float]:
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        return 0.0, 0.0
    count = usable // 2
    samples = struct.unpack("<" + "h" * count, raw[:usable])
    if not samples:
        return 0.0, 0.0
    squares = sum(sample * sample for sample in samples)
    peak = max(abs(sample) for sample in samples) / 32768.0
    rms = (squares / len(samples)) ** 0.5 / 32768.0
    return round(rms, 4), round(peak, 4)
