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


def create_app(status_provider: StatusProvider, patch_handler: PatchHandler, restart_handler: ActionHandler, retry_wired_handler: ActionHandler, reload_devices_handler: ActionHandler, remove_entities_handler: ActionHandler, input_level_handler: ActionHandler, monitor_clip_handler: ClipHandler, snapcast_action_handler: PayloadActionHandler) -> web.Application:
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

    async def monitor_clip(request):
        clip = await monitor_clip_handler()
        return web.Response(body=clip, content_type="audio/wav")

    async def live_stream(request):
        return await live_encoded_stream(request, "mp3")

    async def live_opus_stream(request):
        return await live_encoded_stream(request, "opus")

    async def live_encoded_stream(request, codec: str):
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/ogg; codecs=opus" if codec == "opus" else "audio/mpeg",
                "Cache-Control": "no-store",
                "Connection": "keep-alive",
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
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "pulse",
            "-fragment_size",
            "960",
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
                "-f",
                "ogg",
                "-flush_packets",
                "1",
                "-",
            ]
        return [
            *base,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "4",
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
    app.router.add_get("/api/monitor.wav", monitor_clip)
    app.router.add_get("/{prefix:.+}/api/monitor.wav", monitor_clip)
    app.router.add_get("/api/live.mp3", live_stream)
    app.router.add_get("/{prefix:.+}/api/live.mp3", live_stream)
    app.router.add_get("/api/live.opus", live_opus_stream)
    app.router.add_get("/{prefix:.+}/api/live.opus", live_opus_stream)
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
