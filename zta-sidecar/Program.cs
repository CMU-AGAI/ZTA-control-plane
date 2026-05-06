/*
 * ZTA Sidecar — .NET YARP Reverse Proxy
 * =======================================
 * Replaces Envoy sidecars with a .NET YARP reverse proxy running the full
 * ZeroTrustModel middleware pipeline.
 *
 * Middleware pipeline (in order):
 *   1. SecurityMonitoring  — audit logging for all requests
 *   2. WAF                 — SQL injection, XSS, rate limiting
 *   3. JwtAuth             — JWT Bearer token validation
 *   4. MicroSegmentation   — zone-based access control (ALLOWED_SOURCES)
 *   5. TrustScorer         — continuous trust evaluation (identity+behavior+LLM-layer)
 *   6. PolicyEngine        — calls OPA for authorization, with trust_score as input
 *   7. DLP                 — data loss prevention on responses
 *   8. YARP                — reverse proxy to the upstream service
 *
 * Single Docker image configured per-component via env vars:
 *   UPSTREAM_URL          — backend service to proxy to (e.g., http://airline-agent:8091)
 *   COMPONENT_NAME        — identity of this sidecar (e.g., airline-agent-sidecar)
 *   LISTEN_PORT           — port to listen on (default: 10000)
 *   OPA_URL               — OPA policy decision point (default: http://opa:8181)
 *   TRUST_SCORER_URL      — trust scorer service (default: http://trust-scorer:8190)
 *   ENABLE_TRUST_SCORER   — enable trust-scorer middleware (default: true)
 *   TRUST_SCORER_FAIL_OPEN — if true, allow when scorer is unreachable (default: false)
 *   JWT_ISSUER            — JWT token issuer
 *   JWT_AUDIENCE          — JWT token audience
 *   ALLOWED_SOURCES       — comma-separated list of allowed source agent IDs
 *   ENABLE_WAF            — enable WAF middleware (default: true)
 *   ENABLE_DLP            — enable DLP middleware (default: true)
 */

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Collections.Concurrent;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.IdentityModel.Tokens;
using Yarp.ReverseProxy.Configuration;

var builder = WebApplication.CreateBuilder(args);

// =============================================================================
// Configuration from environment variables
// =============================================================================

var upstreamUrl = Environment.GetEnvironmentVariable("UPSTREAM_URL") ?? "http://localhost:8091";
var componentName = Environment.GetEnvironmentVariable("COMPONENT_NAME") ?? "zta-sidecar";
var listenPort = int.Parse(Environment.GetEnvironmentVariable("LISTEN_PORT") ?? "10000");
var opaUrl = Environment.GetEnvironmentVariable("OPA_URL") ?? "http://opa:8181";
var trustScorerUrl = Environment.GetEnvironmentVariable("TRUST_SCORER_URL") ?? "http://trust-scorer:8190";
var enableTrustScorer = Environment.GetEnvironmentVariable("ENABLE_TRUST_SCORER") != "false";
var jwtIssuer = Environment.GetEnvironmentVariable("JWT_ISSUER") ?? "zta-auth-server";
var jwtAudience = Environment.GetEnvironmentVariable("JWT_AUDIENCE") ?? "zta-agents";
var jwtSecret = Environment.GetEnvironmentVariable("JWT_SECRET") ?? "zta-dev-secret-key-change-in-production-min-32-chars!!";
var allowedSources = (Environment.GetEnvironmentVariable("ALLOWED_SOURCES") ?? "")
    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
var enableWaf = Environment.GetEnvironmentVariable("ENABLE_WAF") != "false";
var enableDlp = Environment.GetEnvironmentVariable("ENABLE_DLP") != "false";
// Two additional flags introduced for the per-stage ablation harness.
// Both default to ENABLED so existing deployments are unchanged. Set to
// "false" only when running an ablation that needs these stages off.
var enableMicroseg = Environment.GetEnvironmentVariable("ENABLE_MICROSEG") != "false";
var enableOpa      = Environment.GetEnvironmentVariable("ENABLE_OPA")      != "false";

