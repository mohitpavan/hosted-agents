"""Toolbox MCP client — connects to Azure Foundry Toolbox endpoint via HTTP JSON-RPC.

Uses DefaultAzureCredential (PMI) for authentication. Calls the MohitMiOpenAPI
tool to fetch test-run metadata when needed for form-filling tasks.

Based on the official foundry-samples bring-your-own-toolbox pattern.
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

# Construct Toolbox URL from project endpoint + toolbox name (official pattern),
# or fall back to explicit TOOLBOX_MCP_ENDPOINT for local testing.
if _TOOLBOX_NAME and _FOUNDRY_ENDPOINT:
    _base = f"{_FOUNDRY_ENDPOINT.rstrip('/')}/toolboxes/{_TOOLBOX_NAME}"
    if _TOOLBOX_VERSION:
        _base += f"/versions/{_TOOLBOX_VERSION}"
    TOOLBOX_ENDPOINT = f"{_base}/mcp?api-version=v1"
else:
    TOOLBOX_ENDPOINT = os.getenv(
        "TOOLBOX_MCP_ENDPOINT",
        "https://cnt-test-gblmitsha-cin-aif.services.ai.azure.com/api/projects/cnt-test-gblmitsha-cin-proj/toolboxes/Mohit-ttoolbox/versions/5/mcp?api-version=v1",
    )
TEST_RUN_ID = "a462f60b-7b09-4a4f-9cb5-1d08a4c4103f"
TOOLBOX_TIMEOUT_SECONDS = int(os.getenv("TOOLBOX_TIMEOUT_SECONDS", "60"))

# Scope to authenticate to the Toolbox MCP endpoint itself
# (Toolbox handles downstream API auth internally)
_TOKEN_SCOPE = "https://ai.azure.com/.default"

# Feature-flag header
_TOOLBOX_FEATURES = os.getenv("FOUNDRY_AGENT_TOOLBOX_FEATURES", "Toolboxes=V1Preview")


class ToolboxClient:
    """Connects to the Foundry Toolbox MCP endpoint via HTTP JSON-RPC with PMI auth."""

    def __init__(self) -> None:
        self._credential = DefaultAzureCredential()
        self._token_provider = get_bearer_token_provider(self._credential, _TOKEN_SCOPE)
        self._session_id: str | None = None
        self._req_id = 0
        self._initialized = False

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
                        "clientInfo": {"name": "async-browser-agent", "version": "1.0.0"},
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

    def get_test_run(self) -> dict[str, Any]:
        """Call MohitMiOpenAPI to get test-run metadata."""
        self._ensure_initialized()

        with httpx.Client(timeout=TOOLBOX_TIMEOUT_SECONDS) as client:
            resp = client.post(
                TOOLBOX_ENDPOINT,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {
                        "name": "MohitMiOpenAPI.GetTestRun",
                        "arguments": {"test-run": TEST_RUN_ID},
                    },
                },
            )
            resp.raise_for_status()
            result = resp.json().get("result", {})
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

    async def shutdown(self) -> None:
        """No persistent connection to close — httpx is per-request."""
        self._initialized = False
        self._session_id = None
