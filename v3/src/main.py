"""Browser automation agent v3 — raw agentserver-responses + custom skills + Toolbox + playwright-cli.

Architecture:
- Hosting: azure-ai-agentserver-responses (ResponsesAgentServerHost)
- Browser sessions: Toolbox MCP (JSON-RPC via httpx)
- Browser commands: playwright-cli (subprocess, session-persistent daemon)
- Skills: Custom skills loader (reads markdown files on demand)
- Tools: run_browser, load_skill, create_session, end_session
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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

from skills import SkillsManager
from toolbox import ToolboxClient
from browser import BrowserSession

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def _redact(text: str) -> str:
    """Redact sensitive URLs and tokens from output."""
    import re
    text = re.sub(r"wss://[^\s\"']+", "wss://<redacted>", text)
    text = re.sub(r"\beyJ[a-zA-Z0-9._-]{20,}\b", "<token>", text)
    return text

# ─── Configuration ───

def _env(name: str, *fallbacks: str) -> str:
    for key in (name, *fallbacks):
        val = os.getenv(key)
        if val:
            return val
    raise EnvironmentError(f"Missing env var: {name}")


def _responses_client():
    endpoint = _env("FOUNDRY_PROJECT_ENDPOINT")
    model = _env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "BROWSER_AGENT_MODEL")
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    return client.get_openai_client().responses, model


# ─── System prompt ───

SYSTEM_PROMPT = """You are a browser automation agent deployed on Azure Foundry.

A remote Chromium browser session has already been created and connected for you via playwright-cli.

You have these tools:
1. **load_skill** — Load a skill by name to get detailed instructions. Available skills: {skills}
2. **run_browser** — Run a playwright-cli command against the active session.

## Workflow

1. Call `load_skill` with the relevant skill name to get detailed instructions.
2. Use `goto` to navigate to a URL.
3. Use `snapshot` to see page elements (with refs).
4. Use `click`, `fill`, `select`, etc. to interact with elements by ref.