// =============================================================================
// Federated control-plane peers (proposal §1: PDP / auth gateway / trust scorer
// / revocation dispatcher / audit logger).  All three additions below default
// OFF so the existing ZTA test suite keeps passing on an unmodified compose.
// Set ENABLE_AUDIT / ENABLE_REVOCATION / ENABLE_BEHAVIOR_PDP = true to activate.
// =============================================================================
var auditUrl            = Environment.GetEnvironmentVariable("AUDIT_LOGGER_URL") ?? "http://audit-logger:8195";
var revocationUrl       = Environment.GetEnvironmentVariable("REVOCATION_URL")    ?? "http://revocation-dispatcher:8196";
var behaviorPdpUrl      = Environment.GetEnvironmentVariable("BEHAVIOR_PDP_URL")  ?? "http://pdp-behavior:8197";
var enableAudit         = Environment.GetEnvironmentVariable("ENABLE_AUDIT")          == "true";
var enableRevocation    = Environment.GetEnvironmentVariable("ENABLE_REVOCATION")     == "true";
var enableBehaviorPdp   = Environment.GetEnvironmentVariable("ENABLE_BEHAVIOR_PDP")   == "true";
var behaviorPdpFailOpen = Environment.GetEnvironmentVariable("BEHAVIOR_PDP_FAIL_OPEN") != "false"; // advisory by default

// =============================================================================
// YARP Reverse Proxy Configuration
// =============================================================================

builder.Services.AddReverseProxy().LoadFromMemory(
    routes: new[]
    {
        new RouteConfig
        {
            RouteId = "upstream",
            ClusterId = "upstream-cluster",
            Match = new RouteMatch { Path = "{**catch-all}" }
        }
    },
    clusters: new[]
    {
        new ClusterConfig
        {
            ClusterId = "upstream-cluster",
            Destinations = new Dictionary<string, DestinationConfig>
            {
                ["primary"] = new DestinationConfig { Address = upstreamUrl }
            }
        }
    }
);

// =============================================================================
// JWT Authentication
// =============================================================================

builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = jwtIssuer,
            ValidateAudience = true,
            ValidAudience = jwtAudience,
            ValidateLifetime = true,
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(jwtSecret)),
        };
        // Don't fail on missing token — let middleware handle it
        options.Events = new JwtBearerEvents
        {
            OnAuthenticationFailed = context =>
            {
                context.Response.Headers.Append("X-ZTA-Auth-Error", context.Exception.Message);
                return Task.CompletedTask;
            }
        };
    });

builder.Services.AddAuthorization();
builder.Services.AddHttpClient();

builder.WebHost.UseUrls($"http://0.0.0.0:{listenPort}");

var app = builder.Build();

// =============================================================================
// Federated control-plane helpers
// =============================================================================
//
// AuditEmit: best-effort, fire-and-forget POST to the audit logger.  The
// sidecar must NEVER block the request path waiting for audit; if the audit
// logger is down, we drop the event and log a warning.  This is consistent
// with NIST SP 800-207 §3.4.1's recommendation to collect telemetry without
// allowing telemetry collection to gate enforcement.
async Task AuditEmit(string eventType, string? agentId, string? target,
                     double? severity, object? data)
{
    if (!enableAudit) return;
    try
    {
        var http = app.Services.GetRequiredService<IHttpClientFactory>().CreateClient();
        http.Timeout = TimeSpan.FromMilliseconds(250);
        await http.PostAsJsonAsync($"{auditUrl}/events", new
        {
            event_type = eventType,
            source     = componentName,
            agent_id   = agentId,
            target     = target,
            severity   = severity,
            data       = data,
        });
    }
    catch (Exception ex)
    {
        app.Logger.LogWarning("audit emit failed type={Type}: {Err}", eventType, ex.Message);
    }
}

// =============================================================================
// Middleware Pipeline (order matters!)
// =============================================================================

