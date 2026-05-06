"""Toolbox MCP client — connects to Azure Foundry Toolbox endpoint via SSE.

Uses DefaultAzureCredential (PMI) for authentication. Calls the MohitMiOpenAPI
tool to fetch test-run metadata when needed for form-filling tasks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from azure.identity import DefaultAzureCredential
from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

TOOLBOX_ENDPOINT = os.getenv(
    "TOOLBOX_MCP_ENDPOINT",
    "https://kumarmoh-7290-resource.services.ai.azure.com/api/projects/kumarmoh-7290/toolboxes/Mohit-toolbox/mcp?api-version=v1",
)
TEST_RUN_ID = "a462f60b-7b09-4a4f-9cb5-1d08a4c4103f"
TOOLBOX_TIMEOUT_SECONDS = int(os.getenv("TOOLBOX_TIMEOUT_SECONDS", "60"))

# Scope for Azure AI Services
_TOKEN_SCOPE = "https://ai.azure.com/.default"


class ToolboxClient:
    """Connects to the Foundry Toolbox MCP endpoint via SSE with PMI auth."""

    def __init__(self) -> None:
        self._credential = DefaultAzureCredential()
        self._session: ClientSession | None = None
        self._context_manager: Any = None

    def _get_token(self) -> str:
        token = self._credential.get_token(_TOKEN_SCOPE)
        return token.token

    async def _ensure_session(self) -> ClientSession:
        if self._session is not None:
            return self._session

        token = await asyncio.get_running_loop().run_in_executor(
            None, self._get_token
        )
        headers = {"Authorization": f"Bearer {token}"}

        self._context_manager = sse_client(
            url=TOOLBOX_ENDPOINT,
            headers=headers,
        )
        read_stream, write_stream = await self._context_manager.__aenter__()

        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()
        await self._session.initialize()
        logger.info("Toolbox MCP session initialized at %s", TOOLBOX_ENDPOINT)
        return self._session

    async def get_test_run(self) -> dict[str, Any]:
        """Call MohitMiOpenAPI to get test-run metadata."""
        session = await self._ensure_session()
        result = await asyncio.wait_for(
            session.call_tool(
                "MohitMiOpenAPI",
                {"testRunId": TEST_RUN_ID},
            ),
            timeout=TOOLBOX_TIMEOUT_SECONDS,
        )

        text_parts = [
            getattr(item, "text", "") for item in result.content if getattr(item, "text", None)
        ]
        text = "\n".join(text_parts)

        if result.isError:
            raise RuntimeError(f"Toolbox MohitMiOpenAPI failed: {text}")

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Return raw text if not JSON
            return {"raw": text}

    async def shutdown(self) -> None:
        """Close the Toolbox MCP session."""
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
