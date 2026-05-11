#!/usr/bin/env python3
import asyncio
import logging
import os
import signal
from collections.abc import Sequence

LOG = logging.getLogger("process")


class ManagedProcess:
    def __init__(self, name: str, command: Sequence[str], env: dict[str, str] | None = None):
        self.name = name
        self.command = list(command)
        self.env = env or {}
        self.proc: asyncio.subprocess.Process | None = None
        self.started = False

    async def start(self) -> None:
        await self.stop()
        env = os.environ.copy()
        env.update(self.env)
        LOG.info("starting %s: %s", self.name, " ".join(self.command))
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self.started = True
        asyncio.create_task(self._log_output())

    async def _log_output(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            LOG.info("[%s] %s", self.name, line.decode(errors="replace").rstrip())

    async def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.returncode is None:
            LOG.info("stopping %s", self.name)
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=8)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.proc = None
        self.started = False

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


async def run_checked(command: Sequence[str], timeout: int = 10, env: dict[str, str] | None = None) -> tuple[int, str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=process_env,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timeout"
    return proc.returncode or 0, output.decode(errors="replace")
