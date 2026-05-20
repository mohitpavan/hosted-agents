"""Toolbox MCP client — connects to BATv2Toolbox for browser session management.

Uses DefaultAzureCredential (PMI) for authentication. Calls mohit_pmi_bat tools
(create_session, end_session) to manage remote Playwright browser sessions.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger(__name__)

_FOUNDRY_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
_TOOLBOX_NAME = os.getenv("TOOLBOX_NAME", "")
_TOOLBOX_VERSION = os.getenv("TOOLBOX_VERSION", "")

# Construct Toolbox URL from project endpoint + toolbox name
if _TOOLBOX_NAME and _FOUNDRY_ENDPOINT:
    _base = f"{_FOUNDRY_ENDPOINT.rstrip('/')}/toolboxes/{_TOOLBOX_NAME}"
    if _TOOLBOX_VERSION and _TOOLBOX_VERSION != "0":
        _base += f"/versions/{_TOOLBOX_VERSION}"
    TOOLBOX_ENDPOINT = f"{_base}/mcp?api-version=v1"
else:
    TOOLBOX_ENDPOINT = os.getenv(
        "TOOLBOX_MCP_ENDPOINT",
        "https://Mohit-bat-era-suub-resource.services.ai.azure.com/api/projects/Mohit-bat-era-suub/toolboxes/Mohit/mcp?api-version=v1",
    )

TOOLBOX_TIMEOUT_SECONDS = int(os.getenv("TOOLBOX_TIMEOUT_SECONDS", "90"))

_TOKEN_SCOPE = "https://ai.azure.com/.default"
_TOOLBOX_FEATURES = os.getenv("FOUNDRY_AGENT_TOOLBOX_FEATURES", "Toolboxes=V1Preview")

# Tool name prefix from the toolbox spec
_TOOL_PREFIX = os.getenv("TOOLBOX_TOOL_PREFIX", "mohit_pmi_bat")


class ToolboxClient:
    """Connects to BATv2Toolbox via HTTP JSON-RPC for browser session lifecycle."""

    def __init__(self) -> None:
        self._credential = DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(self._credential, _TOKEN_SCOPE)
        self._session_id: str | None = None
        self._req_id = 0
        self._initialized = False
        self._tools: list[dict] = []

        # Debug: log the identity we're authenticating as
        try:
            import jwt
            token = self._credential.get_token(_TOKEN_SCOPE).token
            claims = jwt.decode(token, options={"verify_signature": False})
            logger.info("TOOLBOX AUTH IDENTITY: oid=%s, appid=%s, sub=%s, upn=%s",
                        claims.get("oid"), claims.get("appid"), claims.get("sub"), claims.get("upn"))
            logger.info("TOOLBOX ENDPOINT: %s", TOOLBOX_ENDPOINT)
        except Exception as e:
            logger.warning("Could not decode token for debug: %s", e)

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token_provider()}",
        }
        if _TOOLBOX_FEATURES:
            h["Foundry-Features"] = _TOOLBOX_FEATURES
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _ensure_initialized(self) -> None:
        """Send MCP initialize + initialized notification (once)."""
        if self._initialized:
            return

        with httpx.Client(timeout=TOOLBOX_TIMEOUT_SECONDS) as client:
            resp = client.post(
                TOOLBOX_ENDPOINT,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "bat-browser-agent", "version": "1.0.0"},
                    },
                },
            )
            resp.raise_for_status()
            self._session_id = resp.headers.get("mcp-session-id")
            data = resp.json()
            server_name = data.get("result", {}).get("serverInfo", {}).get("name", "unknown")
            logger.info("Toolbox initialized: server=%s session=%s", server_name, self._session_id)

            # Send initialized notification
            client.post(
                TOOLBOX_ENDPOINT,
                headers=self._headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

        self._initialized = True

    def list_tools(self) -> list[dict]:
        """Discover available tools in the toolbox."""
        self._ensure_initialized()

        with httpx.Client(timeout=TOOLBOX_TIMEOUT_SECONDS) as client:
            resp = client.post(
                TOOLBOX_ENDPOINT,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/list",
                    "params": {},
                },
            )
            resp.raise_for_status()
            result = resp.json().get("result", {})
            self._tools = result.get("tools", [])
            logger.info("Toolbox tools: %s", [t.get("name") for t in self._tools])
            return self._tools

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Generic tool call via JSON-RPC."""
        self._ensure_initialized()

        with httpx.Client(timeout=TOOLBOX_TIMEOUT_SECONDS) as client:
            resp = client.post(
                TOOLBOX_ENDPOINT,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # Check for JSON-RPC error
            if "error" in data:
                error = data["error"]
                raise RuntimeError(f"Toolbox error: {error.get('message', error)}")

            result = data.get("result", {})
            content = result.get("content", [])
            texts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text" and c.get("text"):
                        texts.append(c["text"])
                    elif c.get("type") == "resource":
                        resource = c.get("resource", {})
                        if resource.get("text"):
                            texts.append(resource["text"])
            text = "\n".join(texts) if texts else json.dumps(result)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def create_session(self) -> dict[str, Any]:
        """Create a remote browser session via Toolbox.
        Returns dict with cdpUrl, liveViewUrl, etc."""
        tool_name = "browser_automation_preview___create_session"
        logger.info("Calling %s", tool_name)
        return self._call_tool(tool_name, {})

    def end_session(self) -> dict[str, Any]:
        """End a remote browser session via Toolbox (if tool exists)."""
        tool_name = "browser_automation_preview___end_session"
        logger.info("Calling %s", tool_name)
        try:
            return self._call_tool(tool_name, {})
        except RuntimeError as e:
            logger.warning("end_session not available: %s", e)
            return {"status": "skipped", "reason": str(e)}

    async def shutdown(self) -> None:
        """Reset state."""
        self._initialized = False
        self._session_id = None