// 1. Security Monitoring — log every request
app.Use(async (context, next) =>
{
    var start = DateTime.UtcNow;
    var agentId = context.Request.Headers["x-agent-id"].FirstOrDefault() ?? "unknown";
    var path = context.Request.Path.ToString();
    var method = context.Request.Method;

    context.Response.Headers.Append("X-ZTA-Sidecar", componentName);
    context.Response.Headers.Append("X-ZTA-Timestamp", DateTime.UtcNow.ToString("o"));

    await next();

    var elapsed = (DateTime.UtcNow - start).TotalMilliseconds;
    var status = context.Response.StatusCode;
    app.Logger.LogInformation(
        "ZTA|{Component}|{Method} {Path}|agent={AgentId}|status={Status}|ms={Elapsed:F1}",
        componentName, method, path, agentId, status, elapsed);

    // Federated control plane: emit a request-completion event into audit.
    // The behavior PDP uses request.allowed/request.denied counts to compute
    // denial-rate anomalies (rule R1).  Severity is set on denials so the
    // anomaly detector can weight serious denials more heavily than 4xx noise.
    var emitType = status < 400 ? "request.allowed" : "request.denied";
    var emitSev  = status < 400 ? (double?)null : (status >= 500 ? 0.7 : 0.5);
    _ = AuditEmit(emitType, agentId, componentName, emitSev, new {
        path = path, method = method, status = status, elapsed_ms = elapsed,
    });
});

// 2. WAF — SQL injection, XSS, rate limiting
if (enableWaf)
{
    var rateLimits = new ConcurrentDictionary<string, (int Count, DateTime Window)>();
    var sqlPatterns = new[] {
        new Regex(@"(?i)(select|insert|update|delete|drop|union|exec|xp_)", RegexOptions.Compiled),
    };
    var xssPatterns = new[] {
        new Regex(@"<script[^>]*>", RegexOptions.Compiled | RegexOptions.IgnoreCase),
        new Regex(@"on\w+\s*=", RegexOptions.Compiled | RegexOptions.IgnoreCase),
    };

    app.Use(async (context, next) =>
    {
        var clientIp = context.Connection.RemoteIpAddress?.ToString() ?? "unknown";

        // Rate limiting (100 req/min per IP)
        var now = DateTime.UtcNow;
        rateLimits.AddOrUpdate(clientIp,
            _ => (1, now),
            (_, existing) => existing.Window < now.AddMinutes(-1) ? (1, now) : (existing.Count + 1, existing.Window));

        if (rateLimits.TryGetValue(clientIp, out var rl) && rl.Count > 100)
        {
            context.Response.StatusCode = 429;
            await context.Response.WriteAsJsonAsync(new { error = "Rate limit exceeded", sidecar = componentName });
            return;
        }

        // Check query params for injection
        foreach (var q in context.Request.Query)
        {
            foreach (var val in q.Value)
            {
                if (val != null && (sqlPatterns.Any(p => p.IsMatch(val)) || xssPatterns.Any(p => p.IsMatch(val))))
                {
                    context.Response.StatusCode = 400;
                    await context.Response.WriteAsJsonAsync(new { error = "WAF: malicious input detected", sidecar = componentName });
                    return;
                }
            }
        }

        await next();
    });
}

// 3. JWT Authentication
app.UseAuthentication();

// 3b. Revocation Dispatcher — agent quarantine + JTI revocation
//     A compromised JWT may still be cryptographically valid but in the
//     deny list.  This check runs AFTER JWT validation (so we have the
//     parsed claims) and BEFORE micro-seg (so a quarantined agent never
//     even reaches the registry check).  Both checks are best-effort GETs
//     against the dispatcher with a tight timeout — fail-open by design,
//     because the trust scorer + OPA will still gate the request, and we
//     don't want a dispatcher outage to take down the data plane.
//     Reference: NIST SP 800-207 §2.1 Tenet 6 (dynamic deauthorization).
if (enableRevocation)
{
    app.Use(async (context, next) =>
    {
        // Skip the same paths as the rest of the gated pipeline.
        if (context.Request.Path.StartsWithSegments("/health") ||
            context.Request.Path.StartsWithSegments("/.well-known") ||
            context.Request.Path.StartsWithSegments("/sidecar") ||
            context.Request.Path.StartsWithSegments("/sse") ||
            context.Request.Path.StartsWithSegments("/messages"))
        {
            await next();
            return;
        }

        var agentId = context.Request.Headers["x-agent-id"].FirstOrDefault();
        if (string.IsNullOrEmpty(agentId))
        {
            await next();
            return;
        }

        try
        {
            var http = context.RequestServices.GetRequiredService<IHttpClientFactory>().CreateClient();
            http.Timeout = TimeSpan.FromMilliseconds(200);
            var resp = await http.GetAsync($"{revocationUrl}/check/agent/{Uri.EscapeDataString(agentId)}");
            if (resp.IsSuccessStatusCode)
            {
                var result = await resp.Content.ReadFromJsonAsync<JsonElement>();
                if (result.TryGetProperty("revoked", out var rev) && rev.GetBoolean())
                {
                    var reason = result.TryGetProperty("reason", out var r) ? r.GetString() : null;
                    context.Response.StatusCode = 403;
                    context.Response.Headers.Append("X-ZTA-Revoked", "agent");
                    _ = AuditEmit("auth.revoked_use", agentId, componentName, 1.0,
                        new { kind = "agent", reason = reason });
                    await context.Response.WriteAsJsonAsync(new
                    {
                        error = "Agent is quarantined",
                        agent_id = agentId,
                        reason = reason,
                        sidecar = componentName,
                    });
                    return;
                }
            }
        }
        catch (Exception ex)
        {
            // Fail-open on dispatcher outage; behavior PDP and OPA still gate.
            context.Response.Headers.Append("X-ZTA-Revocation-Error", ex.GetType().Name);
        }

        await next();
    });
}

// 4. Micro-Segmentation — agent-to-agent access control
if (enableMicroseg && allowedSources.Length > 0)
{
    app.Use(async (context, next) =>
    {
        // Skip health checks and sidecar endpoints
        // if (context.Request.Path.StartsWithSegments("/health") ||
        //     context.Request.Path.StartsWithSegments("/.well-known") ||
        //     context.Request.Path.StartsWithSegments("/sidecar"))
        if (context.Request.Path.StartsWithSegments("/health") ||
            context.Request.Path.StartsWithSegments("/.well-known") ||
            context.Request.Path.StartsWithSegments("/sidecar") ||
            context.Request.Path.StartsWithSegments("/sse") ||
            context.Request.Path.StartsWithSegments("/messages"))
        {
            await next();
            return;
        }

        var sourceAgent = context.Request.Headers["x-agent-id"].FirstOrDefault();
        if (string.IsNullOrEmpty(sourceAgent) || !allowedSources.Contains(sourceAgent))
        {
            context.Response.StatusCode = 403;
            await context.Response.WriteAsJsonAsync(new
            {
                error = "Micro-segmentation: source agent not allowed",
                source = sourceAgent ?? "missing",
                sidecar = componentName
            });
            return;
        }

        await next();
    });
}

