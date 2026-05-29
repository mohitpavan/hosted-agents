// Copyright (c) Microsoft. All rights reserved.

using System.Diagnostics;
using System.Text.RegularExpressions;

namespace BrowserAutomation;

/// <summary>
/// Manages a playwright-cli session against a remote CDP browser.
/// </summary>
public class BrowserSession
{
    private static readonly HashSet<string> AllowedCommands = new(StringComparer.OrdinalIgnoreCase)
    {
        "goto", "go-back", "go-forward", "reload",
        "snapshot", "screenshot",
        "click", "dblclick", "hover",
        "fill", "type", "press", "keys", "select", "check", "uncheck",
        "scroll", "eval",
        "tab-list", "tab-new", "tab-close", "tab-select",
        "wait", "state"
    };

    private static readonly Regex TokenPattern = new(@"\beyJ[a-zA-Z0-9._-]{20,}\b", RegexOptions.Compiled);

    public string SessionId { get; }
    public bool Connected { get; private set; }
    public string? LiveViewUrl { get; set; }

    private readonly int _timeoutSeconds;

    public BrowserSession(string sessionId, int timeoutSeconds = 180)
    {
        SessionId = sessionId;
        _timeoutSeconds = timeoutSeconds;
    }

    /// <summary>
    /// Connect to a remote browser via CDP URL.
    /// </summary>
    public async Task<BrowserResult> ConnectAsync(string cdpUrl)
    {
        // Quote the CDP URL — it contains &, =, and token chars that need protection
        var result = await ExecAsync($"attach \"--cdp={cdpUrl}\"");
        if (result.Success)
        {
            Connected = true;
            Console.WriteLine($"[Session] '{SessionId}' ATTACHED successfully");
            if (string.IsNullOrEmpty(result.Stdout))
                result = result with { Stdout = "Connected successfully to remote browser." };
        }
        else
        {
            Console.WriteLine($"[Session] '{SessionId}' ATTACH FAILED: {result.Error ?? result.Stderr}");
        }
        return result;
    }

    /// <summary>
    /// Run a playwright-cli command.
    /// </summary>
    public async Task<BrowserResult> RunAsync(string command, string[]? args = null)
    {
        if (!AllowedCommands.Contains(command))
            return new BrowserResult(false, Error: $"Unknown command: {command}. Allowed: {string.Join(", ", AllowedCommands.Order())}");

        if (!Connected)
            return new BrowserResult(false, Error: $"NOT CONNECTED. Create a session first before using '{command}'.");

        var fullCmd = command;
        if (args is { Length: > 0 })
        {
            var quoted = args.Select(a => a.Contains(' ') ? $"\"{a}\"" : a);
            fullCmd += " " + string.Join(" ", quoted);
        }

        return await ExecAsync(fullCmd);
    }

    /// <summary>
    /// Disconnect the session.
    /// </summary>
    public async Task CloseAsync()
    {
        if (Connected)
            await ExecAsync("detach");
        Connected = false;
    }

    private async Task<BrowserResult> ExecAsync(string command)
    {
        var cli = FindCli();
        var fullArgs = $"-s={SessionId} {command}";
        Console.WriteLine($"[pw-cli] {cli} -s={SessionId} {Redact(command)}");

        ProcessStartInfo psi = new()
        {
            FileName = cli,
            Arguments = fullArgs,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        try
        {
            using var process = Process.Start(psi)
                ?? throw new InvalidOperationException("Failed to start playwright-cli");

            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(_timeoutSeconds));

            var stdoutTask = process.StandardOutput.ReadToEndAsync(cts.Token);
            var stderrTask = process.StandardError.ReadToEndAsync(cts.Token);

            try
            {
                await process.WaitForExitAsync(cts.Token);
            }
            catch (OperationCanceledException)
            {
                process.Kill(entireProcessTree: true);
                return new BrowserResult(false, Error: "Command timed out");
            }

            var stdout = Redact(Truncate(await stdoutTask));
            var stderr = Redact(Truncate(await stderrTask));

            return new BrowserResult(
                Success: process.ExitCode == 0,
                Stdout: stdout,
                Stderr: string.IsNullOrEmpty(stderr) ? null : stderr,
                ExitCode: process.ExitCode);
        }
        catch (Exception ex)
        {
            return new BrowserResult(false, Error: $"Failed to run playwright-cli: {ex.Message}");
        }
    }

    private static string FindCli()
    {
        // Look for playwright-cli in PATH
        var pathDirs = Environment.GetEnvironmentVariable("PATH")?.Split(Path.PathSeparator) ?? Array.Empty<string>();
        foreach (var dir in pathDirs)
        {
            var candidate = Path.Combine(dir, "playwright-cli");
            if (File.Exists(candidate)) return candidate;
            // Windows
            var candidateExe = candidate + ".exe";
            if (File.Exists(candidateExe)) return candidateExe;
            // npm global (node_modules/.bin)
            var candidateCmd = candidate + ".cmd";
            if (File.Exists(candidateCmd)) return candidateCmd;
        }
        return "playwright-cli";
    }

    private static string Redact(string text) => TokenPattern.Replace(text, "<token>");

    private static string Truncate(string text, int maxLen = 12000) =>
        text.Length <= maxLen ? text : text[..maxLen] + "\n...[truncated]";
}

public record BrowserResult(
    bool Success,
    string? Stdout = null,
    string? Stderr = null,
    string? Error = null,
    int? ExitCode = null);
