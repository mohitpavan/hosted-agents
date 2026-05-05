from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

DEFAULT_MCP_SERVER_PATH = Path(__file__).parent / "azure-playwright-service-mcp" / "src" / "index.js"
DEFAULT_TIMEOUT_SECONDS = 90


class McpBrowserClient:
    """Async MCP client that spawns the azure-playwright-service-mcp Node.js server
    and communicates with it via stdio to create/end browser sessions."""

    def __init__(
        self,
        mcp_server_path: Path | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._server_path = mcp_server_path or Path(
            os.getenv("MCP_SERVER_PATH", str(DEFAULT_MCP_SERVER_PATH))
        ).resolve()
        self._timeout_seconds = timeout_seconds or int(
            os.getenv("MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        self._session: ClientSession | None = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._context_manager: Any = None

    def _make_server_params(self) -> StdioServerParameters:
        env = os.environ.copy()
        # Ensure the MCP server can find Playwright Service credentials
        service_url = os.getenv("AZURE_PLAYWRIGHT_SERVICE_URL") or os.getenv("PLAYWRIGHT_SERVICE_URL", "")
        access_token = os.getenv("AZURE_PLAYWRIGHT_SERVICE_ACCESS_TOKEN") or os.getenv("PLAYWRIGHT_SERVICE_ACCESS_TOKEN", "")
        env["AZURE_PLAYWRIGHT_SERVICE_URL"] = service_url
        env["AZURE_PLAYWRIGHT_SERVICE_ACCESS_TOKEN"] = access_token

        return StdioServerParameters(
            command="node",
            args=[str(self._server_path)],
            env=env,
            cwd=str(self._server_path.parent),
        )

    async def _ensure_session(self) -> ClientSession:
        """Spawn MCP server and initialize session (lazy, once per request)."""
        if self._session is not None:
            return self._session

        server_params = self._make_server_params()
        self._context_manager = stdio_client(server_params)
        self._read_stream, self._write_stream = await self._context_manager.__aenter__()

        self._session = ClientSession(
            self._read_stream,
            self._write_stream,
        )
        await self._session.__aenter__()
        await self._session.initialize()
        logger.info("MCP session initialized with server at %s", self._server_path)
        return self._session

    async def create_browser_session(self, session_id: str) -> dict[str, Any]:
        """Create a remote browser session and return {sessionId, cdpUrl}."""
        session = await self._ensure_session()
        result = await asyncio.wait_for(
            session.call_tool("create_browser_session", {"sessionId": session_id}),
            timeout=self._timeout_seconds,
        )

        text_parts = [getattr(item, "text", "") for item in result.content if getattr(item, "text", None)]
        text = "\n".join(text_parts)

        if result.isError:
            raise RuntimeError(f"MCP create_browser_session failed: {text}")

        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"MCP returned invalid JSON: {text}") from error

    async def end_browser_session(self, session_id: str) -> dict[str, Any]:
        """End a remote browser session."""
        session = await self._ensure_session()
        result = await asyncio.wait_for(
            session.call_tool("end_browser_session", {"sessionId": session_id}),
            timeout=self._timeout_seconds,
        )

        text_parts = [getattr(item, "text", "") for item in result.content if getattr(item, "text", None)]
        text = "\n".join(text_parts)

        if result.isError:
            raise RuntimeError(f"MCP end_browser_session failed: {text}")

        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    async def shutdown(self) -> None:
        """Close the MCP session and stdio transport."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None

        if self._context_manager is not None:
            try:
                await self._context_manager.__aexit__(None, None, None)
            except Exception:
                pass
            self._context_manager = None