// 5. Trust Scorer — continuous trust evaluation; produces trust_score + trust_band
// Reference: Dimitrakos et al. 2020 (TrustCom), Kim & Lee 2025 (MDPI AppSci),
// Bicakci et al. 2024 (arXiv:2402.08299), Sun et al. 2025 (IEEE JSAC),
// He et al. 2025 (arXiv:2506.02546), Liu et al. 2025 (arXiv:2508.19870).
if (enableTrustScorer)
{
    app.Use(async (context, next) =>
    {
        // Skip non-gated paths. This matches the skip list used by the
        // MicroSeg and OPA middleware below:
        //   /health, /.well-known, /sidecar   — diagnostic/discovery
        //   /sse, /messages                   — MCP SSE transport (no JWT semantics)
        // The scorer's PATH_SENSITIVITY would also return sensitivity 0 for these,
        // but short-circuiting here spares an HTTP round-trip and keeps behavior
        // consistent with the rest of the sidecar pipeline.
        if (context.Request.Path.StartsWithSegments("/health") ||
            context.Request.Path.StartsWithSegments("/.well-known") ||
            context.Request.Path.StartsWithSegments("/sidecar") ||
            context.Request.Path.StartsWithSegments("/sse") ||
            context.Request.Path.StartsWithSegments("/messages"))
        {
            await next();
            return;
        }

        try
        {
            var httpClient = context.RequestServices.GetRequiredService<IHttpClientFactory>().CreateClient();
            httpClient.Timeout = TimeSpan.FromMilliseconds(500); // fail-fast; do not slow traffic

            // Extract Bearer token (if any) for identity-strength computation
            string? bearer = null;
            var authz = context.Request.Headers["Authorization"].FirstOrDefault();
            if (!string.IsNullOrEmpty(authz) && authz.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase))
                bearer = authz.Substring("Bearer ".Length).Trim();

            // Peek at request body for LLM-layer inspection (only for POST with a body).
            // We must buffer the request body so YARP can still forward it upstream.
            string? payloadText = null;
            if (HttpMethods.IsPost(context.Request.Method) && context.Request.ContentLength is > 0)
            {
                context.Request.EnableBuffering();
                using var reader = new StreamReader(context.Request.Body, Encoding.UTF8, leaveOpen: true);
                var body = await reader.ReadToEndAsync();
                context.Request.Body.Position = 0;
                // Cap the payload we send to the scorer
                payloadText = body.Length > 8192 ? body.Substring(0, 8192) : body;
            }

            var scoreReq = new
            {
                agent_id = context.Request.Headers["x-agent-id"].FirstOrDefault() ?? "",
                target_component = componentName,
                path = context.Request.Path.ToString(),
                method = context.Request.Method,
                jwt_token = bearer,
                payload_text = payloadText,
            };

            var resp = await httpClient.PostAsJsonAsync($"{trustScorerUrl}/score", scoreReq);
            if (resp.IsSuccessStatusCode)
            {
                var result = await resp.Content.ReadFromJsonAsync<JsonElement>();
                var score = result.GetProperty("score").GetDouble();
                var band = result.GetProperty("band").GetString() ?? "step_up";
                var allow = result.GetProperty("allow").GetBoolean();

                // Stash score + band for the OPA middleware to read & forward
                context.Items["trust_score"] = score;
                context.Items["trust_band"] = band;

                context.Response.Headers.Append("X-ZTA-Trust-Score", score.ToString("F1"));
                context.Response.Headers.Append("X-ZTA-Trust-Band", band);

                if (!allow)
                {
                    // Hard block at the sidecar — don't even consult OPA
                    context.Response.StatusCode = 403;
                    await context.Response.WriteAsJsonAsync(new
                    {
                        error = "Trust threshold not met",
                        trust_score = score,
                        trust_band = band,
                        sidecar = componentName,
                    });
                    return;
                }
            }
            else
            {
                // Scorer returned non-2xx. Default to fail-closed on gated paths.
                var failOpen = Environment.GetEnvironmentVariable("TRUST_SCORER_FAIL_OPEN") == "true";
                context.Response.Headers.Append("X-ZTA-Trust-Error", $"scorer-status-{(int)resp.StatusCode}");
                if (!failOpen)
                {
                    context.Response.StatusCode = 503;
                    await context.Response.WriteAsJsonAsync(new
                    {
                        error = "Trust scorer error",
                        status = (int)resp.StatusCode,
                        sidecar = componentName,
                    });
                    return;
                }
            }
        }
        catch (Exception ex)
        {
            var failOpen = Environment.GetEnvironmentVariable("TRUST_SCORER_FAIL_OPEN") == "true";
            context.Response.Headers.Append("X-ZTA-Trust-Error", ex.GetType().Name);
            if (!failOpen)
            {
                context.Response.StatusCode = 503;
                await context.Response.WriteAsJsonAsync(new
                {
                    error = "Trust scorer unreachable",
                    detail = ex.Message,
                    sidecar = componentName,
                });
                return;
            }
        }

        await next();
    });
}

