"""BAT Browser Agent — Foundry hosted agent using BATv2Toolbox for browser sessions.

Architecture:
1. Toolbox (BATv2Toolbox) → create_session/end_session (remote Playwright sessions)
2. browser-use CLI → execute browser commands against the CDP session
3. Responses protocol → stream results back to the user
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from azure.ai.agentserver.responses.models import (
    MessageContentInputTextContent,
    MessageContentOutputTextContent,
)
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from browser_executor import BrowserExecutor, BrowserExecutorError
from toolbox_client import ToolboxClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# Verbose streaming: show tool calls in agent output (set to "false" to suppress)
_VERBOSE_STREAM = os.getenv("BROWSER_VERBOSE", "true").lower() in ("true", "1", "yes")


_MI_TOKEN_DEBUG = ""


def _debug_mi_token():
    """Debug: get a token using DefaultAzureCredential and store decoded claims."""
    global _MI_TOKEN_DEBUG
    import base64
    try:
        cred = DefaultAzureCredential()
        # Get token for management.core.windows.net (same audience as PMI connection)
        token = cred.get_token("https://management.core.windows.net/.default")
        # Decode JWT payload (no verification, just decode)
        parts = token.token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            claims = {k: decoded.get(k) for k in ["aud", "iss", "oid", "sub", "appid", "appidacr", "idtyp", "ver", "tid"]}
            _MI_TOKEN_DEBUG = json.dumps(claims, indent=2)
            logger.warning("MI TOKEN CLAIMS: %s", _MI_TOKEN_DEBUG)
    except Exception as e:
        _MI_TOKEN_DEBUG = f"FAILED: {e}"
        logger.warning(f"MI TOKEN DEBUG FAILED: {e}")


# Run token debug on import
_debug_mi_token()

_SYSTEM_PROMPT = """You are a Foundry hosted browser automation agent.

You help users complete browser automation tasks by calling the run_browser_command tool.
A remote Chromium browser session has already been created and connected for you via playwright-cli.

Rules:
- Use `goto` to navigate to a URL. Do NOT use `open` — the browser is already open.
- Execute ONE command at a time. Do NOT call multiple tool commands in parallel. Wait for each command result before issuing the next.
- If the user asks about multiple URLs, handle them sequentially: goto first URL, extract all needed info, then goto second URL, extract info, etc.
- NEVER use the `close` or `open` command — the session is managed externally.
- Use snapshot to discover element refs before interacting (click, fill, etc.).
- Use eval to extract page data (title, meta tags, text content).
- Use screenshot for visual proof when needed.
- Keep command sequences short and purposeful.
- Summarize what happened and report results clearly.
- Do not reveal credentials, remote endpoint values, or access tokens.
"""

_BROWSER_TOOL = {
    "type": "function",
    "name": "run_browser_command",
    "description": "Run a single playwright-cli command against the remote Chromium session.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The playwright-cli command: open, state, click, input, type, keys, select, screenshot, scroll, eval, hover, back, tab, close, etc.",
            },
            "args": {
                "type": "array",
                "description": "Command arguments as separate strings (e.g. element index, text, URL).",
                "items": {"type": "string"},
                "default": [],
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    "strict": False,
}

_TOOLS = [_BROWSER_TOOL]

app = ResponsesAgentServerHost(options=ResponsesServerOptions(default_fetch_history_count=20))


def _env(name: str, *fallbacks: str) -> str:
    for key in (name, *fallbacks):
        value = os.getenv(key)
        if value:
            return value
    raise EnvironmentError(f"Missing required environment variable: {name}")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _responses_client() -> tuple[Any, str]:
    endpoint = _env("FOUNDRY_PROJECT_ENDPOINT")
    model = _env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "BROWSER_AGENT_MODEL")
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    return client.get_openai_client().responses, model


def _build_input(current_input: str, history: list[Any]) -> list[dict[str, str]]:
    input_items: list[dict[str, str]] = []
    for item in history:
        if hasattr(item, "content") and item.content:
            for content in item.content:
                if isinstance(content, MessageContentOutputTextContent) and content.text:
                    input_items.append({"role": "assistant", "content": content.text})
                elif isinstance(content, MessageContentInputTextContent) and content.text:
                    input_items.append({"role": "user", "content": content.text})
    input_items.append({"role": "user", "content": current_input})
    return input_items


def _function_calls(response: Any) -> list[Any]:
    return [item for item in getattr(response, "output", []) if getattr(item, "type", None) == "function_call"]


# ─── Persistent browser session (shared across requests within container lifetime) ───

class _BrowserSession:
    """Holds state for a single browser session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.active = False
        self.cdp_url: str | None = None
        self.executor: BrowserExecutor | None = None
        self.live_view_url: str | None = None
        self.consecutive_failures: int = 0

    def is_alive(self) -> bool:
        """Check if the session is still usable."""
        if not self.active or not self.executor:
            return False
        # Too many consecutive failures means session is dead
        if self.consecutive_failures >= 3:
            logger.info("Session %s has %d consecutive failures, marking dead", self.session_id, self.consecutive_failures)
            return False
        return True

    def reset(self):
        """Mark session as inactive."""
        self.active = False
        self.cdp_url = None
        self.executor = None
        self.live_view_url = None
        self.consecutive_failures = 0


