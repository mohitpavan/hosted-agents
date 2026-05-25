"""Browser session manager — runs playwright-cli commands via subprocess.

Uses playwright-cli with session persistence (-s=<id>):
- First call: `attach --cdp=<url>` to connect to remote browser
- Subsequent calls: `goto`, `snapshot`, `click`, `fill`, etc.
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

logger = logging.getLogger(__name__)

# Commands allowed via playwright-cli
ALLOWED_COMMANDS = {
    "goto", "go-back", "go-forward", "reload",
    "snapshot", "screenshot",
    "click", "dblclick", "hover",
    "fill", "type", "press", "keys", "select", "check", "uncheck",
    "scroll", "eval",
    "tab-list", "tab-new", "tab-close", "tab-select",
    "wait",
    "state",
}

# Redaction patterns
_REDACT = [
    (re.compile(r"wss://[^\s\"']+"), "wss://<redacted>"),
    (re.compile(r"(accessKey=)[^&\s\"']+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"\beyJ[a-zA-Z0-9._-]{20,}\b"), "<token>"),
]


def _redact(text: str) -> str:
    for pat, rep in _REDACT:
        text = pat.sub(rep, text)
    return text


def _truncate(text: str, max_len: int = 12000) -> str:
    return text if len(text) <= max_len else text[:max_len] + "\n...[truncated]"


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _find_cli(env: dict[str, str]) -> str:
    return shutil.which("playwright-cli", path=env.get("PATH", "")) or "playwright-cli"


class BrowserSession:
    """Manages a playwright-cli session against a remote CDP browser."""

    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self._connected = False
        self._cdp_url: str | None = None
        self._timeout = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "180"))

    def set_cdp_url(self, url: str):
        self._cdp_url = url

    async def run(self, command: str, args: list[str] | None = None) -> dict:
        """Run a playwright-cli command."""
        cmd = command.strip()
        cmd_args = args or []

        # Special "connect" command — attaches to CDP
        if cmd == "connect":
            cdp = cmd_args[0] if cmd_args else self._cdp_url
            if not cdp:
                return {"success": False, "error": "No CDP URL provided. Call create_session first."}
            return await self._exec(f"attach --cdp={cdp}")

        # Validate command
        if cmd not in ALLOWED_COMMANDS:
            return {"success": False, "error": f"Unknown command: {cmd}. Allowed: {sorted(ALLOWED_COMMANDS)}"}

        if not self._connected:
            return {"success": False, "error": f"NOT CONNECTED. You must call run_browser with command='connect' and args=['<cdp_url>'] FIRST before using '{cmd}'. The CDP URL comes from the create_session tool result."}

        # Build command string
        full_cmd = cmd
        if cmd_args:
            # Quote args that contain spaces
            quoted = [f'"{a}"' if " " in a else a for a in cmd_args]
            full_cmd += " " + " ".join(quoted)

        return await self._exec(full_cmd)

    async def close(self):
        """Disconnect the session."""
        if self._connected:
            await self._exec("detach")
        self._connected = False
        self._cdp_url = None

    async def _exec(self, command: str) -> dict:
        """Execute a playwright-cli subprocess."""
        env = _subprocess_env()
        cli = _find_cli(env)
        parts = [cli, f"-s={self.session_id}"] + shlex.split(command)
        safe_cmd = _redact(" ".join(parts))
        logger.info("[pw-cli] %s", safe_cmd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return {"success": False, "error": f"playwright-cli not found at: {cli}"}

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            return {
                "success": False,
                "error": "Command timed out",
                "stdout": _truncate(_redact(stdout_b.decode("utf-8", errors="replace"))),
                "stderr": _truncate(_redact(stderr_b.decode("utf-8", errors="replace"))),
            }

        stdout = _redact(stdout_b.decode("utf-8", errors="replace"))
        stderr = _redact(stderr_b.decode("utf-8", errors="replace"))
        success = proc.returncode == 0

        if success and "attach" in command:
            self._connected = True
            stdout = stdout or "Connected successfully to remote browser."

        return {
            "success": success,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr) if stderr else None,
            "exit_code": proc.returncode,
        }