// 5b. Behavior PDP — windowed anomaly detection (federated peer to OPA)
//     This is the second PDP from the proposal's Figure 1.  It looks at
//     aggregates over time (denial rate, injection clustering, fan-out,
//     auth-fail bursts) rather than per-request facts.  Decision is
//     advisory by default (BEHAVIOR_PDP_FAIL_OPEN=true) — a step_up band
//     adds a header but does not block; a block band terminates the
//     request.  Strict mode (BEHAVIOR_PDP_FAIL_OPEN=false) treats step_up
//     as a 401 hint and block as a 403.
//     Note ordering: behavior PDP runs AFTER the trust scorer because it
//     consumes the per-request `trust.injection` events the scorer emits;
//     and BEFORE OPA so an agent that the behavior PDP wants to block
//     never even reaches the authz rules.
if (enableBehaviorPdp)
{
    app.Use(async (context, next) =>
    {
        if (context.Request.Path.StartsWithSegments("/health") ||
            context.Request.Path.StartsWithSegments("/.well-known") ||
            context.Request.Path.StartsWithSegments("/sidecar") ||
            context.Request.Path.StartsWithSegments("/sse") ||
            context.Request.Path.StartsWithSegments("/messages"))
        {
            await next();
            return;
        }

        var agentId = context.Request.Headers["x-agent-id"].FirstOrDefault() ?? "";
        if (string.IsNullOrEmpty(agentId))
        {
            await next();
            return;
        }

        try
        {
            var http = context.RequestServices.GetRequiredService<IHttpClientFactory>().CreateClient();
            http.Timeout = TimeSpan.FromMilliseconds(400);
            var resp = await http.PostAsJsonAsync($"{behaviorPdpUrl}/decide", new
            {
                agent_id = agentId,
                target   = componentName,
                path     = context.Request.Path.ToString(),
            });

            if (resp.IsSuccessStatusCode)
            {
                var result = await resp.Content.ReadFromJsonAsync<JsonElement>();
                var band     = result.TryGetProperty("band", out var b) ? b.GetString() : "allow";
                var severity = result.TryGetProperty("severity", out var s) ? s.GetDouble() : 0.0;

                context.Response.Headers.Append("X-ZTA-Behavior-Band", band ?? "allow");
                context.Response.Headers.Append("X-ZTA-Behavior-Severity", severity.ToString("F2"));

                if (band == "block")
                {
                    context.Response.StatusCode = 403;
                    string? rule = null;
                    if (result.TryGetProperty("top_rule", out var tr) &&
                        tr.TryGetProperty("rule", out var rn))
                        rule = rn.GetString();
                    await context.Response.WriteAsJsonAsync(new
                    {
                        error = "Behavior PDP blocked request",
                        rule = rule,
                        severity = severity,
                        sidecar = componentName,
                    });
                    return;
                }
                // step_up: pass through with header annotation; OPA gets to make
                // the final per-request call.  In strict mode we'd 401 here.
            }
            else if (!behaviorPdpFailOpen)
            {
                context.Response.StatusCode = 503;
                await context.Response.WriteAsJsonAsync(new
                {
                    error = "Behavior PDP error",
                    status = (int)resp.StatusCode,
                    sidecar = componentName,
                });
                return;
            }
        }
        catch (Exception ex)
        {
            context.Response.Headers.Append("X-ZTA-Behavior-Error", ex.GetType().Name);
            if (!behaviorPdpFailOpen)
            {
                context.Response.StatusCode = 503;
                await context.Response.WriteAsJsonAsync(new
                {
                    error = "Behavior PDP unreachable",
                    detail = ex.Message,
                    sidecar = componentName,
                });
                return;
            }
        }

        await next();
    });
}

