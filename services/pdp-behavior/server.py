"""
ZTA Behavior PDP — windowed anomaly detection
==============================================
Peer to the OPA authorization PDP, sourced from the audit logger.
This is the second PDP shown in the proposal's Figure 1
("PDP for behavioral anomaly detection") and is structurally distinct
from the authz PDP in three important ways:

  (a) Inputs are *aggregates over time windows*, not per-request facts.
      The authz PDP decides "may agent A invoke target T at this moment?"
      The behavior PDP decides "has agent A's recent activity drifted
      outside its profile?"  These questions need different machinery.

  (b) Outputs are *advisory bands* (allow / step_up / block) plus a list
      of *reasons*, designed to be combined with — not override — the
      authz PDP's binary decision.  The sidecar enforces both: a step_up
      from this PDP can be combined with an authz allow to require a
      higher trust band, while a block from either PDP terminates.

  (c) State is *tenant-scoped, decayed, and explainable*.  Each rule
      reports which window contributed to its decision, so denials are
      auditable.  The authz PDP's Rego rules are also auditable, but at
      a different granularity (rule firing, not signal trajectory).

Detection rules (intentionally simple — this is a research artifact,
not an SOC product):
    R1  Denial-rate spike       — denial fraction in last N events > θ
    R2  Injection cluster       — ≥ K injection events in window W
    R3  Cross-target fan-out    — agent touched > F distinct targets in W
                                  (lateral movement signal)
    R4  Auth failure burst      — ≥ K auth.fail events in W
                                  (token guessing / replay)
    R5  Recent revocation       — agent has been quarantined in W

Each rule contributes a severity in [0,1].  A rule firing at severity
≥ STEP_UP_FLOOR drives the band to step_up; ≥ BLOCK_FLOOR drives it to
block.  The highest-severity rule wins.

References:
    Sun et al., "Behavior-aware Zero-Trust", IEEE JSAC 43(6):2089, 2025.
        — exponential-decay over historical events.
    Liu et al., arXiv:2508.19870, 2025.
        — multi-LLM agent ZTA: anomaly detection as a peer to authz.
    Chandola, Banerjee & Kumar, "Anomaly detection: a survey",
        ACM Computing Surveys 41(3), 2009.
        — windowed contextual anomalies; the framing for R1/R3.
"""

import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =============================================================================
# Config
# =============================================================================

PORT       = int(os.getenv("PORT", "8197"))
AUDIT_URL  = os.getenv("AUDIT_LOGGER_URL", "http://audit-logger:8195")
DISPATCHER_URL = os.getenv("DISPATCHER_URL", "http://revocation-dispatcher:8196")
SERVICE_NAME = "pdp-behavior"

# Windows
SHORT_WINDOW_S = int(os.getenv("SHORT_WINDOW_S", "300"))    # 5 min
LONG_WINDOW_S  = int(os.getenv("LONG_WINDOW_S",  "3600"))   # 1 hour

# Rule thresholds — tunable via env so the report can show sensitivity
DENIAL_RATE_FLOOR        = float(os.getenv("DENIAL_RATE_FLOOR",        "0.30"))  # 30% denials
DENIAL_RATE_MIN_EVENTS   = int(  os.getenv("DENIAL_RATE_MIN_EVENTS",   "10"))
INJECTION_CLUSTER_K      = int(  os.getenv("INJECTION_CLUSTER_K",      "3"))
FAN_OUT_F                = int(  os.getenv("FAN_OUT_F",                "5"))
AUTH_FAIL_BURST_K        = int(  os.getenv("AUTH_FAIL_BURST_K",        "5"))

# Band floors — at what severity do we escalate
STEP_UP_FLOOR = float(os.getenv("STEP_UP_FLOOR", "0.4"))
BLOCK_FLOOR   = float(os.getenv("BLOCK_FLOOR",   "0.8"))

# When the behavior PDP itself decides to *quarantine* an agent (severity 1.0
# rules), it can call the dispatcher.  Off by default — operators may want
# advisory-only mode while tuning thresholds.
AUTO_QUARANTINE = os.getenv("AUTO_QUARANTINE", "false").lower() == "true"
AUTO_QUARANTINE_TTL_S = int(os.getenv("AUTO_QUARANTINE_TTL_S", "900"))  # 15 min

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("zta-pdp-behavior")

