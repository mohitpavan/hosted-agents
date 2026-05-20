# Async Browser Automation Hosted Agent

A Foundry-hosted browser automation agent that **streams the CDP URL immediately** so you can watch the remote browser live, then executes automation commands asynchronously.

## How It Works

```
1. You send a request: "Navigate to example.com and take a screenshot"
2. Agent creates a remote Chromium session (Azure Playwright Service)
3. Agent streams the CDP URL back to you instantly
4. You connect to the CDP URL (Chrome DevTools, etc.) and watch live
5. Agent executes browser commands via playwright-cli or browser-use
6. Agent returns the final result
7. Agent cleans up the session
```

## Architecture

```
User → Foundry Gateway → Container (port 8088, Responses, streaming)
                              ↓
                    Host: create session (MCP) → stream CDP URL
                              ↓
                    Model loop: run_browser_command tool
                              ↓
                    playwright-cli / browser-use → Remote Chromium (CDP)
                              ↓
                    Host: cleanup (close CLI → end MCP session)
```

Key design: **Host owns the session lifecycle** (not the model). This guarantees:
- CDP URL is always streamed first
- Cleanup always happens (in `finally`)
- Model only worries about running browser commands

## CLI Modes

| Mode | CLI Tool | Set via |
|------|----------|---------|
| `playwright-cli` (default) | `@playwright/cli` | `BROWSER_CLI_MODE=playwright-cli` |
| `browser-use` | `browser-use` | `BROWSER_CLI_MODE=browser-use` |

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `FOUNDRY_PROJECT_ENDPOINT` | Yes (injected) | — | Foundry project endpoint |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Yes | — | Model deployment name |
| `AZURE_PLAYWRIGHT_SERVICE_URL` | Yes | — | WSS endpoint for Playwright Service |
| `AZURE_PLAYWRIGHT_SERVICE_ACCESS_TOKEN` | Yes | — | Access token |
| `BROWSER_CLI_MODE` | No | `playwright-cli` | Which CLI to use |
| `BROWSER_COMMAND_TIMEOUT_SECONDS` | No | `120` | Per-command timeout |
| `BROWSER_MAX_COMMANDS` | No | `24` | Max commands per request |
| `BROWSER_MAX_OUTPUT_CHARS` | No | `15000` | Max output chars returned to model |
| `MCP_TIMEOUT_SECONDS` | No | `90` | MCP tool call timeout |

## Local Setup

```bash
# Python
python -m venv .venv
source .venv/bin/activate  # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt

# Node.js (for playwright-cli and MCP server)
npm install
cd azure-playwright-service-mcp && npm install && cd ..

# Set environment variables
cp .env.example .env
# Edit .env with your values

# Run
python main.py
```

## Local Testing

```bash
curl -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "Open https://example.com and take a screenshot", "stream": false}'
```

## Deployment

```bash
# Install azd agent extension
azd ext install azure.ai.agents

# Set environment values
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME gpt-4.1
azd env set AZURE_PLAYWRIGHT_SERVICE_URL "wss://..."
azd env set AZURE_PLAYWRIGHT_SERVICE_ACCESS_TOKEN "<token>"
azd env set BROWSER_CLI_MODE playwright-cli

# Provision and deploy
azd provision
azd deploy
```

## Security Notes

- The CDP URL grants **full browser control** — the requester is the controller.
- Secrets (access tokens, CDP URLs) are redacted from all logged/returned output.
- Subprocess commands run with timeouts and output truncation.
- `.playwright-remote/` config files (containing tokens) are cleaned up after each request.

## Files

| File | Purpose |
|------|---------|
| `main.py` | Responses handler: creates session, streams CDP URL, model loop, cleanup |
| `browser_executor.py` | Dual CLI executor (playwright-cli + browser-use) |
| `mcp_client.py` | Async MCP client for browser session lifecycle |
| `azure-playwright-service-mcp/` | Node.js MCP server (create/end sessions) |
| `agent.yaml` | Foundry agent schema |
| `azure.yaml` | azd deployment config |
| `Dockerfile` | Container image (Python + Node.js) |
