"""Browser CLI executor — runs playwright-cli commands against a remote CDP session.

Pattern (from reference sample):
1. First call: pass CDP URL via PLAYWRIGHT_MCP_CDP_ENDPOINT env var (open about:blank)
2. Subsequent calls: do NOT pass CDP URL — the -s= session daemon persists the connection
3. Uses asyncio.create_subprocess_exec (async) — matches the working sample exactly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLAYWRIGHT_CLI_COMMANDS = {
    "state", "screenshot", "snapshot",
    "click", "dblclick", "rightclick", "hover",
    "type", "input", "keys", "select", "upload",
    "scroll", "back", "eval",
    "tab", "get", "wait",
    "goto", "go-back", "go-forward", "reload",
    "press", "keydown", "keyup",
    "fill", "check", "uncheck",
    "tab-list", "tab-new", "tab-close", "tab-select",
    "resize", "mousemove", "mousedown", "mouseup", "mousewheel",
}

TOKEN_PATTERNS = [
    (re.compile(r"(accessKey=)[^&\s\"']+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"\beyJ[a-zA-Z0-9._-]+\b"), "<redacted-token>"),
    (re.compile(r"wss://[^\s\"']+"), "wss://<redacted>"),
]


class BrowserExecutorError(RuntimeError):
    """Raised when a browser command cannot be executed safely."""


def _make_subprocess_env() -> dict[str, str]:
    """Build env dict for subprocess — matches the sample's make_subprocess_env."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Ensure scripts dir is in PATH
    scripts_path = Path(sys.executable).parent
    env["PATH"] = str(scripts_path) + os.pathsep + env.get("PATH", "")
    return env


def _resolve_playwright_cli(env: dict[str, str]) -> str:
    """Find playwright-cli binary."""
    return shutil.which("playwright-cli", path=env.get("PATH", "")) or "playwright-cli"


def _redact(text: str) -> str:
    for pattern, replacement in TOKEN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


class BrowserExecutor:
    """Executes playwright-cli commands against a remote CDP session (async).

    First call (connect) passes CDP URL via env var.
    Subsequent calls use -s= session persistence — no CDP URL passed.
    """

    def __init__(
        self,
        session_id: str = "default",
        *,
        command_timeout_seconds: int | None = None,
        max_output_chars: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.command_timeout_seconds = command_timeout_seconds or int(
            os.getenv("PLAYWRIGHT_CLI_TIMEOUT_SECONDS", "180")
        )
        self.max_output_chars = max_output_chars or int(
            os.getenv("BROWSER_MAX_OUTPUT_CHARS", "15000")
        )
        self._connected = False
        self._cdp_url: str | None = None
        self._project_root = Path.cwd()

    async def connect(self, cdp_url: str) -> dict[str, Any]:
        """Connect playwright-cli to the remote browser via attach --cdp (the documented way)."""
        self._cdp_url = cdp_url
        logger.info("CONNECT: attaching playwright-cli to CDP for session %s", self.session_id)
        # Use 'attach --cdp=<url>' which is the documented way to connect to a remote browser
        # Then the session persists and subsequent commands work without re-specifying CDP
        result = await self._run_async(
            command=f"attach --cdp={cdp_url}",
            include_cdp_env=False,
            timeout=90,
        )
        logger.info("CONNECT result: success=%s exit_code=%s stdout=%.500s stderr=%.500s",
                    result.get("success"), result.get("exit_code"),
                    result.get("stdout", ""), result.get("stderr", ""))
        if result["success"]:
            self._connected = True
        return result

    async def run_command(self, command: str, args: list[str] | None = None) -> dict[str, Any]:
        """Run a browser command. Never passes CDP URL — session handles it."""
        normalized = command.strip()
        if normalized not in PLAYWRIGHT_CLI_COMMANDS:
            raise BrowserExecutorError(f"Command '{normalized}' is not allowed.")

        if not self._connected:
            raise BrowserExecutorError("Browser is not connected. Call connect() first.")

        command_args = args or []
        self._validate_args(command_args)

        full_command = normalized + (" " + " ".join(command_args) if command_args else "")
        logger.info("Browser command: %s", _redact(full_command))
        result = await self._run_async(command=full_command, include_cdp_env=False)
        logger.info("Command result: success=%s exit_code=%s stdout=%.300s stderr=%.300s",
                    result.get("success"), result.get("exit_code"),
                    result.get("stdout", ""), result.get("stderr", ""))
        return result

    def is_process_alive(self) -> bool:
        """Check if the session is still connected."""
        return self._connected

    async def _run_async(
        self,
        command: str,
        include_cdp_env: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run playwright-cli async subprocess — matches the sample's pattern exactly."""
        effective_timeout = timeout or self.command_timeout_seconds
        env = _make_subprocess_env()
        if include_cdp_env and self._cdp_url:
            env["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = self._cdp_url

        playwright_cli = _resolve_playwright_cli(env)
        cli_args = shlex.split(command)
        process_args = [playwright_cli, f"-s={self.session_id}", *cli_args]
        safe_cmd = _redact(" ".join(process_args))
        logger.info("[playwright-cli] timeout=%ds cmd=%s", effective_timeout, safe_cmd)

        try:
            process = await asyncio.create_subprocess_exec(
                *process_args,
                cwd=str(self._project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            raise BrowserExecutorError(f"Executable not found: {playwright_cli}")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = _redact(stdout_bytes.decode("utf-8", errors="replace"))
            stderr = _redact(stderr_bytes.decode("utf-8", errors="replace"))
            return {
                "success": False,
                "command": safe_cmd,
                "exit_code": -1,
                "stdout": _truncate(stdout, self.max_output_chars),
                "stderr": _truncate(stderr, self.max_output_chars),
                "timed_out": True,
            }

        stdout = _redact(stdout_bytes.decode("utf-8", errors="replace"))
        stderr = _redact(stderr_bytes.decode("utf-8", errors="replace"))
        return {
            "success": process.returncode == 0,
            "command": safe_cmd,
            "exit_code": process.returncode,
            "stdout": _truncate(stdout, self.max_output_chars),
            "stderr": _truncate(stderr, self.max_output_chars),
        }

    def _validate_args(self, args: list[str]) -> None:
        for arg in args:
            if not isinstance(arg, str):
                raise BrowserExecutorError("Command arguments must be strings.")
            if "\x00" in arg:
                raise BrowserExecutorError("Command arguments cannot contain NUL bytes.")