_audit = httpx.Client(timeout=httpx.Timeout(0.5))
_dispatcher = httpx.Client(timeout=httpx.Timeout(0.5))


# =============================================================================
# Helpers
# =============================================================================

def _emit_audit(event_type: str, agent_id: str, data: dict, severity: float) -> None:
    try:
        _audit.post(f"{AUDIT_URL}/events", json={
            "event_type": event_type, "source": SERVICE_NAME,
            "agent_id": agent_id, "severity": severity, "data": data,
        })
    except Exception as e:
        logger.warning("audit emit failed: %s", e)


def _quarantine(agent_id: str, reason: str) -> None:
    if not AUTO_QUARANTINE:
        return
    try:
        _dispatcher.post(f"{DISPATCHER_URL}/quarantine/agent", json={
            "agent_id": agent_id, "reason": reason,
            "ttl_seconds": AUTO_QUARANTINE_TTL_S, "added_by": SERVICE_NAME,
        })
        logger.info("auto-quarantine|agent=%s|reason=%s", agent_id, reason)
    except Exception as e:
        logger.warning("quarantine call failed: %s", e)


def _fetch_events(agent_id: str, window_s: int, limit: int = 1000) -> list[dict]:
    since = time.time() - window_s
    try:
        r = _audit.get(f"{AUDIT_URL}/events",
                       params={"agent_id": agent_id, "since": since, "limit": limit})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.warning("fetch_events failed for %s: %s", agent_id, e)
    return []


# =============================================================================
# Rules — each returns (severity in [0,1], detail_dict) or (0.0, {})
# =============================================================================

DENY_TYPES = {"request.denied", "policy.deny", "waf.block", "microseg.deny",
              "auth.fail", "behavior.deny", "trust.injection", "dlp.hit"}


def rule_denial_rate(events: list[dict]) -> tuple[float, dict]:
    """R1: high denial fraction relative to total recent activity."""
    if len(events) < DENIAL_RATE_MIN_EVENTS:
        return 0.0, {}
    denies = sum(1 for e in events if e.get("event_type") in DENY_TYPES)
    rate = denies / len(events)
    if rate < DENIAL_RATE_FLOOR:
        return 0.0, {}
    # Linear ramp from floor (severity 0.4) to 1.0 at rate=1.0
    sev = 0.4 + 0.6 * ((rate - DENIAL_RATE_FLOOR) / (1.0 - DENIAL_RATE_FLOOR))
    return min(1.0, sev), {
        "rule": "denial_rate", "rate": round(rate, 3),
        "denies": denies, "total": len(events),
        "threshold": DENIAL_RATE_FLOOR,
    }


def rule_injection_cluster(events: list[dict]) -> tuple[float, dict]:
    """R2: K or more high-confidence injection events in the short window."""
    inj = [e for e in events if e.get("event_type") == "trust.injection"]
    if len(inj) < INJECTION_CLUSTER_K:
        return 0.0, {}
    # Severity scales with cluster size beyond threshold
    over = len(inj) - INJECTION_CLUSTER_K
    sev = min(1.0, 0.8 + 0.05 * over)
    return sev, {"rule": "injection_cluster", "count": len(inj),
                 "threshold": INJECTION_CLUSTER_K}


def rule_fan_out(events: list[dict]) -> tuple[float, dict]:
    """R3: lateral movement — same agent talking to many distinct targets."""
    targets = Counter(e.get("target") for e in events
                      if e.get("target") and e.get("event_type") == "request.allowed")
    distinct = len([t for t in targets if t])
    if distinct <= FAN_OUT_F:
        return 0.0, {}
    over = distinct - FAN_OUT_F
    sev = min(1.0, 0.4 + 0.1 * over)
    return sev, {"rule": "fan_out", "distinct_targets": distinct,
                 "threshold": FAN_OUT_F}


def rule_auth_fail_burst(events: list[dict]) -> tuple[float, dict]:
    """R4: cluster of authentication failures (token guessing / replay)."""
    fails = sum(1 for e in events if e.get("event_type") == "auth.fail")
    if fails < AUTH_FAIL_BURST_K:
        return 0.0, {}
    over = fails - AUTH_FAIL_BURST_K
    sev = min(1.0, 0.7 + 0.05 * over)
    return sev, {"rule": "auth_fail_burst", "count": fails,
                 "threshold": AUTH_FAIL_BURST_K}


