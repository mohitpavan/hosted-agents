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
from mcp_client import McpBrowserClient
from toolbox_client import ToolboxClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

_SYSTEM_PROMPT = """You are a Foundry hosted browser automation agent.

You help users complete browser automation tasks by calling the run_browser_command tool.
A remote Chromium browser session has already been created and connected for you.

Rules:
- The browser is already open. Use snapshot or state first to see the current page.
- Use open/goto for navigation to URLs.
- Use snapshot to discover element refs before interacting (click, fill, select, etc.).
- Use screenshot for visual proof.
- Keep command sequences short and purposeful.
- Summarize what happened and report results clearly.
- Do not reveal credentials, remote endpoint values, or access tokens.
- If the user asks you to fill a form, call get_test_run_metadata FIRST to get metadata.
  If the result has a displayName field, use it to fill the display name / name field in the form.
  Use any other relevant fields from the metadata to fill other form fields.

Form filling tips:
- After taking a snapshot, identify input fields.
- To fill form fields reliably on ANY web app (Google Forms, MS Forms, React apps), use this eval pattern:
  eval (function(){var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;var t=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;var fields=document.querySelectorAll('input[type="text"],textarea');var vals=['VALUE1','VALUE2','VALUE3'];for(var i=0;i<Math.min(fields.length,vals.length);i++){var f=fields[i];var setter=f.tagName==='TEXTAREA'?t:s;setter.call(f,vals[i]);f.dispatchEvent(new Event('input',{bubbles:true}));f.dispatchEvent(new Event('change',{bubbles:true}));}return 'filled'})()
  Replace VALUE1, VALUE2, VALUE3 with the actual metadata values.
- Do NOT use input.value = x directly. Do NOT use the input command. Always use the eval pattern above.
- After filling, take a screenshot to VERIFY values appear.
- Only click Submit AFTER verifying fields show correct values in the screenshot.
"""

_BROWSER_TOOL_PLAYWRIGHT_CLI = {
    "type": "function",
    "name": "run_browser_command",
    "description": "Run a single browser automation command against the remote Chromium session.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The browser command name: open, goto, snapshot, click, fill, screenshot, select, hover, check, type, press, eval, console, network, tab-list, tab-new, close, etc.",
            },
            "args": {
                "type": "array",
                "description": "Command arguments as separate strings.",
                "items": {"type": "string"},
                "default": [],
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    "strict": False,
}

