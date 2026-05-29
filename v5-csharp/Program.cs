// Copyright (c) Microsoft. All rights reserved.

using System.ComponentModel;
using System.Text.Json;
using Azure.AI.AgentServer.Core;
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
var sessionManager = new SessionManager(toolbox);

// Load available skill names
var skillsDir = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "skills");
if (!Directory.Exists(skillsDir))
    skillsDir = Path.Combine(Directory.GetCurrentDirectory(), "skills");
var availableSkills = Directory.Exists(skillsDir)
    ? Directory.GetFiles(skillsDir, "*.md").Select(f => Path.GetFileNameWithoutExtension(f)).ToList()
    : new List<string>();

var systemPrompt = $$"""
    You are a multi-session browser automation agent deployed on Azure Foundry.

    You can manage MULTIPLE browser sessions simultaneously and run tasks IN PARALLEL.

    ## Available Skills: {{string.Join(", ", availableSkills)}}

    ## Session Announcements (MANDATORY)

    Before starting any task, check existing sessions with `list_sessions`.
    - If creating a NEW session for a task: 🟢 **Created session: `<name>`** → [Live View](<live_view_url>)
    - If picking up an EXISTING session from a previous conversation turn: 🔄 **Re-using session: `<name>`** → [Live View](<live_view_url>)

    Do NOT say "Re-using" if you just created the session in THIS turn. Only say it when you come back to a session from a prior turn.
    Format the live_view_url as a markdown link — NEVER paste the raw URL.

    ## WHEN TO STOP (CRITICAL)

    If you are BLOCKED and need user input (e.g. OTP, confirmation, password, choice):
    - STOP calling tools immediately
    - Tell the user exactly what you need and which session is waiting
    - End your response so the user can reply
    - Do NOT keep looping or retrying — just hand control back to the user

    ## Parallel Execution

    When the user wants work done across multiple sessions, use `run_parallel` to execute commands concurrently:
    - Each task in the list runs independently and simultaneously.
    - Results come back together once ALL tasks complete.
    - Use this for: navigating multiple pages at once, filling multiple forms, scraping multiple sites.

    Example: To goto two URLs in two sessions at once:
    ```
    run_parallel(tasksJson='[{"session":"s1","command":"goto","args":["https://site1.com"]},{"session":"s2","command":"goto","args":["https://site2.com"]}]')
    ```

    ## Multi-Form / Multi-Task Workflow

    When user gives you multiple forms or tasks:
    1. Create a session per task: create_session("form1"), create_session("form2"), create_session("form3")
    2. Announce each session with its markdown live view link
    3. Load the skill: load_skill("form-filler")
    4. Use run_parallel to navigate all sessions to their URLs simultaneously
    5. Then work through each form — use run_parallel for concurrent steps where possible

    ## Rules
    - Always `snapshot` before interacting — element refs change after navigation.
    - Use `goto` to navigate, `fill` for inputs, `click` for buttons.
    - If a field rejects input, try alternatives (click first, different format).
    - NEVER reveal credentials, CDP URLs, or tokens.
    - **KILL SESSION PRIORITY:** If the user asks to kill/close/stop a session, do it IMMEDIATELY. Do NOT create new sessions first.
    - **COMPLETE THE FULL TASK AUTONOMOUSLY.** Do NOT stop after filling fields — click Next/Submit, advance through ALL pages, confirm the final result. Never ask the user to continue what you can do yourself.
    - After filling fields on a page, ALWAYS look for and click the Next/Continue/Submit button.
    - After clicking a button, ALWAYS snapshot to see the new page state and continue.

    ## Commands for run_browser
    Navigation: goto, go-back, go-forward, reload
    Observe: snapshot, screenshot, state
    Interact: click, dblclick, hover, fill, type, press, keys, select, check, uncheck, scroll
    Tabs: tab-list, tab-new, tab-close, tab-select
    Other: eval, wait
    """;

// Create agent with browser automation tools
AIAgent agent = new AIProjectClient(projectEndpoint, credential)
    .AsAIAgent(
        model: deployment,
        instructions: systemPrompt,
        name: "browser-automation",
        description: "A browser automation agent using Playwright via Foundry Toolbox",
        tools:
        [
            AIFunctionFactory.Create(
                ([Description("Session name (e.g. 'research', 'login')")] string? name) =>
                    sessionManager.CreateSessionAsync(name ?? $"session-{sessionManager.SessionCount + 1}"),
                "create_session",
                "Create a new named browser session. Returns session name and live_view_url. Share the live_view_url with the user!"),

            AIFunctionFactory.Create(
                ([Description("Session name to kill, or 'all' to kill all")] string name) =>
                    sessionManager.KillSessionAsync(name),
                "kill_session",
                "Kill/close a browser session by name. Use 'all' to kill all sessions."),

            AIFunctionFactory.Create(
                ([Description("playwright-cli command: goto, snapshot, click, fill, etc.")] string command,
                 [Description("Command arguments (e.g. URL for goto, selector for click)")] string[]? args,
                 [Description("Session name (uses last active if omitted)")] string? session) =>
                    sessionManager.RunBrowserAsync(command, args, session),
                "run_browser",
                "Run a playwright-cli command in a browser session. Auto-creates a session if none exist."),

            AIFunctionFactory.Create(
                () => sessionManager.ListSessions(),
                "list_sessions",
                "List all active browser sessions with their status and live_view URLs."),

            AIFunctionFactory.Create(
                ([Description("JSON array of tasks: [{\"session\":\"s1\",\"command\":\"snapshot\"}, {\"session\":\"s2\",\"command\":\"goto\",\"args\":[\"https://...\"]}]")] string tasksJson) =>
                    sessionManager.RunParallelAsync(tasksJson),
                "run_parallel",
                "Run multiple browser commands across different sessions concurrently. Each task needs session, command, and optional args."),

            AIFunctionFactory.Create(
                ([Description("Skill name to load (e.g. 'form-filler', 'web-scraper')")] string name) =>
                {
                    var path = Path.Combine(skillsDir, $"{name}.md");
                    if (!File.Exists(path))
                        return $"Skill '{name}' not found. Available: {string.Join(", ", availableSkills)}";
                    return File.ReadAllText(path);
                },
                "load_skill",
                "Load a skill by name. Skills provide detailed workflows for complex tasks."),
        ]);

var builder = AgentHost.CreateBuilder(args);
builder.Services.AddFoundryResponses(agent);
builder.RegisterProtocol("responses", endpoints => endpoints.MapFoundryResponses());

var app = builder.Build();
app.Run();
