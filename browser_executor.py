from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Commands allowed for playwright-cli mode
PLAYWRIGHT_CLI_COMMANDS = {
    "open", "goto", "go-back", "go-forward", "reload",
    "type", "click", "dblclick", "fill", "drag", "drop", "hover",
    "select", "upload", "check", "uncheck",
    "snapshot", "eval", "press", "keydown", "keyup",
    "screenshot", "pdf",
    "tab-list", "tab-new", "tab-close", "tab-select",
    "console", "network",
    "dialog-accept", "dialog-dismiss",
    "resize", "mousemove", "mousedown", "mouseup", "mousewheel",
    "highlight", "generate-locator",
    "cookie-list", "cookie-get", "cookie-set", "cookie-delete", "cookie-clear",
    "localstorage-list", "localstorage-get", "localstorage-set", "localstorage-delete", "localstorage-clear",
    "sessionstorage-list", "sessionstorage-get", "sessionstorage-set", "sessionstorage-delete", "sessionstorage-clear",
    "route", "route-list", "unroute",
    "tracing-start", "tracing-stop",
    "run-code", "state-save", "state-load",
    "close",
}

# Commands allowed for browser-use mode
BROWSER_USE_COMMANDS = {
    "open", "state", "screenshot",
    "click", "dblclick", "rightclick", "hover",
    "type", "input", "keys", "select", "upload",
    "scroll", "back", "eval",
    "tab", "get", "wait",
    "close",
}

TOKEN_PATTERNS = [
    (re.compile(r"(accessKey=)[^&\s\"']+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(PLAYWRIGHT_SERVICE_ACCESS_TOKEN=)[^\s]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"\beyJ[a-zA-Z0-9._-]+\b"), "<redacted-token>"),
    (re.compile(r"wss://[^\s\"']+"), "wss://<redacted>"),
]


class BrowserExecutorError(RuntimeError):
    """Raised when a browser command cannot be executed safely."""


class BrowserExecutor:
    """Executes browser automation commands via playwright-cli or browser-use CLI."""

    def __init__(
        self,
        cli_mode: str = "playwright-cli",
        session_id: str = "default",
        *,
        command_timeout_seconds: int | None = None,
        max_output_chars: int | None = None,
    ) -> None:
        self.cli_mode = cli_mode
        self.session_id = session_id
        self.command_timeout_seconds = command_timeout_seconds or int(
            os.getenv("BROWSER_COMMAND_TIMEOUT_SECONDS", "120")
        )
        self.max_output_chars = max_output_chars or int(
            os.getenv("BROWSER_MAX_OUTPUT_CHARS", "15000")
        )
        self._connected = False
        self._cdp_url: str | None = None
        self._project_root = Path.cwd()

    @property
    def allowed_commands(self) -> set[str]:
        if self.cli_mode == "browser-use":
            return BROWSER_USE_COMMANDS
        return PLAYWRIGHT_CLI_COMMANDS

    def connect(self, cdp_url: str) -> dict[str, Any]:
        """Connect the CLI to the remote browser via CDP URL."""
        self._cdp_url = cdp_url

        if self.cli_mode == "browser-use":
            # browser-use: connect with --cdp-url and open about:blank
            result = self._run_subprocess(
                self._browser_use_args(["--cdp-url", cdp_url, "open", "about:blank"])
            )
        else:
            # playwright-cli: pass CDP URL via env var, then open about:blank
            result = self._run_subprocess(
                self._playwright_cli_args(["open", "about:blank"])
            )

        if result["success"]:
            self._connected = True
        return result

    def run_command(self, command: str, args: list[str] | None = None) -> dict[str, Any]:
        """Run a browser command. Raises BrowserExecutorError on invalid commands."""
        normalized = command.strip()
        if normalized not in self.allowed_commands:
            raise BrowserExecutorError(
                f"Command '{normalized}' is not allowed in {self.cli_mode} mode."
            )

        if not self._connected:
            raise BrowserExecutorError("Browser is not connected. Call connect() first.")

        command_args = args or []
        self._validate_args(command_args)

        if self.cli_mode == "browser-use":
            argv = self._browser_use_args([normalized, *command_args])
        else:
            argv = self._playwright_cli_args([normalized, *command_args])

        logger.info("Browser command: %s %s", normalized, self._redact_args(command_args))
        result = self._run_subprocess(argv)
        return result

    def close(self) -> dict[str, Any]:
        """Close the browser CLI session."""
        if not self._connected:
            return {"success": True, "message": "Not connected"}

        if self.cli_mode == "browser-use":
            result = self._run_subprocess(
                self._browser_use_args(["close"]),
                timeout=15,
            )
        else:
            result = self._run_subprocess(
                self._playwright_cli_args(["close"]),
                timeout=15,
            )

        self._connected = False
        return result

    def _browser_use_args(self, args: list[str]) -> list[str]:
        env_path = os.environ.get("PATH", "")
        browser_use_cmd = shutil.which("browser-use", path=env_path) or "browser-use"
        return [browser_use_cmd, "--session", self.session_id, *args]

    def _playwright_cli_args(self, args: list[str]) -> list[str]:
        local_bin_name = "playwright-cli.cmd" if os.name == "nt" else "playwright-cli"
        local_bin = self._project_root / "node_modules" / ".bin" / local_bin_name
        cli_path = str(local_bin) if local_bin.exists() else (shutil.which("playwright-cli") or "playwright-cli")
        return [cli_path, f"-s={self.session_id}", *args]

    def _make_env(self) -> dict[str, str]:
        """Build subprocess env with CDP endpoint set."""
        env = os.environ.copy()
        if self._cdp_url:
            env["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = self._cdp_url
        return env

    def _run_subprocess(self, argv: list[str], timeout: int | None = None) -> dict[str, Any]:
        effective_timeout = timeout or self.command_timeout_seconds
        try:
            completed = subprocess.run(
                argv,
                cwd=self._project_root,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
                env=self._make_env(),
            )
            stdout = self._truncate(self._redact(completed.stdout or ""))
            stderr = self._truncate(self._redact(completed.stderr or ""))
            return {
                "success": completed.returncode == 0,
                "command": " ".join(argv[:3]),
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except subprocess.TimeoutExpired:
            raise BrowserExecutorError(f"Command timed out after {effective_timeout}s: {self._redact(' '.join(argv[:3]))}")
        except FileNotFoundError:
            raise BrowserExecutorError(f"Executable not found: {argv[0]}")

    def _validate_args(self, args: list[str]) -> None:
        for arg in args:
            if not isinstance(arg, str):
                raise BrowserExecutorError("Command arguments must be strings.")
            if "\x00" in arg:
                raise BrowserExecutorError("Command arguments cannot contain NUL bytes.")

    def _redact(self, text: str) -> str:
        for pattern, replacement in TOKEN_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _redact_args(self, args: list[str]) -> list[str]:
        return [self._redact(arg) for arg in args]

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars] + "\n...[truncated]"
