#!/usr/bin/env python3
import asyncio
import logging
import os
from pathlib import Path

from process import ManagedProcess, run_checked

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
}


class PulseAudioManager:
    def __init__(self, config: dict):
        self.config = config
        self.processes: dict[str, ManagedProcess] = {}
        self.modules: list[str] = []

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
                "--system=false",
                "--disable-shm=yes",
                "-n",
                "--exit-idle-time=-1",
                "--disallow-exit=true",
                "--log-target=stderr",
                "-L",
                f"module-native-protocol-unix socket={PULSE_SOCKET} auth-anonymous=1",
            ],
            env=PULSE_ENV,
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
        if not device:
            LOG.warning("wired input enabled but no ALSA capture device was found")
            return
        source_name = "wired_input"
        try:
            await self._pactl("load-module", "module-alsa-source", f"device={device}", f"source_name={source_name}", f"source_properties=device.description=Wired_Input_{device}")
        except RuntimeError as err:
            LOG.warning("wired input %s could not be opened; continuing without wired source: %s", device, err)
            return
        loopback_args = ["load-module", "module-loopback", f"source={source_name}", "sink=snap_hub_mix", f"latency_msec={latency}"]
        if self.config["audio"]["routing_mode"] == "fallback_duck":
            loopback_args.append("sink_input_properties=media.role=phone")
        await self._pactl(*loopback_args)

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
            await run_checked(["pactl", "set-source-volume", "wired_input", percent], timeout=5, env=PULSE_ENV)
        elif target in ("network", "bluetooth"):
            rc, out = await run_checked(["pactl", "list", "sink-inputs", "short"], timeout=5, env=PULSE_ENV)
            if rc == 0:
                for line in out.splitlines():
                    if target in line.lower() or "ffmpeg" in line.lower():
                        sink_input = line.split()[0]
                        await run_checked(["pactl", "set-sink-input-volume", sink_input, percent], timeout=5, env=PULSE_ENV)

    async def stop(self) -> None:
        for process in reversed(list(self.processes.values())):
            await process.stop()
        self.processes.clear()
        self.modules.clear()

    def health(self) -> str:
        pulse = self.processes.get("pulseaudio")
        parec = self.processes.get("parec")
        if pulse and pulse.running() and parec and parec.running():
            return "running"
        if pulse and pulse.running():
            return "partial"
        return "stopped"
