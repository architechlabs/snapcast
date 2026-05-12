#!/usr/bin/env python3
import asyncio
import logging
import os
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


class PulseAudioManager:
    def __init__(self, config: dict):
        self.config = config
        self.processes: dict[str, ManagedProcess] = {}
        self.modules: list[str] = []
        self.wired_source_loaded = False
        self.wired_device: str | None = None
        self.wired_capture_mode = "none"

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
        device = self.config["wired"]["device"]
        if device == "auto":
            device = devices.get("selected_capture")
        else:
            device = normalize_capture_device(device, devices)
        if not device:
            LOG.info("wired input is enabled, but no USB/ALSA capture device is present yet")
            return
        source_name = "wired_input"
        if await self._try_pulse_alsa_source(device, source_name, latency):
            self.wired_source_loaded = True
            self.wired_device = device
            self.wired_capture_mode = "pulseaudio_source"
            return

        await self._start_ffmpeg_alsa_bridge(device)
        self.wired_source_loaded = True
        self.wired_device = device
        self.wired_capture_mode = "ffmpeg_alsa_bridge"

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

    async def _start_ffmpeg_alsa_bridge(self, device: str) -> None:
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
        LOG.info("wired input attached through FFmpeg ALSA bridge using %s", device)

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
            elif self.wired_capture_mode == "ffmpeg_alsa_bridge":
                await self._set_sink_input_volume(["wired_alsa_bridge"], percent)
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
            return {"ok": False, "state": "no_wired_input", "level": 0, "peak": 0, "mode": self.wired_capture_mode}
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
                "parec",
                "-d",
                "snap_hub_mix.monitor",
                "--raw",
                "--format=s16le",
                "--rate=16000",
                "--channels=1",
            ]
            rc, raw, err = await run_binary(command, timeout=1, env=PULSE_ENV, max_bytes=64000)
        if not raw:
            return {"ok": False, "state": "capture_unavailable", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": err.decode(errors="replace")[:400]}
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
