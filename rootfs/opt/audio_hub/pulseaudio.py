#!/usr/bin/env python3
import asyncio
import logging
import os
import re
import shlex
import struct
import time
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
HOST_PULSE_SOCKET_CANDIDATES = (
    "/run/audio/pulse.sock",
    "/run/audio/pulse/native",
    "/run/pulse/native",
    "/var/run/pulse/native",
)


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
        self.host_pulse_error = ""
        self.host_pulse_env: dict[str, str] | None = None
        self.host_pulse_source: str | None = None
        self.host_pulse_bridge_engine = ""
        self.wired_bridge_engine = ""
        self.last_input_level: dict | None = None
        self.level_task: asyncio.Task | None = None
        self.level_proc: asyncio.subprocess.Process | None = None
        self.current_input_level: dict | None = None
        self.current_input_level_at = 0.0
        self.host_pulse_source_suspended = False

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
        await self._pactl("load-module", "module-null-sink", "sink_name=ma_music_tap", "sink_properties=device.description=Music_Assistant_Tap", f"rate={rate}", f"channels={channels}")
        await self._pactl("set-default-sink", "snap_hub_mix")

        wired_enabled, network_enabled, bluetooth_enabled = self._enabled_routes(routing)

        if routing == "fallback_duck" or cfg.get("music_assistant", {}).get("ducking_enabled"):
            try:
                duck_level = int(float(cfg.get("music_assistant", {}).get("ducking_level", 0.35)) * 100)
                await self._pactl("load-module", "module-role-ducking", "trigger_roles=phone", "ducking_roles=music,video", f"volume={duck_level}%")
            except RuntimeError as err:
                LOG.warning("PulseAudio role ducking is not available; fallback_duck will run as a conservative mix: %s", err)

        if wired_enabled:
            await self._setup_wired_source(devices, latency)
        if network_enabled:
            await self._setup_network_sources()
        if bluetooth_enabled:
            await self._setup_bluetooth()

        await self._apply_volumes()
        if cfg["audio"].get("keepalive_silence", False):
            await self._start_silence_keepalive(rate, channels)

        parec = ManagedProcess(
            "mix-monitor-to-snapfifo",
            [
                "bash",
                "-lc",
                f"exec parec -d snap_hub_mix.monitor --latency-msec={max(5, min(10, latency))} --process-time-msec=5 --raw --format={cfg['audio']['format']} --rate={rate} --channels={channels} > {FIFO}",
            ],
            env={**PULSE_ENV, "PULSE_LATENCY_MSEC": "5"},
        )
        self.processes["parec"] = parec
        await parec.start()
        await self._start_level_monitor()

    async def _start_silence_keepalive(self, rate: int, channels: int) -> None:
        layout = "mono" if channels == 1 else "stereo"
        pulse_args = low_latency_pulse_output_args("silence_keepalive", rate, channels, 5)
        silence = ManagedProcess(
            "silence-keepalive",
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout={layout}:sample_rate={rate}",
                "-ar",
                str(rate),
                "-ac",
                str(channels),
                *pulse_args,
                "snap_hub_mix",
            ],
            env={**PULSE_ENV, "PULSE_PROP": "media.role=music application.name=silence_keepalive", "PULSE_LATENCY_MSEC": "5"},
        )
        self.processes["silence-keepalive"] = silence
        await silence.start()

    async def _start_level_monitor(self) -> None:
        if self.level_task and not self.level_task.done():
            return
        self.level_task = asyncio.create_task(self._level_monitor_loop())

    async def _level_monitor_loop(self) -> None:
        rate = 16000
        chunk_bytes = 1600
        while True:
            proc = await asyncio.create_subprocess_exec(
                "parec",
                "-d",
                "snap_hub_mix.monitor",
                "--latency-msec=5",
                "--process-time-msec=5",
                "--raw",
                "--format=s16le",
                "--rate",
                str(rate),
                "--channels=1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**PULSE_ENV, "PULSE_LATENCY_MSEC": "5"},
            )
            self.level_proc = proc
            try:
                while proc.stdout:
                    raw = await proc.stdout.read(chunk_bytes)
                    if not raw:
                        break
                    level, peak = pcm16_level(raw)
                    result = {
                        "ok": True,
                        "state": signal_state(level, peak),
                        "level": level,
                        "peak": peak,
                        "mode": self.wired_capture_mode,
                        "stage": "realtime_mixer",
                    }
                    self.current_input_level = result
                    self.current_input_level_at = time.monotonic()
                    self.last_input_level = result
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.terminate()
                raise
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()
                if self.level_proc is proc:
                    self.level_proc = None
            await asyncio.sleep(0.2)

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
        backend = self.config["wired"].get("capture_backend", "auto")
        capture_latency = int(self.config["wired"].get("latency_ms", min(latency, 20)))
        host_pulse_tried = False

        if backend == "auto" and await self._try_exclusive_alsa_after_host_release(devices, candidates):
            return

        if backend == "auto" and host_pulse_env_candidates():
            host_pulse_tried = True
            if await self._start_host_pulse_bridge(devices, capture_latency):
                self.wired_source_loaded = True
                self.wired_device = "haos_pulse_source"
                self.wired_capture_mode = "haos_pulse_bridge"
                self.wired_busy = False
                return

        if backend in ("auto", "direct_alsa"):
            for candidate in candidates:
                if await self._start_ffmpeg_alsa_bridge(candidate):
                    self.wired_source_loaded = True
                    self.wired_device = candidate
                    self.wired_capture_mode = "ffmpeg_alsa_bridge"
                    self.wired_busy = False
                    return
                if self.wired_error:
                    errors.append(f"{candidate}: {self.wired_error}")

        if backend == "auto" and any(is_busy_error(error) for error in errors):
            if await self._try_exclusive_alsa_after_host_release(devices, candidates):
                return

        if backend == "direct_alsa":
            self.wired_busy = any(is_busy_error(error) for error in errors)
            self.wired_capture_mode = "busy" if self.wired_busy else "none"
            self.wired_error = concise_error("Direct ALSA capture could not be attached.", errors) if errors else "Direct ALSA capture did not start."
            LOG.warning("%s", self.wired_error)
            return

        if backend in ("auto", "haos_pulse") and not host_pulse_tried and await self._start_host_pulse_bridge(devices, capture_latency):
            self.wired_source_loaded = True
            self.wired_device = "haos_pulse_source"
            self.wired_capture_mode = "haos_pulse_bridge"
            self.wired_busy = False
            return

        if backend == "haos_pulse":
            self.wired_error = self.wired_error or self.host_pulse_error or "HAOS PulseAudio capture bridge did not start."
            LOG.warning("%s", self.wired_error)
            return

        if backend == "auto" and not errors:
            for candidate in candidates:
                if await self._start_ffmpeg_alsa_bridge(candidate):
                    self.wired_source_loaded = True
                    self.wired_device = candidate
                    self.wired_capture_mode = "ffmpeg_alsa_bridge"
                    self.wired_busy = False
                    return
                if self.wired_error:
                    errors.append(f"{candidate}: {self.wired_error}")

        if backend == "auto" and not self.wired_source_loaded and self.host_pulse_error:
            errors.append(f"haos_pulse: {self.host_pulse_error}")

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
        if self.wired_source_loaded and self.wired_capture_mode in ("pulseaudio_source", "haos_pulse_bridge"):
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
        self.host_pulse_bridge_engine = ""
        self.wired_bridge_engine = ""

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
                loopback_args = ["load-module", "module-loopback", f"source={source_name}", "sink=snap_hub_mix", f"latency_msec={max(5, min(20, latency))}"]
                if self.config["audio"]["routing_mode"] == "fallback_duck" or self.config.get("music_assistant", {}).get("ducking_enabled"):
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
        if await self._start_arecord_alsa_bridge(device):
            return True
        cfg = self.config
        rate = int(cfg["audio"]["sample_rate"])
        channels = int(cfg["audio"]["channels"])
        pulse_args = low_latency_pulse_output_args("wired_alsa_bridge", rate, channels, int(cfg["wired"].get("latency_ms", 8)))
        bridge = ManagedProcess(
            "wired-alsa-bridge",
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-f",
                "alsa",
                "-thread_queue_size",
                "32",
                "-i",
                device,
                "-af",
                "aresample=async=1:first_pts=0:min_hard_comp=0.05",
                "-ar",
                str(rate),
                "-ac",
                str(channels),
                *pulse_args,
                "snap_hub_mix",
            ],
            env={**PULSE_ENV, "PULSE_PROP": "media.role=phone application.name=wired_alsa_bridge", "PULSE_LATENCY_MSEC": "5"},
            quiet_substrings=[
                "cannot open audio device",
                "Error opening input",
                "Error opening input file",
                "Error opening input files",
                "ALSA lib pcm_dsnoop",
                "ALSA lib conf",
                "ALSA buffer xrun",
            ],
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
        self.wired_bridge_engine = "ffmpeg_pulse"
        LOG.info("wired input attached through FFmpeg ALSA bridge using %s", device)
        return True

    async def _start_arecord_alsa_bridge(self, device: str) -> bool:
        cfg = self.config
        rate = int(cfg["audio"]["sample_rate"])
        channels = int(cfg["audio"]["channels"])
        latency = max(5, min(20, int(cfg["wired"].get("latency_ms", 8))))
        period_us = max(2500, latency * 1000 // 2)
        buffer_us = max(10000, latency * 1000 * 2)
        command = (
            f"arecord -q -D {shlex.quote(device)} -t raw -f S16_LE -r {rate} -c {channels} "
            f"--period-time={period_us} --buffer-time={buffer_us} "
            f"| PULSE_SERVER={shlex.quote(PULSE_ENV['PULSE_SERVER'])} PULSE_LATENCY_MSEC=5 "
            f"PULSE_PROP='media.role=phone application.name=wired_alsa_bridge' "
            f"pacat --latency-msec=5 --process-time-msec=5 --raw --format=s16le --rate={rate} --channels={channels} -d snap_hub_mix"
        )
        bridge = ManagedProcess(
            "wired-alsa-bridge",
            ["bash", "-o", "pipefail", "-lc", command],
            env=PULSE_ENV,
            quiet_substrings=[
                "Recording raw data",
                "overrun",
                "xrun",
                "arecord: main:",
                "Device or resource busy",
            ],
        )
        self.processes["wired-alsa-bridge"] = bridge
        await bridge.start()
        await asyncio.sleep(0.8)
        if not bridge.running():
            self.wired_error = "\n".join(bridge.last_output[-6:]) or "arecord ALSA bridge exited immediately"
            self.processes.pop("wired-alsa-bridge", None)
            return False
        self.wired_error = ""
        self.wired_bridge_engine = "arecord_pacat"
        LOG.info("wired input attached through arecord ALSA bridge using %s", device)
        return True

    async def latency_report(self) -> dict:
        local = await pulse_latency_snapshot(PULSE_ENV)
        host = await pulse_latency_snapshot(self.host_pulse_env) if self.host_pulse_env else None
        report = {
            "capture": {
                "attached": self.wired_source_loaded,
                "mode": self.wired_capture_mode,
                "engine": self.wired_bridge_engine or self.host_pulse_bridge_engine,
                "device": self.wired_device,
                "host_source": self.host_pulse_source,
                "bridge_engine": self.host_pulse_bridge_engine,
                "host_source_suspended_for_direct_alsa": self.host_pulse_source_suspended,
                "error": self.wired_error,
            },
            "configured_ms": {
                "wired": self.config["wired"].get("latency_ms"),
                "audio": self.config["audio"].get("latency_ms"),
                "snapcast_buffer": self.config["snapcast"].get("buffer_ms"),
                "snapcast_chunk": self.config["snapcast"].get("chunk_ms"),
            },
            "local_pulse": local,
            "host_pulse": host,
        }
        report["summary"] = latency_summary(report)
        return report

    async def _start_host_pulse_bridge(self, devices: dict, latency_ms: int) -> bool:
        host_env = await self._host_pulse_env()
        if not host_env:
            return False
        source = await self._select_host_pulse_source(devices, host_env)
        if not source:
            return False
        await self._prepare_host_pulse_source(host_env, source)
        cfg = self.config
        bridges = (
            (
                "ffmpeg_pulse",
                host_pulse_ffmpeg_bridge_command(host_env, source, cfg["audio"]["sample_rate"], cfg["audio"]["channels"], latency_ms),
                {},
            ),
            (
                "parec_pacat",
                ["bash", "-o", "pipefail", "-lc", host_pulse_bridge_command(host_env, source, cfg["audio"]["sample_rate"], cfg["audio"]["channels"], cfg["audio"]["format"], latency_ms)],
                PULSE_ENV,
            ),
        )
        last_error = ""
        for engine, command, env in bridges:
            bridge = ManagedProcess(
                "host-pulse-capture-bridge",
                command,
                env=env,
                quiet_substrings=["Failed to create secure directory"],
            )
            self.processes["host-pulse-capture-bridge"] = bridge
            await bridge.start()
            await asyncio.sleep(0.8)
            if bridge.running():
                self.wired_error = ""
                self.host_pulse_env = host_env
                self.host_pulse_source = source
                self.host_pulse_bridge_engine = engine
                LOG.info("wired input attached through HAOS PulseAudio source %s using %s", source, engine)
                return True
            last_error = "\n".join(bridge.last_output[-8:]) or "HAOS PulseAudio capture bridge exited immediately"
            LOG.warning("HAOS PulseAudio %s bridge failed for %s: %s", engine, source, last_error)
            self.processes.pop("host-pulse-capture-bridge", None)
        self.wired_error = last_error
        self.host_pulse_error = last_error
        return False

    async def _prepare_host_pulse_source(self, host_env: dict[str, str], source: str) -> None:
        await run_checked(["pactl", "suspend-source", source, "0"], timeout=4, env=host_env)
        await run_checked(["pactl", "set-source-mute", source, "0"], timeout=4, env=host_env)
        await run_checked(["pactl", "set-source-volume", source, "100%"], timeout=4, env=host_env)
        self.host_pulse_source_suspended = False

    async def _release_host_pulse_source(self, host_env: dict[str, str], source: str) -> None:
        await run_checked(["pactl", "set-source-mute", source, "0"], timeout=4, env=host_env)
        await run_checked(["pactl", "suspend-source", source, "1"], timeout=4, env=host_env)
        await asyncio.sleep(0.2)

    async def _host_pulse_env(self) -> dict[str, str] | None:
        for env in host_pulse_env_candidates():
            rc, out = await run_checked(["pactl", "info"], timeout=3, env=env)
            if rc == 0:
                self.host_pulse_error = ""
                LOG.info("HAOS PulseAudio service detected at %s", env.get("PULSE_SERVER"))
                return env
            self.host_pulse_error = out.strip()
        return None

    async def _select_host_pulse_source(self, devices: dict, host_env: dict[str, str]) -> str | None:
        if not host_env:
            return None
        rc, out = await run_checked(["pactl", "list", "sources", "short"], timeout=4, env=host_env)
        if rc != 0:
            self.host_pulse_error = out.strip()
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
            self.host_pulse_error = "HAOS PulseAudio is reachable, but it exposes no capture source"
            return None
        tokens = host_pulse_match_tokens(devices)
        for source in sources:
            lowered = source.lower()
            if any(token and token in lowered for token in tokens):
                return source
        selected = sources[0]
        LOG.info("using first HAOS PulseAudio capture source %s; available sources: %s", selected, ", ".join(sources))
        return selected

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
        await self.set_volume("music", self.config["music_assistant"]["music_volume"])
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
        elif target == "music":
            await self._set_sink_input_volume(["ma_snapcast_tap", "ma_music_tap"], percent)
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
        if self.level_task:
            self.level_task.cancel()
            try:
                await self.level_task
            except asyncio.CancelledError:
                pass
            self.level_task = None
        if self.level_proc and self.level_proc.returncode is None:
            self.level_proc.terminate()
            try:
                await asyncio.wait_for(self.level_proc.wait(), timeout=2)
            except TimeoutError:
                self.level_proc.kill()
                await self.level_proc.wait()
            self.level_proc = None
        if self.host_pulse_source_suspended and self.host_pulse_env and self.host_pulse_source:
            await run_checked(["pactl", "suspend-source", self.host_pulse_source, "0"], timeout=4, env=self.host_pulse_env)
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
        self.host_pulse_error = ""
        self.host_pulse_env = None
        self.host_pulse_source = None
        self.host_pulse_bridge_engine = ""
        self.wired_bridge_engine = ""
        self.last_input_level = None
        self.current_input_level = None
        self.current_input_level_at = 0.0
        self.host_pulse_source_suspended = False

    async def _try_exclusive_alsa_after_host_release(self, devices: dict, candidates: list[str]) -> bool:
        host_env = await self._host_pulse_env()
        if not host_env:
            return False
        source = await self._select_host_pulse_source(devices, host_env)
        if not source:
            return False
        await self._release_host_pulse_source(host_env, source)
        for candidate in candidates:
            if await self._start_ffmpeg_alsa_bridge(candidate):
                self.wired_source_loaded = True
                self.wired_device = candidate
                self.wired_capture_mode = "ffmpeg_alsa_bridge"
                self.wired_busy = False
                self.host_pulse_env = host_env
                self.host_pulse_source = source
                self.host_pulse_bridge_engine = "exclusive_alsa"
                self.host_pulse_source_suspended = True
                LOG.info("wired input attached directly through ALSA after releasing HAOS PulseAudio source %s", source)
                return True
        await self._prepare_host_pulse_source(host_env, source)
        return False

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
        if self.wired_capture_mode == "haos_pulse_bridge":
            host_bridge = self.processes.get("host-pulse-capture-bridge")
            if not host_bridge or not host_bridge.running():
                self.wired_source_loaded = False
                self.wired_error = "\n".join(host_bridge.last_output[-6:]) if host_bridge else "HAOS PulseAudio bridge is not running"
                return {
                    "ok": False,
                    "state": "capture_bridge_stopped",
                    "level": 0,
                    "peak": 0,
                    "mode": self.wired_capture_mode,
                    "source": self.host_pulse_source,
                    "stage": "haos_bridge",
                    "error": self.wired_error,
                }
            mix_level = await self._mix_monitor_level()
            return {
                **mix_level,
                "source": self.host_pulse_source,
                "stage": "snapcast_mix",
                "bridge_engine": self.host_pulse_bridge_engine,
                "bridge": "running",
            }
        if self.current_input_level and time.monotonic() - self.current_input_level_at < 1.0:
            return {**self.current_input_level, "engine": self.wired_bridge_engine or self.host_pulse_bridge_engine}
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
            return await self._mix_monitor_level()
        if not raw:
            error = err.decode(errors="replace")[:400] or self.wired_error or "no PCM bytes received from mixer monitor"
            return {"ok": False, "state": "capture_unavailable", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": error}
        level, peak = pcm16_level(raw)
        return {"ok": rc in (0, 124), "state": signal_state(level, peak), "level": level, "peak": peak, "mode": self.wired_capture_mode}

    async def _host_pulse_input_level(self) -> dict:
        if not self.host_pulse_env or not self.host_pulse_source:
            return {"ok": False, "state": "host_source_missing", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": "HAOS PulseAudio source is not selected"}
        command = [
            "parec",
            "-d",
            self.host_pulse_source,
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
        ]
        rc, raw, err = await run_binary(command, timeout=1, env=self.host_pulse_env, max_bytes=64000)
        if not raw:
            error = err.decode(errors="replace")[:400] or "host source probe returned no PCM bytes"
            return {"ok": False, "state": "host_probe_no_bytes", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "source": self.host_pulse_source, "error": error}
        level, peak = pcm16_level(raw)
        return {"ok": rc in (0, 124), "state": signal_state(level, peak), "level": level, "peak": peak, "mode": self.wired_capture_mode, "source": self.host_pulse_source, "stage": "haos_source"}

    async def _mix_monitor_level(self) -> dict:
        command = [
            "parec",
            "-d",
            "snap_hub_mix.monitor",
            "--latency-msec=5",
            "--raw",
            "--format=s16le",
            "--rate",
            "16000",
            "--channels=1",
        ]
        rc, raw, err = await run_binary(command, timeout=0.5, env=PULSE_ENV, max_bytes=32000)
        if not raw:
            if self.last_input_level:
                return {**self.last_input_level, "stale": True}
            error = err.decode(errors="replace")[:400] or self.wired_error or "no PCM bytes received from mixer monitor"
            return {"ok": False, "state": "capture_unavailable", "level": 0, "peak": 0, "mode": self.wired_capture_mode, "error": error}
        level, peak = pcm16_level(raw)
        result = {"ok": rc in (0, 124), "state": signal_state(level, peak), "level": level, "peak": peak, "mode": self.wired_capture_mode, "stage": "mixer"}
        self.last_input_level = result
        return result

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


def host_pulse_env_candidates() -> list[dict[str, str]]:
    candidates = []
    servers = []
    env_server = os.environ.get("AUDIO_HUB_HOST_PULSE_SERVER", "")
    if env_server and env_server != PULSE_ENV["PULSE_SERVER"] and str(PULSE_SOCKET) not in env_server:
        servers.append(env_server)
    for socket in HOST_PULSE_SOCKET_CANDIDATES:
        if Path(socket).exists():
            servers.append(f"unix:{socket}")
    for server in dedupe(servers):
        env = {"PULSE_SERVER": server}
        cookie = os.environ.get("AUDIO_HUB_HOST_PULSE_COOKIE", "")
        runtime = os.environ.get("AUDIO_HUB_HOST_PULSE_RUNTIME_PATH", "")
        if cookie:
            env["PULSE_COOKIE"] = cookie
        if runtime and str(PULSE_DIR) not in runtime:
            env["PULSE_RUNTIME_PATH"] = runtime
        candidates.append(env)
    return candidates


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


def host_pulse_bridge_command(host_env: dict[str, str], source: str, rate: int, channels: int, audio_format: str, latency_ms: int) -> str:
    host_server = shlex.quote(host_env.get("PULSE_SERVER", ""))
    host_cookie = shlex.quote(host_env.get("PULSE_COOKIE", ""))
    host_runtime = shlex.quote(host_env.get("PULSE_RUNTIME_PATH", ""))
    local_server = shlex.quote(PULSE_ENV["PULSE_SERVER"])
    source_arg = shlex.quote(source)
    cookie_part = f" PULSE_COOKIE={host_cookie}" if host_cookie != "''" else " PULSE_COOKIE="
    runtime_part = f" PULSE_RUNTIME_PATH={host_runtime}" if host_runtime != "''" else " PULSE_RUNTIME_PATH="
    latency = max(5, min(20, int(latency_ms)))
    return (
        f"PULSE_SERVER={host_server}{cookie_part}{runtime_part} PULSE_LATENCY_MSEC={latency} "
        f"parec -d {source_arg} --latency-msec={latency} --process-time-msec=5 --raw --format={audio_format} --rate={int(rate)} --channels={int(channels)} "
        f"| PULSE_SERVER={local_server} PULSE_LATENCY_MSEC={latency} PULSE_PROP='media.role=phone application.name=host_pulse_bridge' "
        f"pacat --latency-msec={latency} --process-time-msec=5 --raw --format={audio_format} --rate={int(rate)} --channels={int(channels)} -d snap_hub_mix"
    )


def host_pulse_ffmpeg_bridge_command(host_env: dict[str, str], source: str, rate: int, channels: int, latency_ms: int) -> list[str]:
    latency = max(5, min(20, int(latency_ms)))
    host_server = host_env.get("PULSE_SERVER", "")
    pulse_args = low_latency_pulse_output_args("host_pulse_bridge", rate, channels, latency)
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "pulse",
        "-name",
        "audio_hub_host_capture",
        "-thread_queue_size",
        "32",
        "-server",
        host_server,
        "-fragment_size",
        str(fragment_bytes),
        "-i",
        source,
        "-ac",
        str(int(channels)),
        "-ar",
        str(int(rate)),
        *pulse_args,
        "snap_hub_mix",
    ]


def low_latency_pulse_output_args(stream_name: str, rate: int, channels: int, latency_ms: int) -> list[str]:
    latency = max(5, min(20, int(latency_ms)))
    bytes_per_frame = int(channels) * 2
    fragment_bytes = max(480, int(rate) * bytes_per_frame * latency // 1000)
    minreq_bytes = max(240, fragment_bytes // 2)
    return [
        "-f",
        "pulse",
        "-server",
        PULSE_ENV["PULSE_SERVER"],
        "-name",
        stream_name,
        "-stream_name",
        stream_name,
        "-fragment_size",
        str(fragment_bytes),
        "-buffer_duration",
        str(max(10, latency)),
        "-prebuf",
        "0",
        "-minreq",
        str(minreq_bytes),
    ]


async def pulse_latency_snapshot(env: dict[str, str] | None) -> dict:
    if not env:
        return {"available": False, "error": "PulseAudio environment is not available"}
    snapshot = {"available": True, "max_reported_ms": 0.0, "sections": {}}
    for section in ("sources", "source-outputs", "sinks", "sink-inputs"):
        rc, out = await run_checked(["pactl", "list", section], timeout=5, env=env)
        if rc != 0:
            snapshot["sections"][section] = {"error": out.strip()}
            continue
        items = parse_pactl_latency_items(out)
        snapshot["sections"][section] = items
        for item in items:
            snapshot["max_reported_ms"] = max(snapshot["max_reported_ms"], item.get("max_latency_ms", 0.0))
    snapshot["max_reported_ms"] = round(snapshot["max_reported_ms"], 2)
    return snapshot


def parse_pactl_latency_items(text: str) -> list[dict]:
    items: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        header = re.match(r"^(Sink Input|Source Output|Sink|Source) #(\d+)$", stripped)
        if header:
            if current:
                finalize_latency_item(current)
                items.append(current)
            current = {"kind": header.group(1), "id": header.group(2), "name": "", "properties": {}, "latencies_ms": []}
            continue
        if current is None:
            continue
        if stripped.startswith("Name:") or stripped.startswith("Description:") or stripped.startswith("Driver:"):
            key, value = stripped.split(":", 1)
            current[key.lower()] = value.strip()
        elif stripped.startswith("Sink:") or stripped.startswith("Source:"):
            key, value = stripped.split(":", 1)
            current[key.lower()] = value.strip()
        elif "application.name" in stripped or "device.description" in stripped or "media.role" in stripped:
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                current["properties"][parts[0].strip()] = parts[1].strip().strip('"')
        elif "Latency:" in stripped or "latency:" in stripped:
            current["latencies_ms"].extend(parse_latency_values_ms(stripped))
    if current:
        finalize_latency_item(current)
        items.append(current)
    return items


def finalize_latency_item(item: dict) -> None:
    values = item.get("latencies_ms", [])
    item["max_latency_ms"] = round(max(values), 2) if values else 0.0
    item["latencies_ms"] = [round(value, 2) for value in values]


def parse_latency_values_ms(text: str) -> list[float]:
    values = []
    for value, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(usec|ms)", text):
        amount = float(value)
        values.append(amount / 1000.0 if unit == "usec" else amount)
    return values


def latency_summary(report: dict) -> dict:
    local_snapshot = report.get("local_pulse", {})
    raw_local_ms = float(local_snapshot.get("max_reported_ms", 0.0) or 0.0)
    final_mix_ms = final_mix_latency_ms(local_snapshot)
    local_ms = final_mix_ms if final_mix_ms is not None else raw_local_ms
    host_ms = float((report.get("host_pulse") or {}).get("max_reported_ms", 0.0) or 0.0)
    snap_ms = float(report.get("configured_ms", {}).get("snapcast_buffer") or 0.0)
    capture = report.get("capture", {})
    mode = capture.get("mode")
    if host_ms >= 500 and mode == "haos_pulse_bridge":
        likely = "host_pulse_capture_buffer"
        detail = "HAOS PulseAudio is reporting a large capture/source-output latency before the add-on receives the microphone."
    elif local_ms >= 500:
        likely = "addon_pulse_mixer_buffer"
        detail = "The final AudioHub mix path is reporting a large local PulseAudio buffer."
    elif snap_ms >= 500:
        likely = "snapcast_transport_buffer"
        detail = "Snapcast is configured with a large transport buffer."
    elif mode == "haos_pulse_bridge":
        likely = "host_pulse_or_wireless_mic_path"
        detail = "The add-on buffers are low; remaining delay is likely in HAOS PulseAudio or the wireless mic receiver path."
    else:
        likely = "outside_addon_capture_pipeline"
        detail = "The add-on capture/mixer/Snapcast buffers are low; remaining delay is likely downstream or in the microphone hardware."
    return {
        "likely_cause": likely,
        "detail": detail,
        "max_local_pulse_ms": round(raw_local_ms, 2),
        "final_mix_pulse_ms": round(local_ms, 2),
        "max_host_pulse_ms": round(host_ms, 2),
        "configured_snapcast_buffer_ms": snap_ms,
    }


def final_mix_latency_ms(snapshot: dict) -> float | None:
    if not snapshot.get("available"):
        return None
    sections = snapshot.get("sections", {})
    sinks = sections.get("sinks") if isinstance(sections.get("sinks"), list) else []
    sources = sections.get("sources") if isinstance(sections.get("sources"), list) else []
    sink_inputs = sections.get("sink-inputs") if isinstance(sections.get("sink-inputs"), list) else []
    snap_sink_ids = {item.get("id") for item in sinks if item.get("name") == "snap_hub_mix"}
    values = []
    for item in sinks:
        if item.get("name") == "snap_hub_mix":
            values.append(item.get("max_latency_ms", 0.0))
    for item in sources:
        if item.get("name") == "snap_hub_mix.monitor":
            values.append(item.get("max_latency_ms", 0.0))
    for item in sink_inputs:
        app_name = item.get("properties", {}).get("application.name", "")
        if item.get("sink") in snap_sink_ids and app_name != "silence_keepalive":
            values.append(item.get("max_latency_ms", 0.0))
    return max(values) if values else None


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


def signal_state(level: float, peak: float) -> str:
    return "signal" if level > 0.003 or peak > 0.02 else "quiet"
