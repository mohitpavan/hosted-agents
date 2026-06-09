// Copyright (c) Microsoft. All rights reserved.

using System.ComponentModel;
using System.Text.Json;
using Azure.AI.Projects;
using Azure.Identity;
using DotNetEnv;
using Microsoft.Agents.AI;
using Microsoft.Agents.AI.Foundry.Hosting;
using Microsoft.Extensions.AI;
using BrowserAutomation;

Env.TraversePath().Load();

var projectEndpoint = new Uri(Environment.GetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT")
    ?? throw new InvalidOperationException("FOUNDRY_PROJECT_ENDPOINT environment variable is not set."));
var deployment = Environment.GetEnvironmentVariable("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    ?? throw new InvalidOperationException("AZURE_AI_MODEL_DEPLOYMENT_NAME environment variable is not set.");

// Build the Toolbox endpoint from project endpoint
var toolboxName = Environment.GetEnvironmentVariable("TOOLBOX_NAME") ?? "Mohit-toolbox";
var toolboxEndpoint = $"{projectEndpoint.ToString().TrimEnd('/')}/toolboxes/{toolboxName}/mcp?api-version=v1";

// Initialize services
var credential = new DefaultAzureCredential();
var toolbox = new ToolboxClient(toolboxEndpoint, credential);

// Single browser session for this agent instance
BrowserSession? currentSession = null;
string? liveViewUrl = null;

var systemPrompt = """
    You are a browser automation agent deployed on Azure AI Foundry.

    You control a single browser session to help users with web tasks like filling forms,
    navigating pages, scraping content, and interacting with web apps.

    ## Workflow
    1. Call `create_session` to start a browser — share the Live View link with the user.
    2. Use `browser` tool to navigate and interact with pages.
    3. Call `close_session` when done.

    ## Live View (IMPORTANT)
    When you create a session, you get a live_view_url. Show it to the user as:
    🟢 **Session ready** → [Live View](<live_view_url>)
    Show this ONCE when the session is created. Do NOT repeat it on every response.

    ## WHEN TO STOP
    If you are BLOCKED waiting for user input (OTP, confirmation, choice):
    - STOP calling tools immediately
    - Tell the user what you need
    - End your response so they can reply

    ## Rules
    - Always `snapshot` before interacting — element refs change after navigation.
    - Use `goto` to navigate, `fill` for inputs, `click` for buttons.
    - NEVER reveal CDP URLs or tokens to the user.
    - Complete tasks autonomously — click Submit/Next, don't stop halfway.
    - After clicking a button, always `snapshot` to see the new state.

    ## Browser Commands
    Navigation: goto, go-back, go-forward, reload
    Observe: snapshot, screenshot, state
    Interact: click, dblclick, hover, fill, type, press, keys, select, check, uncheck, scroll
    Tabs: tab-list, tab-new, tab-close, tab-select
    Other: eval, wait
    """;

// Create agent with simple browser tools
AIAgent agent = new AIProjectClient(projectEndpoint, credential)
    .AsAIAgent(
        model: deployment,
        instructions: systemPrompt,
        name: "browser-automation",
        description: "A browser automation agent using Playwright via Foundry Toolbox",
        tools:
        [
            AIFunctionFactory.Create(async () =>
                {
                    if (currentSession != null)
                        return $"Session already active. Live View: {liveViewUrl}";

                    var result = await toolbox.CallToolAsync("browser_automation_preview___create_session", null);
                    var cdpUrl = result.TryGetProperty("cdp_url", out var cdp) ? cdp.GetString() : null;
                    liveViewUrl = result.TryGetProperty("live_view_url", out var lv) ? lv.GetString() : null;

                    if (string.IsNullOrEmpty(cdpUrl))
                        return "Error: No CDP URL returned from Toolbox.";

                    currentSession = new BrowserSession("main");
                    currentSession.LiveViewUrl = liveViewUrl;
                    var connectResult = await currentSession.ConnectAsync(cdpUrl);
                    if (!connectResult.Success)
                    {
                        currentSession = null;
                        return $"Error: Browser connect failed — {connectResult.Error ?? connectResult.Stderr}";
                    }

                    return $"Session created successfully.\nlive_view_url: {liveViewUrl}\n\nShow the user: 🟢 **Session ready** → [Live View]({liveViewUrl})";
                },
                "create_session",
                "Create a browser session. Returns a live_view_url to share with the user."),

            AIFunctionFactory.Create(
                ([Description("playwright-cli command (goto, snapshot, click, fill, etc.)")] string command,
                 [Description("Command arguments (URL for goto, selector for click, etc.)")] string[]? args) =>
                {
                    if (currentSession == null)
                        return Task.FromResult("No active session. Call create_session first.");
                    return currentSession.RunAsync(command, args).ContinueWith(t =>
                        JsonSerializer.Serialize(t.Result));
                },
                "browser",
                "Run a browser command. Use snapshot to see page state, goto to navigate, fill/click to interact."),

            AIFunctionFactory.Create(async () =>
                {
                    if (currentSession == null)
                        return "No active session.";
                    await currentSession.CloseAsync();
                    currentSession = null;
                    liveViewUrl = null;
                    return "Session closed.";
                },
                "close_session",
                "Close the current browser session."),
        ]);

var builder = AgentHost.CreateBuilder(args);
builder.Services.AddFoundryResponses(agent);
builder.RegisterProtocol("responses", endpoints => endpoints.MapFoundryResponses());

var app = builder.Build();
app.Run();
