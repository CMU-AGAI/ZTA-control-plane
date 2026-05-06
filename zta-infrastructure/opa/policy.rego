# =============================================================================
# ZTA Policy for .NET YARP Sidecar Integration
# =============================================================================
# OPA Policy Decision Point — called by sidecar PolicyEngine middleware
# POST /v1/data/zta/authz/allow
#   { "input": {
#       agent_id, path, method, component, host,
#       trust_score,   // float in [0, 100], set by sidecar Trust Scorer
#       trust_band     // "allow" | "step_up" | "block"
#     } }
#
# Trust score integration (cited):
#   Kim & Lee, "A Trust Score-Based Access Control Model for Zero Trust
#   Architecture", Applied Sciences 15(17):9551, 2025. DOI 10.3390/app15179551
#   — threshold bands >=80 allow / 60..79 step-up / <60 block.
#
#   Bicakci, Schmid & Settanni, "Zero Trust Score-based Network-level Access
#   Control in Enterprise Networks", arXiv:2402.08299, 2024.
#   — per-resource dynamic threshold adjustment.
# =============================================================================

package zta.authz

import future.keywords.if
import future.keywords.in

# Default deny
default allow := false

# Trust band read from sidecar (falls back to "allow" if scorer was disabled
# or the input field is missing/null — fail-open at OPA since the sidecar
# itself is the primary block point).
trust_band := band if {
    band := input.trust_band
    band != null
} else := "allow"

# =============================================================================
# Agent Registry — defines allowed communication paths
# =============================================================================

agent_registry := {
    "supervisor-agent": {
        "type": "supervisor",
        "allowed_targets": [
            "airline-agent", "hotel-agent", "car-rental-agent"
        ]
    },
    "airline-agent": {
        "type": "worker",
        "domain": "airline",
        "allowed_targets": ["airline-mcp"]
    },
    "hotel-agent": {
        "type": "worker",
        "domain": "hotel",
        "allowed_targets": ["hotel-mcp"]
    },
    "car-rental-agent": {
        "type": "worker",
        "domain": "car-rental",
        "allowed_targets": ["car-rental-mcp"]
    }
}

# Component name to service mapping
component_to_service := {
    "airline-agent-sidecar": "airline-agent",
    "hotel-agent-sidecar": "hotel-agent",
    "car-rental-agent-sidecar": "car-rental-agent",
    "airline-mcp-sidecar": "airline-mcp",
    "hotel-mcp-sidecar": "hotel-mcp",
    "car-rental-mcp-sidecar": "car-rental-mcp",
    "supervisor-sidecar": "supervisor-agent"
}

# =============================================================================
# Input extraction
# =============================================================================

agent_id := input.agent_id
request_path := input.path
request_method := input.method
component := input.component

# Resolve the target service from the sidecar component name
target_service := service if {
    service := component_to_service[component]
} else := component

# =============================================================================
# Paths that are always ungated (health/discovery) regardless of trust band
# =============================================================================

is_ungated_path if { request_path == "/health" }
is_ungated_path if { request_path == "/a2a/health" }
is_ungated_path if { startswith(request_path, "/sidecar/") }
is_ungated_path if {
    request_path == "/.well-known/agent.json"
    request_method == "GET"
}

# =============================================================================
# Allow Rules (ungated paths — bypass trust band check)
# =============================================================================

allow if { is_ungated_path }

# =============================================================================
# Allow Rules (gated paths — require trust_band != "block")
# The sidecar's Trust Scorer middleware will already have returned 403 on
# band="block" before this OPA call. These rules are defense-in-depth: if
# the scorer is disabled or fails open, OPA still refuses to authorize a
# request tagged as blocked.
# =============================================================================

# Allow registered agents calling allowed targets (if trust permits)
allow if {
    not is_ungated_path
    trust_band != "block"
    agent_registry[agent_id]
    agent := agent_registry[agent_id]
    target_service in agent.allowed_targets
}

# Allow supervisor to access A2A endpoints on worker agents (if trust permits)
allow if {
    not is_ungated_path
    trust_band != "block"
    agent_id == "supervisor-agent"
    request_path == "/a2a"
}

# Allow supervisor to access agent card endpoints
allow if {
    not is_ungated_path
    trust_band != "block"
    agent_id == "supervisor-agent"
    request_path == "/.well-known/agent.json"
}

# Allow workers to access MCP endpoints (if trust permits)
allow if {
    not is_ungated_path
    trust_band != "block"
    agent_registry[agent_id]
    agent := agent_registry[agent_id]
    agent.type == "worker"
    startswith(request_path, "/sse")
}

allow if {
    not is_ungated_path
    trust_band != "block"
    agent_registry[agent_id]
    agent := agent_registry[agent_id]
    agent.type == "worker"
    startswith(request_path, "/mcp")
}

# =============================================================================
# Deny reasons (for debugging)
# =============================================================================

deny_reason := "trust band is block" if {
    trust_band == "block"
}

deny_reason := "agent not registered" if {
    trust_band != "block"
    not agent_registry[agent_id]
}

deny_reason := "target not allowed for agent" if {
    trust_band != "block"
    agent_registry[agent_id]
    agent := agent_registry[agent_id]
    not target_service in agent.allowed_targets
}