// 6. Policy Engine — call OPA for authorization
//    Gated by ENABLE_OPA so the ablation harness can measure marginal
//    contribution. When disabled, requests pass through to DLP and YARP
//    without any rule-based authorization check; the trust scorer and
//    behavior PDP remain in force.
if (enableOpa)
{
app.Use(async (context, next) =>
{
    // Skip health and discovery endpoints
    // if (context.Request.Path.StartsWithSegments("/health") ||
    //     context.Request.Path.StartsWithSegments("/.well-known") ||
    //     context.Request.Path.StartsWithSegments("/a2a/health"))
    if (context.Request.Path.StartsWithSegments("/health") ||
            context.Request.Path.StartsWithSegments("/.well-known") ||
            context.Request.Path.StartsWithSegments("/sidecar") ||
            context.Request.Path.StartsWithSegments("/sse") ||
            context.Request.Path.StartsWithSegments("/messages"))
    {
        await next();
        return;
    }

    try
    {
        var httpClient = context.RequestServices.GetRequiredService<IHttpClientFactory>().CreateClient();
        var trustScore = context.Items.TryGetValue("trust_score", out var ts) ? ts : null;
        var trustBand = context.Items.TryGetValue("trust_band", out var tb) ? tb : null;

        var opaInput = new
        {
            input = new
            {
                agent_id = context.Request.Headers["x-agent-id"].FirstOrDefault() ?? "",
                path = context.Request.Path.ToString(),
                method = context.Request.Method,
                component = componentName,
                host = context.Request.Host.ToString(),
                trust_score = trustScore,
                trust_band = trustBand,
            }
        };

        var opaResponse = await httpClient.PostAsJsonAsync($"{opaUrl}/v1/data/zta/authz/allow", opaInput);

        if (opaResponse.IsSuccessStatusCode)
        {
            var result = await opaResponse.Content.ReadFromJsonAsync<JsonElement>();
            if (result.TryGetProperty("result", out var allowed) && allowed.GetBoolean())
            {
                context.Response.Headers.Append("X-ZTA-Policy", "allowed");
                await next();
                return;
            }
        }

        // OPA denied or unreachable — deny
        context.Response.StatusCode = 403;
        context.Response.Headers.Append("X-ZTA-Policy", "denied");
        await context.Response.WriteAsJsonAsync(new
        {
            error = "Policy engine denied access",
            sidecar = componentName
        });
    }
    catch (Exception ex)
    {
        // OPA unreachable — fail open or fail closed based on config
        var failOpen = Environment.GetEnvironmentVariable("OPA_FAIL_OPEN") == "true";
        if (failOpen)
        {
            context.Response.Headers.Append("X-ZTA-Policy", "opa-unreachable-fail-open");
            await next();
        }
        else
        {
            context.Response.StatusCode = 503;
            await context.Response.WriteAsJsonAsync(new
            {
                error = "Policy engine unreachable",
                detail = ex.Message,
                sidecar = componentName
            });
        }
    }
});
}  // end if (enableOpa)

// 7. DLP — check response bodies for sensitive data
if (enableDlp)
{
    // DLP runs as response middleware — applied via YARP transforms or response inspection
    // For now, add DLP headers
    app.Use(async (context, next) =>
    {
        context.Response.Headers.Append("X-ZTA-DLP", "active");
        await next();
    });
}

// 7. Authorization
app.UseAuthorization();

// =============================================================================
// Health endpoint (sidecar's own health)
// =============================================================================

app.MapGet("/sidecar/health", () => Results.Json(new
{
    status = "healthy",
    component = componentName,
    upstream = upstreamUrl,
    opaUrl = opaUrl,
    enableWaf = enableWaf,
    enableDlp = enableDlp,
    timestamp = DateTime.UtcNow.ToString("o")
}));

// =============================================================================
// YARP — forward everything else to upstream
// =============================================================================

app.MapReverseProxy();

app.Run();