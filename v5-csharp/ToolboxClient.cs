// Copyright (c) Microsoft. All rights reserved.

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;

namespace BrowserAutomation;

/// <summary>
/// MCP client for Foundry Toolbox — handles JSON-RPC over HTTP.
/// </summary>
public class ToolboxClient
{
    private const string TokenScope = "https://ai.azure.com/.default";
    private const string Features = "Toolboxes=V1Preview";

    private readonly string _endpoint;
    private readonly TokenCredential _credential;
    private readonly HttpClient _httpClient;
    private string? _sessionId;
    private int _reqId;
    private bool _initialized;

    public ToolboxClient(string endpoint, TokenCredential credential)
    {
        _endpoint = endpoint;
        _credential = credential;
        _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(90) };
    }

    /// <summary>
    /// Call a tool on the Toolbox via MCP JSON-RPC.
    /// </summary>
    public async Task<JsonElement> CallToolAsync(string name, Dictionary<string, object>? arguments = null)
    {
        await EnsureInitializedAsync();

        var payload = new
        {
            jsonrpc = "2.0",
            id = NextId(),
            method = "tools/call",
            @params = new { name, arguments = arguments ?? new Dictionary<string, object>() }
        };

        var response = await PostAsync(payload);
        var doc = JsonDocument.Parse(response);

        if (doc.RootElement.TryGetProperty("error", out var error))
            throw new InvalidOperationException($"Toolbox error: {error}");

        var result = doc.RootElement.GetProperty("result");

        // Extract text content from MCP response
        if (result.TryGetProperty("content", out var content) && content.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in content.EnumerateArray())
            {
                if (item.TryGetProperty("type", out var type) && type.GetString() == "text"
                    && item.TryGetProperty("text", out var text))
                {
                    // Try to parse as JSON
                    try
                    {
                        return JsonDocument.Parse(text.GetString()!).RootElement.Clone();
                    }
                    catch (JsonException)
                    {
                        // Return as raw text wrapped in JSON
                        return JsonDocument.Parse($"{{\"raw\":\"{text.GetString()}\"}}").RootElement.Clone();
                    }
                }
            }
        }

        return result.Clone();
    }

    private async Task EnsureInitializedAsync()
    {
        if (_initialized) return;

        // MCP initialize
        var initPayload = new
        {
            jsonrpc = "2.0",
            id = NextId(),
            method = "initialize",
            @params = new
            {
                protocolVersion = "2024-11-05",
                capabilities = new { },
                clientInfo = new { name = "browser-automation-csharp", version = "1.0.0" }
            }
        };

        var response = await PostAsync(initPayload, captureSessionId: true);
        Console.WriteLine($"[Toolbox] Initialized, session={_sessionId}");

        // Send initialized notification
        var notifyPayload = new { jsonrpc = "2.0", method = "notifications/initialized" };
        await PostAsync(notifyPayload);

        _initialized = true;
    }

    private async Task<string> PostAsync(object payload, bool captureSessionId = false)
    {
        var token = await _credential.GetTokenAsync(
            new TokenRequestContext(new[] { TokenScope }), CancellationToken.None);

        var request = new HttpRequestMessage(HttpMethod.Post, _endpoint);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
        request.Headers.Add("Foundry-Features", Features);

        if (_sessionId != null)
            request.Headers.Add("mcp-session-id", _sessionId);

        var json = JsonSerializer.Serialize(payload);
        request.Content = new StringContent(json, Encoding.UTF8, "application/json");

        var response = await _httpClient.SendAsync(request);
        response.EnsureSuccessStatusCode();

        if (captureSessionId && response.Headers.TryGetValues("mcp-session-id", out var values))
            _sessionId = values.FirstOrDefault();

        return await response.Content.ReadAsStringAsync();
    }

    private int NextId() => Interlocked.Increment(ref _reqId);
}
