#!/usr/bin/env python3
import asyncio
import logging
import os
import signal
from collections.abc import Sequence

LOG = logging.getLogger("process")


class ManagedProcess:
    def __init__(self, name: str, command: Sequence[str], env: dict[str, str] | None = None, quiet_substrings: Sequence[str] | None = None):
        self.name = name
        self.command = list(command)
        self.env = env or {}
        self.quiet_substrings = list(quiet_substrings or [])
        self.proc: asyncio.subprocess.Process | None = None
        self.log_task: asyncio.Task | None = None
        self.started = False
        self.last_output: list[str] = []

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
        self.log_task = asyncio.create_task(self._log_output(self.proc, self.proc.stdout))
        self.log_task.add_done_callback(self._consume_log_task_exception)

    def _consume_log_task_exception(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            LOG.debug("log task for %s ended with an error", self.name, exc_info=True)

    async def _log_output(self, proc: asyncio.subprocess.Process, stdout: asyncio.StreamReader | None) -> None:
        if not stdout:
            return
        while True:
            line = await stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if any(marker in text for marker in self.quiet_substrings):
                continue
            self.last_output.append(text)
            self.last_output = self.last_output[-20:]
            LOG.info("[%s] %s", self.name, text)

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
        if self.log_task:
            if not self.log_task.done():
                try:
                    await asyncio.wait_for(self.log_task, timeout=2)
                except TimeoutError:
                    self.log_task.cancel()
            self._consume_log_task_exception(self.log_task)
        self.proc = None
        self.log_task = None
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
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        return 124, "timeout"
    return proc.returncode or 0, output.decode(errors="replace")


async def run_binary(command: Sequence[str], timeout: int = 5, env: dict[str, str] | None = None, max_bytes: int = 262144) -> tuple[int, bytes, bytes]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=process_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        return 124, stdout[:max_bytes], stderr[:max_bytes]
    return proc.returncode or 0, stdout[:max_bytes], stderr[:max_bytes]
