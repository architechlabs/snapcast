#!/usr/bin/env python3
import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Any

from aiohttp import web

from pulseaudio import PULSE_ENV

StatusProvider = Callable[[], Awaitable[dict[str, Any]]]
PatchHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ActionHandler = Callable[[], Awaitable[dict[str, Any]]]
PayloadActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ClipHandler = Callable[[], Awaitable[bytes]]


def create_app(status_provider: StatusProvider, patch_handler: PatchHandler, restart_handler: ActionHandler, retry_wired_handler: ActionHandler, reload_devices_handler: ActionHandler, remove_entities_handler: ActionHandler, input_level_handler: ActionHandler, monitor_clip_handler: ClipHandler, latency_report_handler: ActionHandler, snapcast_action_handler: PayloadActionHandler) -> web.Application:
    app = web.Application()
    static_root = Path("/var/www/audio_hub")

    async def index(request):
        return web.FileResponse(static_root / "index.html")

    async def status(request):
        return web.json_response(await status_provider())

    async def patch_config(request):
        payload = await request.json()
        return web.json_response(await patch_handler(payload))

    async def restart(request):
        return web.json_response(await restart_handler())

    async def retry_wired(request):
        return web.json_response(await retry_wired_handler())

    async def reload_devices(request):
        return web.json_response(await reload_devices_handler())

    async def remove_entities(request):
        return web.json_response(await remove_entities_handler())

    async def input_level(request):
        return web.json_response(await input_level_handler())

    async def latency_report(request):
        return web.json_response(await latency_report_handler())

    async def monitor_clip(request):
        clip = await monitor_clip_handler()
        return web.Response(body=clip, content_type="audio/wav")

    async def live_stream(request):
        return await live_encoded_stream(request, "mp3")

    async def live_opus_stream(request):
        return await live_encoded_stream(request, "opus")

    async def live_wav_stream(request):
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/wav",
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        proc = await asyncio.create_subprocess_exec(
            "parec",
            "-d",
            "snap_hub_mix.monitor",
            "--latency-msec=5",
            "--process-time-msec=5",
            "--raw",
            "--format=s16le",
            "--rate=48000",
            "--channels=2",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**PULSE_ENV, "PULSE_LATENCY_MSEC": "5"},
        )
        try:
            await response.write(wav_stream_header(48000, 2, 16))
            while proc.stdout:
                chunk = await proc.stdout.read(960)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        return response

    async def live_encoded_stream(request, codec: str):
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/ogg; codecs=opus" if codec == "opus" else "audio/mpeg",
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        command = live_encoder_command(codec)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=PULSE_ENV,
        )
        try:
            while proc.stdout:
                chunk = await proc.stdout.read(2048 if codec == "opus" else 8192)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
        return response

    def live_encoder_command(codec: str) -> list[str]:
        base = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
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
            "-fragment_size",
            "480",
            "-i",
            "snap_hub_mix.monitor",
            "-ac",
            "2",
            "-ar",
            "48000",
        ]
        if codec == "opus":
            return [
                *base,
                "-codec:a",
                "libopus",
                "-application",
                "lowdelay",
                "-frame_duration",
                "10",
                "-b:a",
                "96k",
                "-flush_packets",
                "1",
                "-page_duration",
                "10000",
                "-f",
                "ogg",
                "-",
            ]
        return [
            *base,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "4",
            "-flush_packets",
            "1",
            "-f",
            "mp3",
            "-",
        ]

    async def snapcast_action(request):
        payload = await request.json()
        return web.json_response(await snapcast_action_handler(payload))

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/{prefix:.+}/api/status", status)
    app.router.add_get("/api/input-level", input_level)
    app.router.add_get("/{prefix:.+}/api/input-level", input_level)
    app.router.add_get("/api/latency-report", latency_report)
    app.router.add_get("/{prefix:.+}/api/latency-report", latency_report)
    app.router.add_get("/api/monitor.wav", monitor_clip)
    app.router.add_get("/{prefix:.+}/api/monitor.wav", monitor_clip)
    app.router.add_get("/api/live.mp3", live_stream)
    app.router.add_get("/{prefix:.+}/api/live.mp3", live_stream)
    app.router.add_get("/api/live.opus", live_opus_stream)
    app.router.add_get("/{prefix:.+}/api/live.opus", live_opus_stream)
    app.router.add_get("/api/live.wav", live_wav_stream)
    app.router.add_get("/{prefix:.+}/api/live.wav", live_wav_stream)
    app.router.add_get("/live.mp3", live_stream)
    app.router.add_get("/live.opus", live_opus_stream)
    app.router.add_get("/live.wav", live_wav_stream)
    app.router.add_post("/api/snapcast/action", snapcast_action)
    app.router.add_post("/{prefix:.+}/api/snapcast/action", snapcast_action)
    app.router.add_patch("/api/config", patch_config)
    app.router.add_patch("/{prefix:.+}/api/config", patch_config)
    app.router.add_post("/api/restart", restart)
    app.router.add_post("/{prefix:.+}/api/restart", restart)
    app.router.add_post("/api/retry-wired", retry_wired)
    app.router.add_post("/{prefix:.+}/api/retry-wired", retry_wired)
    app.router.add_post("/api/reload-devices", reload_devices)
    app.router.add_post("/{prefix:.+}/api/reload-devices", reload_devices)
    app.router.add_post("/api/entities/remove", remove_entities)
    app.router.add_post("/{prefix:.+}/api/entities/remove", remove_entities)
    app.router.add_static("/static", static_root)
    return app


def wav_stream_header(sample_rate: int, channels: int, bits: int) -> bytes:
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = 0x7FFFFFF0
    riff_size = data_size + 36
    return (
        b"RIFF"
        + riff_size.to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + bits.to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )
