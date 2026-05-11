#!/usr/bin/env python3
import asyncio
import json
import logging
from pathlib import Path

from process import ManagedProcess, run_checked
from pulseaudio import FIFO

LOG = logging.getLogger("snapcast")
CONFIG_PATH = Path("/tmp/audio-hub/snapserver.conf")


class SnapcastManager:
    def __init__(self, config: dict):
        self.config = config
        self.process: ManagedProcess | None = None

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
        )
        CONFIG_PATH.write_text(
            "\n".join([
                "[server]",
                f"threads = -1",
                "",
                "[http]",
                "enabled = true",
                f"bind_to_address = 0.0.0.0",
                f"port = {cfg['snapcast']['http_port']}",
                "",
                "[tcp]",
                "enabled = true",
                f"bind_to_address = 0.0.0.0",
                f"port = {cfg['snapcast']['jsonrpc_port']}",
                "",
                "[stream]",
                f"bind_to_address = 0.0.0.0",
                f"port = {cfg['snapcast']['client_stream_port']}",
                f"source = {source}",
                f"buffer = {cfg['snapcast']['buffer_ms']}",
                "",
            ]),
            encoding="utf-8",
        )
        self.process = ManagedProcess("snapserver", ["snapserver", "--config", str(CONFIG_PATH)])
        await self.process.start()

    async def stop(self) -> None:
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


def bits_from_format(fmt: str) -> int:
    if "24" in fmt:
        return 24
    if "32" in fmt or "float" in fmt:
        return 32
    return 16