# Active sessions keyed by session_id — supports multiple concurrent sessions
_sessions: dict[str, _BrowserSession] = {}


def _get_or_create_session_id() -> str:
    """Reuse existing session if one is alive, otherwise generate a new ID."""
    for sid, s in _sessions.items():
        if s.active and s.is_alive():
            return sid
    return f"session-{uuid.uuid4().hex[:8]}"


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    user_input = await context.get_input_text() or "Hello!"
    history = await context.get_history()
    max_commands = _int_env("BROWSER_MAX_COMMANDS", 24)

    # Per-request verbose override: /quiet suppresses, /verbose forces
    verbose = _VERBOSE_STREAM
    if "/quiet" in user_input.lower():
        verbose = False
        user_input = user_input.replace("/quiet", "").replace("/Quiet", "").strip()
    elif "/verbose" in user_input.lower():
        verbose = True
        user_input = user_input.replace("/verbose", "").replace("/Verbose", "").strip()

    session_id = _get_or_create_session_id()
    logger.info("Request %s: session_id=%s", context.response_id, session_id)

    toolbox = ToolboxClient()

    async def stream_response():
        try:
            # Phase 1: Reuse existing session or create a new one
            session = _sessions.get(session_id)
            if session and session.active and session.is_alive():
                logger.info("Reusing session %s", session_id)
                yield f"🌐 **Reusing browser session** `{session_id}`\n\n"
                yield f"🔴 **[Live View]({session.live_view_url})**\n\n"
                yield "---\n\n"
                executor = session.executor
            else:
                # Clean up dead session if any
                if session_id in _sessions:
                    del _sessions[session_id]

                session = _BrowserSession(session_id)
                logger.info("Creating browser session %s via Toolbox", session_id)
                yield "⏳ Creating browser session via Toolbox...\n\n"

                session_result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: toolbox.create_session()
                )
                cdp_url = session_result.get("cdp_url") or ""

                if not cdp_url:
                    logger.error("No CDP URL in session result: %s", session_result)
                    yield f"❌ Toolbox create_session returned no CDP URL.\nResult: {json.dumps(session_result)}\n"
                    return

                # Build live view URL
                from urllib.parse import quote
                live_view_cdp = cdp_url + ("&" if "?" in cdp_url else "?") + "isSecondaryConnection=true"
                live_view_url = f"https://pwwdashboard-f4gkeyekh5bucqb3.eastus-01.azurewebsites.net/?cdp={quote(live_view_cdp, safe='')}"

                yield f"🌐 **Browser session created!** `{session_id}`\n\n"
                yield f"🔴 **[Live View]({live_view_url})**\n\n"
                yield "---\n\n⏳ Connecting playwright-cli...\n\n"

                # Connect
                executor = BrowserExecutor(session_id=session_id)
                connect_result = await executor.connect(cdp_url)
                if not connect_result["success"]:
                    err_detail = connect_result.get('stderr') or connect_result.get('stdout') or 'unknown'
                    logger.error("Connect failed: %s", connect_result)
                    yield f"❌ Failed to connect browser CLI: {err_detail}\n"
                    return

                yield "✅ Browser connected. Working on your request...\n\n"

                # Store session for reuse
                session.active = True
                session.cdp_url = cdp_url
                session.executor = executor
                session.live_view_url = live_view_url
                _sessions[session_id] = session

            # Phase 2: Model tool loop
            responses, model = _responses_client()
            input_items = _build_input(user_input, history)

            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: responses.create(
                    model=model,
                    instructions=_SYSTEM_PROMPT,
                    input=input_items,
                    tools=_TOOLS,
                ),
            )

            for _ in range(max_commands):
                if cancellation_signal.is_set():
                    yield "\n⚠️ Request cancelled.\n"
                    return

                calls = _function_calls(response)
                if not calls:
                    yield f"\n---\n\n**Result:**\n{response.output_text}\n"
                    return

                tool_outputs = []
                for call in calls:
                    tool_name = getattr(call, "name", "")

                    if tool_name == "run_browser_command":
                        try:
                            args = json.loads(call.arguments or "{}")
                            command = args.get("command", "")
                            command_args = args.get("args") or []
                            result = await executor.run_command(command, command_args)
                            if verbose:
                                yield f"🔧 `{command} {' '.join(command_args[:3])}`\n"
                            # Track success/failure for session health
                            if result.get("success"):
                                session.consecutive_failures = 0
                            else:
                                session.consecutive_failures += 1
                        except (json.JSONDecodeError, BrowserExecutorError) as error:
                            result = {"success": False, "error": str(error)}
                            session.consecutive_failures += 1

                        tool_outputs.append({
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(result),
                        })
                    else:
                        tool_outputs.append({
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"}),
                        })

                response = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: responses.create(
                        model=model,
                        previous_response_id=response.id,
                        input=tool_outputs,
                        tools=_TOOLS,
                    ),
                )

            yield f"\n⚠️ Reached command limit ({max_commands}).\n"

        except Exception as error:
            logger.exception("Browser automation failed")
            yield f"\n❌ Browser automation failed: {error}\n"
            # Mark session as dead so next request creates a new one
            if session_id in _sessions:
                _sessions[session_id].reset()
                del _sessions[session_id]

    return TextResponse(context, request, text=stream_response())


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8088"))
    logger.info("Starting BAT Browser Agent on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
