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

You have these tools:
1. **load_skill** — Load a skill by name to get detailed instructions. Available skills: {skills}
2. **run_browser** — Run a playwright-cli command against the active session.
3. **Toolbox tools** — {toolbox_tools} (auto-discovered from Toolbox)

## Workflow (MUST follow this EXACT order — NO EXCEPTIONS)

**BEFORE ANY browser command (goto/snapshot/click/fill/etc), you MUST do steps 1-3:**

1. Call `load_skill` with the relevant skill name.
2. Call `browser_automation_preview___create_session` (Toolbox tool) — returns cdp_url and live_view_url.
3. Call `run_browser` with command="connect", args=["<cdp_url from step 2>"]
4. ONLY NOW can you use: `run_browser` with command="goto", args=["<url>"]
5. Inspect: `run_browser` with command="snapshot" to see page elements.
6. Interact: `run_browser` with command="click"/"fill"/"select", args=["ref", ...]
7. When done, call `browser_automation_preview___end_session` (Toolbox tool).

⚠️ If you skip steps 2-3 and call goto/snapshot/click directly, it WILL FAIL.

## CRITICAL RULES
- You MUST connect before any other browser command. goto/snapshot/click will FAIL without connect.
- Execute ONE command at a time. Wait for each result.
- Always run `snapshot` before interacting — element refs change after navigation.
- Use `goto` to navigate (NOT `open`).
- Use `fill` with [ref, "text"] to type into inputs.
- Use `click` with [ref] to click buttons/links.
- NEVER reveal credentials, CDP URLs, or tokens to the user. They are internal.
- Keep responses concise with concrete results.
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
        "description": "Run a playwright-cli command against the active browser session.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command: connect, goto, snapshot, click, fill, type, press, keys, select, scroll, back, eval, screenshot, hover, dblclick, check, uncheck, wait, tab-list, tab-new, tab-close, go-back, go-forward, reload",
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
_browser = BrowserSession()

# Discover Toolbox tools at startup and build combined tool list
try:
    _toolbox_tools = _toolbox.discover_tools()
    logger.info("Discovered %d Toolbox tools", len(_toolbox_tools))
except Exception as e:
    logger.warning("Failed to discover Toolbox tools at startup: %s", e)
    _toolbox_tools = []

ALL_TOOLS = OWN_TOOLS + _toolbox_tools


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
        try:
            responses, model = _responses_client()
            input_items = _build_input(user_input, history)

            # Build system prompt with available skills and toolbox tool names
            toolbox_names = [t["name"] for t in _toolbox_tools]
            system = SYSTEM_PROMPT.format(
                skills=", ".join(_skills.list_skills()),
                toolbox_tools=", ".join(toolbox_names) if toolbox_names else "(none — Toolbox unavailable)",
            )

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
                    # Stream progress (redact sensitive values)
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
                    elif _toolbox.is_toolbox_tool(name):
                        yield f"🌐 Toolbox: `{name}`\n"
                        # Show live view URL if it came from the tool
                        if isinstance(result, dict):
                            live_url = result.get("liveViewUrl") or result.get("live_view_url") or ""
                            if live_url:
                                yield f"🔴 **[Live View]({live_url})**\n\n"

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
    """Dispatch tool calls — own tools handled locally, Toolbox tools forwarded."""
    name = getattr(call, "name", "")
    args = json.loads(call.arguments or "{}")

    try:
        # Our own tools
        if name == "load_skill":
            return _skills.load(args.get("name", ""))

        elif name == "run_browser":
            command = args.get("command", "")
            cmd_args = args.get("args") or []
            result = await _browser.run(command, cmd_args)
            logger.info("run_browser(%s) result: success=%s", command, result.get("success"))
            return result

        # Toolbox tools — forward directly
        elif _toolbox.is_toolbox_tool(name):
            logger.info("Forwarding to Toolbox: %s(%s)", name, args)
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _toolbox.call_tool(name, args)
            )
            # If this looks like a create_session response, store CDP URL
            cdp_url = result.get("cdp_url") or result.get("cdpUrl") or ""
            if cdp_url:
                _browser.set_cdp_url(cdp_url)
            logger.info("Toolbox result keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
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
