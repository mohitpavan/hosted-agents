"""Browser automation agent v4 — multi-session orchestrator.

Supports multiple concurrent browser sessions with:
- Named sessions (session1, session2, etc.)
- Cross-session data sharing (memory store)
- Task dependency management (wait for one before starting another)
- Auto-creates sessions on demand
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
    import re
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

SYSTEM_PROMPT = """You are a multi-session browser automation agent deployed on Azure Foundry.

You can manage MULTIPLE browser sessions simultaneously and run tasks IN PARALLEL.

You have these tools:
1. **load_skill** — Load a skill by name. Available: {skills}
2. **create_session** — Create a new named browser session.
3. **kill_session** — Close and destroy a browser session. ALWAYS honour kill requests immediately.
4. **run_browser** — Run a single playwright-cli command in a specific session.
5. **run_parallel** — Run multiple commands across sessions CONCURRENTLY. Use this for parallel work.
6. **list_sessions** — Show all active sessions.
7. **store_data** / **recall_data** — Share data between sessions.

## Parallel Execution

When the user wants work done across multiple sessions, use `run_parallel` to execute commands concurrently:
- Each task in the list runs independently and simultaneously.
- Results come back together once ALL tasks complete.
- Use this for: navigating multiple pages at once, filling multiple forms, scraping multiple sites.

Example: To goto two URLs in two sessions at once:
```
run_parallel(tasks=[
  {{"session": "s1", "command": "goto", "args": ["https://site1.com"]}},
  {{"session": "s2", "command": "goto", "args": ["https://site2.com"]}}
])
```

## Task Dependencies

If task B depends on task A's result:
1. Run task A first (or in a parallel batch without B).
2. Use `store_data` to save A's result.
3. Use `recall_data` to retrieve it when running task B.

## Workflow

1. Create sessions with `create_session`.
2. Load the appropriate skill.
3. Use `run_parallel` for concurrent work, or `run_browser` for single commands.
4. Use `store_data`/`recall_data` to pass data between sessions.