## CRITICAL RULES
- The browser is ALREADY connected. Just use `goto` directly — do NOT call `connect` or `create_session`.
- Execute ONE command at a time. Wait for each result.
- Always run `snapshot` before interacting — element refs change after navigation.
- Use `goto` to navigate (NOT `open`).
- Use `fill` with [ref, "text"] to type into inputs.
- Use `click` with [ref] to click buttons/links.
- **If a field rejects your input (e.g. date picker), try alternative approaches**: click it first, try different formats, use the calendar UI. Do NOT give up after one attempt.
- NEVER reveal credentials, CDP URLs, or tokens to the user. They are internal.
- Keep responses concise with concrete results.
- **COMPLETE THE FULL TASK AUTONOMOUSLY.** Do NOT stop after filling fields — you MUST click Next/Submit buttons, advance through ALL pages, and confirm the final result. Keep going until the task is DONE.
- After filling fields on a page, ALWAYS look for and click the Next/Continue/Submit button.
- After clicking a button, ALWAYS snapshot to see the new page and continue working.
"""

# ─── Tool definitions (our own tools — Toolbox tools are auto-discovered) ───

OWN_TOOLS = [
    {
        "type": "function",
        "name": "load_skill",
        "description": "Load a skill to get detailed instructions for a specific task type.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to load (e.g. 'form-filler', 'web-scraper')",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "run_browser",
        "description": "Run a playwright-cli command against the already-connected browser session.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command: goto, snapshot, click, fill, type, press, keys, select, scroll, eval, screenshot, hover, dblclick, check, uncheck, wait, tab-list, tab-new, tab-close, go-back, go-forward, reload",
                },
                "args": {
                    "type": "array",
                    "description": "Command arguments (e.g. URL for goto, element ref for click, [ref, text] for fill).",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]

# ─── App ───

app = ResponsesAgentServerHost(options=ResponsesServerOptions(default_fetch_history_count=20))

# Global state
_skills = SkillsManager()
_toolbox = ToolboxClient()

# Discover Toolbox tools at startup (for internal use, not exposed to model)
try:
    _toolbox.discover_tools()
    logger.info("Toolbox ready")
except Exception as e:
    logger.warning("Failed to initialize Toolbox at startup: %s", e)

# Only expose our own tools to the model (no Toolbox tools — we manage sessions ourselves)
ALL_TOOLS = OWN_TOOLS

# Global browser session — created once per container, recreated if dead
_browser: BrowserSession | None = None
_live_view_url: str | None = None


def _build_input(current_input: str, history: list[Any]) -> list[dict]:
    items: list[dict] = []
    for item in history:
        if hasattr(item, "content") and item.content:
            for content in item.content:
                if isinstance(content, MessageContentOutputTextContent) and content.text:
                    items.append({"role": "assistant", "content": content.text})
                elif isinstance(content, MessageContentInputTextContent) and content.text:
                    items.append({"role": "user", "content": content.text})
    items.append({"role": "user", "content": current_input})
    return items


def _function_calls(response: Any) -> list[Any]:
    return [item for item in getattr(response, "output", []) if getattr(item, "type", None) == "function_call"]


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    user_input = await context.get_input_text() or "Hello!"
    history = await context.get_history()

    async def stream():
        global _browser, _live_view_url
        try:
            # Phase 1: Ensure browser is connected (create if needed)
            if not _browser or not _browser._connected:
                yield "⏳ Creating browser session...\n\n"

                session_result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _toolbox.call_tool("browser_automation_preview___create_session", {})
                )
                cdp_url = session_result.get("cdp_url") or ""
                _live_view_url = session_result.get("live_view_url") or ""

                if not cdp_url:
                    logger.error("No CDP URL: %s", session_result)
                    yield "❌ No CDP URL from Toolbox.\n"
                    return

                if _live_view_url:
                    yield f"🔴 **[Live View]({_live_view_url})**\n\n"

                _browser = BrowserSession(session_id=f"s-{os.urandom(4).hex()}")
                connect_result = await _browser.run("connect", [cdp_url])
                if not connect_result.get("success"):
                    err = connect_result.get("stderr") or connect_result.get("error") or "unknown"
                    logger.error("Connect failed: %s", connect_result)
                    _browser = None
                    yield f"❌ Connect failed: {err}\n"
                    return

                yield "✅ Browser connected!\n\n---\n\n"
            else:
                yield f"🌐 **Reusing browser session**\n\n"
                if _live_view_url:
                    yield f"🔴 **[Live View]({_live_view_url})**\n\n"
                yield "---\n\n"

            # Phase 2: Model tool loop
            responses, model = _responses_client()
            input_items = _build_input(user_input, history)
            system = SYSTEM_PROMPT.format(skills=", ".join(_skills.list_skills()))

            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: responses.create(
                    model=model,
                    instructions=system,
                    input=input_items,
                    tools=ALL_TOOLS,
                ),
            )

            while True:
                if cancellation_signal.is_set():
                    yield "\n⚠️ Cancelled.\n"
                    return

                calls = _function_calls(response)
                if not calls:
                    yield f"{response.output_text}\n"
                    return

                tool_outputs = []
                for call in calls:
                    result = await _handle_tool_call(call)
                    tool_outputs.append({
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result),
                    })
                    name = getattr(call, "name", "")
                    if name == "run_browser":
                        args = json.loads(call.arguments or "{}")
                        cmd = args.get("command", "")
                        cmd_args = args.get("args") or []
                        safe_args = [_redact(a) for a in cmd_args[:2]]
                        yield f"🔧 `{cmd} {' '.join(safe_args)}`\n"
                    elif name == "load_skill":
                        args = json.loads(call.arguments or "{}")
                        yield f"📖 Loading skill: {args.get('name', '?')}\n"

                response = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: responses.create(
                        model=model,
                        previous_response_id=response.id,
                        input=tool_outputs,
                        tools=ALL_TOOLS,
                    ),
                )

        except Exception as e:
            logger.exception("Handler failed")
            yield f"\n❌ Error: {e}\n"

    return TextResponse(context, request, text=stream())


async def _handle_tool_call(call: Any) -> dict:
    """Dispatch tool calls — browser commands use the pre-connected session."""
    global _browser
    name = getattr(call, "name", "")
    args = json.loads(call.arguments or "{}")

    try:
        if name == "load_skill":
            return _skills.load(args.get("name", ""))

        elif name == "run_browser":
            if not _browser:
                return {"error": "Browser not connected."}
            command = args.get("command", "")
            cmd_args = args.get("args") or []
            result = await _browser.run(command, cmd_args)
            # If command failed, mark browser dead so next request reconnects
            if not result.get("success") and command == "goto":
                _browser = None
            return result

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return {"error": str(e)}


# ─── Entrypoint ───

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8088"))
    logger.info("Starting browser-agent-v3 on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