_BROWSER_TOOL_BROWSER_USE = {
    "type": "function",
    "name": "run_browser_command",
    "description": "Run a single browser-use CLI command against the remote Chromium session.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The browser-use command: open, state, click, input, type, keys, select, screenshot, scroll, eval, hover, back, tab, close, etc.",
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

_TEST_RUN_METADATA_TOOL = {
    "type": "function",
    "name": "get_test_run_metadata",
    "description": "Fetch test-run metadata from the Toolbox API. Use this when you need data to fill forms (e.g. displayName, status, etc.). Returns a JSON object with test-run fields.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": False,
}

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


def _cli_mode() -> str:
    return os.getenv("BROWSER_CLI_MODE", "browser-use").strip().lower()


def _cli_mode_from_input(user_input: str) -> str:
    """Detect CLI mode from user prompt, falling back to env var."""
    lower = user_input.lower()
    if "browser-use" in lower or "browser use" in lower:
        return "browser-use"
    if "playwright" in lower:
        return "playwright-cli"
    return _cli_mode()


def _get_tool_definitions(cli_mode: str = None) -> list[dict]:
    mode = cli_mode or _cli_mode()
    if mode == "browser-use":
        return [_BROWSER_TOOL_BROWSER_USE, _TEST_RUN_METADATA_TOOL]
    return [_BROWSER_TOOL_PLAYWRIGHT_CLI, _TEST_RUN_METADATA_TOOL]


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


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    user_input = await context.get_input_text() or "Hello!"
    history = await context.get_history()
    session_id = f"session-{uuid.uuid4().hex[:12]}"
    cli_mode = _cli_mode_from_input(user_input)
    max_commands = _int_env("BROWSER_MAX_COMMANDS", 24)

    logger.info("Request %s: cli=%s session=%s", context.response_id, cli_mode, session_id)

    mcp_client = McpBrowserClient()
    executor = BrowserExecutor(cli_mode=cli_mode, session_id=session_id)
    toolbox_client = ToolboxClient()

    async def stream_response():
        try:
            # Phase 1: Create browser session via MCP
            logger.info("Creating browser session %s", session_id)
            session_result = await mcp_client.create_browser_session(session_id)
            cdp_url = session_result["cdpUrl"]

            # Phase 2: Stream CDP URL to user immediately
            yield f"🌐 **Browser session created!**\n\n"
            short_cdp = cdp_url[:20] + "..."
            yield f"**[{short_cdp}]({cdp_url})**\n\n"
            yield f"Session ID: `{session_id}` | CLI: `{cli_mode}`\n\n"
            yield "---\n\n⏳ Starting browser automation...\n\n"

            # Phase 3: Connect the CLI to the CDP URL
            logger.info("Connecting %s to CDP session %s", cli_mode, session_id)
            connect_result = executor.connect(cdp_url)
            if not connect_result["success"]:
                err_detail = connect_result.get('stderr') or connect_result.get('stdout') or 'unknown'
                logger.error("Connect failed: %s", connect_result)
                yield f"❌ Failed to connect browser CLI: {err_detail}\n"
                return

            yield "✅ Browser connected. Working on your request...\n\n"

            # Phase 4: Model tool loop
            responses, model = _responses_client()
            input_items = _build_input(user_input, history)
            tools = _get_tool_definitions(cli_mode)

            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: responses.create(
                    model=model,
                    instructions=_SYSTEM_PROMPT,
                    input=input_items,
                    tools=tools,
                ),
            )

            for _ in range(max_commands):
                if cancellation_signal.is_set():
                    yield "\n⚠️ Request cancelled.\n"
                    return

                calls = _function_calls(response)
                if not calls:
                    # Model is done — yield final response
                    yield f"\n---\n\n**Result:**\n{response.output_text}\n"
                    return

                tool_outputs = []
                for call in calls:
                    tool_name = getattr(call, "name", "")

                    if tool_name == "get_test_run_metadata":
                        try:
                            metadata = await asyncio.get_running_loop().run_in_executor(
                                None, toolbox_client.get_test_run
                            )
                            yield f"📋 Fetched test-run metadata (displayName: `{metadata.get('displayName', 'N/A')}`)\n"
                            tool_outputs.append({
                                "type": "function_call_output",
                                "call_id": call.call_id,
                                "output": json.dumps(metadata),
                            })
                        except Exception as toolbox_err:
                            logger.warning("Toolbox call failed: %s", toolbox_err)
                            tool_outputs.append({
                                "type": "function_call_output",
                                "call_id": call.call_id,
                                "output": json.dumps({"success": False, "error": str(toolbox_err)}),
                            })

                    elif tool_name == "run_browser_command":
                        try:
                            args = json.loads(call.arguments or "{}")
                            command = args.get("command", "")
                            command_args = args.get("args") or []
                            result = executor.run_command(command, command_args)
                            yield f"🔧 `{command} {' '.join(command_args[:3])}`\n"
                        except (json.JSONDecodeError, BrowserExecutorError) as error:
                            result = {"success": False, "error": str(error)}

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
                        tools=tools,
                    ),
                )

            yield f"\n⚠️ Reached command limit ({max_commands}).\n"

        except Exception as error:
            logger.exception("Browser automation failed")
            yield f"\n❌ Browser automation failed: {error}\n"

        finally:
            # Phase 5: Cleanup — always runs
            logger.info("Cleaning up session %s", session_id)
            try:
                executor.close()
            except Exception as close_error:
                logger.warning("CLI close failed: %s", close_error)

            try:
                await mcp_client.end_browser_session(session_id)
            except Exception as end_error:
                logger.warning("MCP end_browser_session failed: %s", end_error)

            try:
                await toolbox_client.shutdown()
            except Exception as tb_error:
                logger.warning("Toolbox shutdown failed: %s", tb_error)

            await mcp_client.shutdown()
            logger.info("Session %s cleaned up", session_id)

    return TextResponse(context, request, text=stream_response())


if __name__ == "__main__":
    app.run()
