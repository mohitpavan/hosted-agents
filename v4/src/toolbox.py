"""Toolbox MCP client — auto-discovers and forwards tool calls via Foundry Toolbox.

Connects to the Toolbox MCP endpoint using JSON-RPC over HTTP.
Authentication via DefaultAzureCredential (managed identity in hosted agent).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger(__name__)

_TOKEN_SCOPE = "https://ai.azure.com/.default"
_FEATURES = os.getenv("FOUNDRY_AGENT_TOOLBOX_FEATURES", "Toolboxes=V1Preview")


def _build_endpoint() -> str:
    """Get Toolbox MCP endpoint URL from env."""
    endpoint = os.getenv("TOOLBOX_ENDPOINT", "")
    if not endpoint:
        raise EnvironmentError("TOOLBOX_ENDPOINT is required")
    return endpoint


class ToolboxClient:
    """Auto-discovers Toolbox MCP tools and forwards calls transparently."""

    def __init__(self):
        self._credential = DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(self._credential, _TOKEN_SCOPE)
        self._endpoint = _build_endpoint()
        self._session_id: str | None = None
        self._req_id = 0
        self._initialized = False
        self._tools: list[dict] = []  # Raw MCP tool definitions
        self._timeout = int(os.getenv("TOOLBOX_TIMEOUT_SECONDS", "90"))
        logger.info("Toolbox endpoint: %s", self._endpoint)

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token_provider()}",
        }
        if _FEATURES:
            h["Foundry-Features"] = _FEATURES
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _initialize(self) -> None:
        """MCP initialize handshake (once per lifetime)."""
        if self._initialized:
            return

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._endpoint, headers=self._headers(), json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "browser-agent-v3", "version": "1.0.0"},
                },
            })
            resp.raise_for_status()
            self._session_id = resp.headers.get("mcp-session-id")
            data = resp.json()
            server = data.get("result", {}).get("serverInfo", {}).get("name", "?")
            logger.info("Toolbox initialized: server=%s session=%s", server, self._session_id)

            # Send initialized notification
            client.post(self._endpoint, headers=self._headers(), json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })

        self._initialized = True

    def discover_tools(self) -> list[dict]:
        """Call tools/list and return tools in OpenAI function-calling format."""
        self._initialize()

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._endpoint, headers=self._headers(), json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            })
            resp.raise_for_status()
            result = resp.json().get("result", {})
            self._tools = result.get("tools", [])

        logger.info("Toolbox tools discovered: %s", [t.get("name") for t in self._tools])

        # Convert MCP tool schemas → OpenAI function tool format
        openai_tools = []
        for t in self._tools:
            openai_tools.append({
                "type": "function",
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                "strict": False,
            })
        return openai_tools

    def call_tool(self, name: str, arguments: dict[str, Any] = None) -> dict[str, Any]:
        """Forward a tool call to Toolbox via JSON-RPC."""
        self._initialize()

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._endpoint, headers=self._headers(), json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            })
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                raise RuntimeError(f"Toolbox error: {data['error'].get('message', data['error'])}")

            # Extract text content from result
            content = data.get("result", {}).get("content", [])
            texts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text" and c.get("text"):
                        texts.append(c["text"])
                    elif c.get("type") == "resource" and c.get("resource", {}).get("text"):
                        texts.append(c["resource"]["text"])
            text = "\n".join(texts) if texts else json.dumps(data.get("result", {}))

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def is_toolbox_tool(self, name: str) -> bool:
        """Check if a tool name belongs to Toolbox."""
        return any(t.get("name") == name for t in self._tools)
