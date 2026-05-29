// Copyright (c) Microsoft. All rights reserved.

using System.Text.Json;

namespace BrowserAutomation;

/// <summary>
/// Manages multiple browser sessions — handles creation, destruction, and routing.
/// </summary>
public class SessionManager
{
    private readonly ToolboxClient _toolbox;
    private readonly Dictionary<string, BrowserSession> _sessions = new();
    private string? _lastSession;

    public int SessionCount => _sessions.Count;

    public SessionManager(ToolboxClient toolbox)
    {
        _toolbox = toolbox;
    }

    /// <summary>
    /// Create a new browser session via Toolbox and connect playwright-cli.
    /// </summary>
    public async Task<string> CreateSessionAsync(string name)
    {
        if (_sessions.ContainsKey(name))
            return JsonSerializer.Serialize(new { status = "already_exists", session = name, live_view_url = _sessions[name].LiveViewUrl });

        // Call Toolbox to create a browser session
        var result = await _toolbox.CallToolAsync("browser_automation_preview___create_session", null);

        var cdpUrl = result.TryGetProperty("cdp_url", out var cdp) ? cdp.GetString() : null;
        var liveViewUrl = result.TryGetProperty("live_view_url", out var lv) ? lv.GetString() : null;

        if (string.IsNullOrEmpty(cdpUrl))
            return JsonSerializer.Serialize(new { error = "No CDP URL returned from Toolbox" });

        var browser = new BrowserSession(name);
        browser.LiveViewUrl = liveViewUrl;

        var connectResult = await browser.ConnectAsync(cdpUrl);
        if (!connectResult.Success)
            return JsonSerializer.Serialize(new { error = $"Browser connect failed: {connectResult.Error ?? connectResult.Stderr}" });

        _sessions[name] = browser;
        _lastSession = name;

        Console.WriteLine($"[Session] '{name}' created (live_view: {!string.IsNullOrEmpty(liveViewUrl)})");
        var liveViewMarkdown = !string.IsNullOrEmpty(liveViewUrl) ? $"[Live View]({liveViewUrl})" : "no live view";
        return $"SESSION CREATED. Copy this EXACTLY to the user:\n🟢 **Created session: `{name}`** → {liveViewMarkdown}\n\nSession is ready for commands.";
    }

    /// <summary>
    /// Kill a session by name, or "all" to kill everything.
    /// </summary>
    public async Task<string> KillSessionAsync(string name)
    {
        if (name == "all")
        {
            var killed = _sessions.Keys.ToList();
            foreach (var s in _sessions.Values)
            {
                try { await s.CloseAsync(); } catch { }
            }
            _sessions.Clear();
            _lastSession = null;
            return JsonSerializer.Serialize(new { status = "killed_all", sessions = killed });
        }

        if (!_sessions.TryGetValue(name, out var session))
            return JsonSerializer.Serialize(new { error = $"Session '{name}' not found. Available: [{string.Join(", ", _sessions.Keys)}]" });

        try { await session.CloseAsync(); } catch { }
        _sessions.Remove(name);

        if (_lastSession == name)
            _lastSession = _sessions.Keys.FirstOrDefault();

        return JsonSerializer.Serialize(new { status = "killed", session = name, remaining = _sessions.Keys.ToList() });
    }

    /// <summary>
    /// Run a playwright-cli command in a session.
    /// </summary>
    public async Task<string> RunBrowserAsync(string command, string[]? args, string? session)
    {
        var sessName = session ?? _lastSession;

        // Lazy session creation
        if (_sessions.Count == 0)
        {
            var createResult = await CreateSessionAsync("default");
            if (createResult.Contains("error"))
                return createResult;
            sessName = "default";
        }
        else if (string.IsNullOrEmpty(sessName) || !_sessions.ContainsKey(sessName))
        {
            return JsonSerializer.Serialize(new { error = $"Session '{sessName}' not found. Available: [{string.Join(", ", _sessions.Keys)}]" });
        }

        var browser = _sessions[sessName!];
        _lastSession = sessName;

        var result = await browser.RunAsync(command, args);
        return JsonSerializer.Serialize(result);
    }

    /// <summary>
    /// Run multiple browser commands across sessions concurrently.
    /// </summary>
    public async Task<string> RunParallelAsync(string tasksJson)
    {
        List<ParallelTask>? tasks;
        try
        {
            tasks = JsonSerializer.Deserialize<List<ParallelTask>>(tasksJson);
        }
        catch (JsonException ex)
        {
            return JsonSerializer.Serialize(new { error = $"Invalid JSON: {ex.Message}" });
        }

        if (tasks == null || tasks.Count == 0)
            return JsonSerializer.Serialize(new { error = "No tasks provided" });

        var results = await Task.WhenAll(tasks.Select(async t =>
        {
            var sessName = t.Session ?? _lastSession ?? "default";
            if (!_sessions.ContainsKey(sessName))
                return new { session = sessName, error = $"Session '{sessName}' not found", stdout = (string?)null };

            var browser = _sessions[sessName];
            var result = await browser.RunAsync(t.Command ?? "", t.Args);
            return new { session = sessName, error = result.Error, stdout = result.Stdout };
        }));

        return JsonSerializer.Serialize(new { parallel_results = results });
    }

    /// <summary>
    /// List all active sessions.
    /// </summary>
    public string ListSessions()
    {
        var info = _sessions.ToDictionary(
            kv => kv.Key,
            kv => new { live_view_url = kv.Value.LiveViewUrl, connected = kv.Value.Connected });

        return JsonSerializer.Serialize(new { sessions = info, @default = _lastSession });
    }
}

public class ParallelTask
{
    [System.Text.Json.Serialization.JsonPropertyName("session")]
    public string? Session { get; set; }

    [System.Text.Json.Serialization.JsonPropertyName("command")]
    public string? Command { get; set; }

    [System.Text.Json.Serialization.JsonPropertyName("args")]
    public string[]? Args { get; set; }
}