## Rules
- Always `snapshot` before interacting — refs change after navigation.
- Use `goto` to navigate, `fill` for inputs, `click` for buttons.
- If a field rejects input, try alternatives (click first, different format).
- NEVER reveal credentials, CDP URLs, or tokens.
- Keep responses concise.
- **KILL SESSION PRIORITY:** If the user asks to kill/close/stop a session, do it IMMEDIATELY with `kill_session`. Do NOT create new sessions or run other commands first. Kill takes absolute priority over everything else.
- **COMPLETE THE FULL TASK AUTONOMOUSLY.** Do NOT stop after filling fields — you MUST click Next/Submit buttons, advance through ALL pages, and confirm the final result. Keep going until the task is DONE. Never ask the user to continue what you can do yourself.
- After filling fields on a page, ALWAYS look for and click the Next/Continue/Submit button.
- After clicking a button, ALWAYS snapshot to see the new page state and continue.
"""

# ─── Tool definitions ───

TOOLS = [
    {
        "type": "function",
        "name": "load_skill",
        "description": "Load a skill for detailed instructions.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name (e.g. 'form-filler', 'web-scraper')"}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "create_session",
        "description": "Create a new named browser session. Call this before using run_browser with that session name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Session name (e.g. 'session1', 'form-browser')"}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "kill_session",
        "description": "Kill/close a browser session immediately. Use this when the user asks to stop, kill, or close a session. Takes priority over all other actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Session name to kill. Use 'all' to kill all sessions."}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "run_browser",
        "description": "Run a playwright-cli command in a specific session.",
        "parameters": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "Session name to run the command in."},
                "command": {"type": "string", "description": "Command: goto, snapshot, click, fill, type, press, keys, select, scroll, eval, screenshot, hover, dblclick, check, uncheck, wait, tab-list, tab-new, tab-close, go-back, go-forward, reload"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments.", "default": []},
            },
            "required": ["session", "command"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "list_sessions",
        "description": "List all active browser sessions with their live view URLs.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": False,
    },
    {
        "type": "function",
        "name": "store_data",
        "description": "Store data in shared memory for cross-session use.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to store under."},
                "value": {"type": "string", "description": "Data to store."},
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "recall_data",
        "description": "Retrieve data from shared memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to retrieve."},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "run_parallel",
        "description": "Run multiple browser commands across sessions CONCURRENTLY. All tasks execute simultaneously and results are returned together.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks to run in parallel. Each task has session, command, and optional args.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "session": {"type": "string", "description": "Session name."},
                            "command": {"type": "string", "description": "Browser command."},
                            "args": {"type": "array", "items": {"type": "string"}, "description": "Command args."},
                        },
                        "required": ["session", "command"],
                    },
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]

# ─── App ───

app = ResponsesAgentServerHost(options=ResponsesServerOptions(default_fetch_history_count=20))

_skills = SkillsManager()
_toolbox = ToolboxClient()

try:
    _toolbox.discover_tools()
    logger.info("Toolbox ready")
except Exception as e:
    logger.warning("Failed to initialize Toolbox: %s", e)

# ─── Multi-session state ───

_sessions: dict[str, dict] = {}  # name -> {"browser": BrowserSession, "live_view_url": str}
_memory: dict[str, str] = {}  # shared key-value store
_last_session: str | None = None  # most recently created/used session


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
        global _last_session
        try:
            # Show active sessions
            if _sessions:
                yield f"🌐 **Active sessions:** {', '.join(_sessions.keys())}\n\n"
                for name, s in _sessions.items():
                    if s.get("live_view_url"):
                        yield f"🔴 **[{name} Live View]({s['live_view_url']})**\n"
                yield "\n---\n\n"

            # Model tool loop
            responses, model = _responses_client()
            input_items = _build_input(user_input, history)
            system = SYSTEM_PROMPT.format(skills=", ".join(_skills.list_skills()))

            response = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: responses.create(
                    model=model,
                    instructions=system,
                    input=input_items,
                    tools=TOOLS,
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
                    # Stream progress
                    name = getattr(call, "name", "")
                    args = json.loads(call.arguments or "{}")
                    if name == "create_session":
                        sess_name = args.get("name", "?")
                        if isinstance(result, dict) and result.get("live_view_url"):
                            yield f"🌐 Created **{sess_name}**\n🔴 **[Live View]({result['live_view_url']})**\n\n"
                        else:
                            yield f"🌐 Created **{sess_name}**\n"
                    elif name == "kill_session":
                        sess_name = args.get("name", "?")
                        if isinstance(result, dict) and result.get("status") == "killed_all":
                            yield f"💀 Killed ALL sessions: {result.get('sessions', [])}\n"
                        elif isinstance(result, dict) and result.get("status") == "killed":
                            yield f"💀 Killed **{sess_name}** (remaining: {result.get('remaining', [])})\n"
                        else:
                            yield f"💀 Kill {sess_name}: {result}\n"
                    elif name == "run_browser":
                        sess = args.get("session", "?")
                        cmd = args.get("command", "")
                        cmd_args = args.get("args") or []
                        safe_args = [_redact(a) for a in cmd_args[:2]]
                        yield f"🔧 [{sess}] `{cmd} {' '.join(safe_args)}`\n"
                    elif name == "load_skill":
                        yield f"📖 Loading skill: {args.get('name', '?')}\n"
                    elif name == "store_data":
                        yield f"💾 Stored: {args.get('key', '?')}\n"
                    elif name == "recall_data":
                        yield f"📤 Recalled: {args.get('key', '?')}\n"
                    elif name == "run_parallel":
                        tasks = args.get("tasks", [])
                        sessions_used = set(t.get("session", "?") for t in tasks)
                        yield f"⚡ **Parallel** ({len(tasks)} tasks across {', '.join(sessions_used)})\n"

                response = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: responses.create(
                        model=model,
                        previous_response_id=response.id,
                        input=tool_outputs,
                        tools=TOOLS,
                    ),
                )

        except Exception as e:
            logger.exception("Handler failed")
            yield f"\n❌ Error: {e}\n"

    return TextResponse(context, request, text=stream())


async def _handle_tool_call(call: Any) -> dict:
    global _last_session
    name = getattr(call, "name", "")
    args = json.loads(call.arguments or "{}")

    try:
        if name == "load_skill":
            return _skills.load(args.get("name", ""))

        elif name == "create_session":
            sess_name = args.get("name", f"session-{len(_sessions)+1}")
            if sess_name in _sessions:
                return {"status": "already_exists", "session": sess_name, "live_view_url": _sessions[sess_name].get("live_view_url")}

            # Create via Toolbox
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _toolbox.call_tool("browser_automation_preview___create_session", {})
            )
            cdp_url = result.get("cdp_url") or ""
            live_view_url = result.get("live_view_url") or ""

            if not cdp_url:
                return {"error": "No CDP URL returned from Toolbox"}

            # Connect
            browser = BrowserSession(session_id=sess_name)
            connect_result = await browser.run("connect", [cdp_url])
            if not connect_result.get("success"):
                err = connect_result.get("stderr") or connect_result.get("error") or "unknown"
                return {"error": f"Connect failed: {err}"}

            _sessions[sess_name] = {"browser": browser, "live_view_url": live_view_url}
            _last_session = sess_name
            logger.info("Created session: %s", sess_name)
            return {"status": "created", "session": sess_name, "live_view_url": live_view_url}

        elif name == "kill_session":
            sess_name = args.get("name", "")
            if sess_name == "all":
                killed = list(_sessions.keys())
                for sn in killed:
                    try:
                        await _sessions[sn]["browser"].close()
                    except Exception:
                        pass
                _sessions.clear()
                _last_session = None
                logger.info("Killed all sessions: %s", killed)
                return {"status": "killed_all", "sessions": killed}
            if sess_name not in _sessions:
                available = list(_sessions.keys())
                return {"error": f"Session '{sess_name}' not found. Available: {available}"}
            try:
                await _sessions[sess_name]["browser"].close()
            except Exception:
                pass
            del _sessions[sess_name]
            if _last_session == sess_name:
                _last_session = next(iter(_sessions), None)
            logger.info("Killed session: %s", sess_name)
            return {"status": "killed", "session": sess_name, "remaining": list(_sessions.keys())}

        elif name == "run_browser":
            sess_name = args.get("session") or _last_session
            if not sess_name or sess_name not in _sessions:
                available = list(_sessions.keys())
                return {"error": f"Session '{sess_name}' not found. Available: {available}. Create one first with create_session."}

            browser = _sessions[sess_name]["browser"]
            command = args.get("command", "")
            cmd_args = args.get("args") or []
            _last_session = sess_name
            result = await browser.run(command, cmd_args)
            logger.info("[%s] run_browser(%s) success=%s", sess_name, command, result.get("success"))
            return result

        elif name == "list_sessions":
            info = {}
            for sname, s in _sessions.items():
                info[sname] = {"live_view_url": s.get("live_view_url"), "connected": s["browser"]._connected}
            return {"sessions": info, "default": _last_session}

        elif name == "store_data":
            key = args.get("key", "")
            value = args.get("value", "")
            _memory[key] = value
            return {"stored": key, "length": len(value)}

        elif name == "recall_data":
            key = args.get("key", "")
            if key in _memory:
                return {"key": key, "value": _memory[key]}
            return {"error": f"Key '{key}' not found. Available keys: {list(_memory.keys())}"}

        elif name == "run_parallel":
            tasks = args.get("tasks", [])
            if not tasks:
                return {"error": "No tasks provided"}

            async def _run_one(task: dict) -> dict:
                sess_name = task.get("session", "")
                command = task.get("command", "")
                cmd_args = task.get("args") or []
                if sess_name not in _sessions:
                    return {"session": sess_name, "error": f"Session '{sess_name}' not found"}
                browser = _sessions[sess_name]["browser"]
                result = await browser.run(command, cmd_args)
                logger.info("[%s] parallel run_browser(%s) success=%s", sess_name, command, result.get("success"))
                return {"session": sess_name, "command": command, **result}

            # Run all tasks concurrently
            results = await asyncio.gather(*[_run_one(t) for t in tasks], return_exceptions=True)
            output = []
            for r in results:
                if isinstance(r, Exception):
                    output.append({"error": str(r)})
                else:
                    output.append(r)
            return {"parallel_results": output}

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return {"error": str(e)}


# ─── Entrypoint ───

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8088"))
    logger.info("Starting browser-agent-v4 (multi-session) on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