def rule_recent_revocation(events: list[dict]) -> tuple[float, dict]:
    """R5: agent has been the subject of a revocation event recently.
    This isn't a 'detection' so much as a 'remember the last operator action'
    — useful when an operator quarantines an agent that's already cleared up."""
    revs = [e for e in events if e.get("event_type") == "revocation.issued"]
    if not revs:
        return 0.0, {}
    return 0.5, {"rule": "recent_revocation", "count": len(revs)}


# =============================================================================
# Decision
# =============================================================================

def band_from_severity(sev: float) -> str:
    if sev >= BLOCK_FLOOR:   return "block"
    if sev >= STEP_UP_FLOOR: return "step_up"
    return "allow"


def decide(agent_id: str) -> dict:
    short = _fetch_events(agent_id, SHORT_WINDOW_S)
    long_ = _fetch_events(agent_id, LONG_WINDOW_S)

    rule_results = []
    rule_results.append(rule_denial_rate(short))
    rule_results.append(rule_injection_cluster(short))
    rule_results.append(rule_fan_out(long_))
    rule_results.append(rule_auth_fail_burst(short))
    rule_results.append(rule_recent_revocation(short))

    # Highest severity rule wins (with its detail).
    fired = [(s, d) for s, d in rule_results if s > 0]
    if fired:
        fired.sort(key=lambda x: x[0], reverse=True)
        top_sev, top_detail = fired[0]
    else:
        top_sev, top_detail = 0.0, {}

    band = band_from_severity(top_sev)

    return {
        "agent_id": agent_id,
        "band": band,
        "severity": round(top_sev, 3),
        "top_rule": top_detail,
        "all_fired_rules": [d for _, d in fired],
        "windows": {
            "short_seconds": SHORT_WINDOW_S, "short_events": len(short),
            "long_seconds":  LONG_WINDOW_S,  "long_events":  len(long_),
        },
        "thresholds": {
            "step_up_floor": STEP_UP_FLOOR, "block_floor": BLOCK_FLOOR,
        },
    }


# =============================================================================
# API
# =============================================================================

class DecideRequest(BaseModel):
    agent_id: str = Field(..., description="Subject agent")
    target:   Optional[str] = None  # currently informational
    path:     Optional[str] = None  # currently informational


app = FastAPI(
    title="ZTA Behavior PDP",
    description="Windowed anomaly detection PDP for the federated ZTA control plane",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/decide")
def post_decide(req: DecideRequest):
    result = decide(req.agent_id)
    band = result["band"]
    if band != "allow":
        # Record our own decision into audit so the next caller sees it.
        ev_type = "behavior.deny" if band == "block" else "behavior.step_up"
        _emit_audit(ev_type, agent_id=req.agent_id, severity=result["severity"],
                    data={"top_rule": result["top_rule"],
                          "target": req.target, "path": req.path})
        if band == "block":
            _quarantine(req.agent_id,
                        reason=f"behavior-pdp:{result['top_rule'].get('rule', 'unknown')}")
    return result


@app.get("/agents/{agent_id}/decision")
def get_agent_decision(agent_id: str):
    """Read-only inspection — same as /decide but does not emit audit
    or trigger quarantine.  Useful for the report and for dashboards."""
    return decide(agent_id)


@app.get("/health")
def health():
    return {
        "status": "healthy", "service": SERVICE_NAME, "version": "1.0.0",
        "audit_url": AUDIT_URL, "dispatcher_url": DISPATCHER_URL,
        "auto_quarantine": AUTO_QUARANTINE,
        "rules": ["denial_rate", "injection_cluster", "fan_out",
                  "auth_fail_burst", "recent_revocation"],
        "thresholds": {
            "denial_rate_floor": DENIAL_RATE_FLOOR,
            "injection_cluster_k": INJECTION_CLUSTER_K,
            "fan_out_f": FAN_OUT_F,
            "auth_fail_burst_k": AUTH_FAIL_BURST_K,
            "step_up_floor": STEP_UP_FLOOR,
            "block_floor": BLOCK_FLOOR,
        },
        "windows": {"short_s": SHORT_WINDOW_S, "long_s": LONG_WINDOW_S},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
