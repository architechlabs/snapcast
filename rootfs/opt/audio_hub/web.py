#!/usr/bin/env python3
from pathlib import Path
from typing import Awaitable, Callable, Any

from aiohttp import web

StatusProvider = Callable[[], Awaitable[dict[str, Any]]]
PatchHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ActionHandler = Callable[[], Awaitable[dict[str, Any]]]
ClipHandler = Callable[[], Awaitable[bytes]]


def create_app(status_provider: StatusProvider, patch_handler: PatchHandler, restart_handler: ActionHandler, retry_wired_handler: ActionHandler, reload_devices_handler: ActionHandler, remove_entities_handler: ActionHandler, input_level_handler: ActionHandler, monitor_clip_handler: ClipHandler) -> web.Application:
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

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/{prefix:.+}/api/status", status)
    app.router.add_get("/api/input-level", input_level)
    app.router.add_get("/{prefix:.+}/api/input-level", input_level)
    app.router.add_get("/api/monitor.wav", monitor_clip)
    app.router.add_get("/{prefix:.+}/api/monitor.wav", monitor_clip)
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
