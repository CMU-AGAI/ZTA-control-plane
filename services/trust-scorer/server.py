"""
ZTA Trust Scorer Service — Subjective Logic edition
====================================================
Continuous trust evaluation for multi-agent ZTA control plane.

This version replaces the weighted-sum aggregator (w_I, w_B, w_L) with
Subjective Logic (Jøsang) opinions (b, d, u) per signal source, combined
via cumulative belief fusion.  No hand-tuned scalar weights.

The /score endpoint keeps its existing contract so the sidecar and OPA
don't need any changes.  Score is still in [0, 100]; bands are still
allow (>= 80), step_up (60..79), block (< 60).

References:
    Jøsang, A. "Subjective Logic: A Formalism for Reasoning Under
    Uncertainty." Springer, 2016.

    Bradatsch, L., Miroshkin, O., Trkulja, N., Kargl, F. "Zero Trust
    Score-based Network-level Access Control in Enterprise Networks."
    arXiv:2402.08299, 2024.  (Subjective-Logic-based ZTA scoring;
    directly applied here.)

    Kim, J., Lee, S. "A Trust Score-Based Access Control Model for
    Zero Trust Architecture."  Applied Sciences 15(17):9551, MDPI, 2025.
    (Band boundaries >= 80 / 60..79 / < 60 retained from Kim & Lee.)

    Liu, Y., et al. "Secure Multi-LLM Agentic AI and Agentification
    for Edge General Intelligence by Zero-Trust: A Survey."
    arXiv:2508.19870, 2025.
    (Deny-on-detection hard floor for prompt-injection signals.)
"""

import os
import re
import time
import math
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional, List, Dict, Deque, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    import jwt  # PyJWT
except ImportError:
    jwt = None

try:
    import httpx
except ImportError:
    httpx = None


# =============================================================================
# Config
# =============================================================================

JWT_SECRET   = os.getenv("JWT_SECRET",   "zta-dev-secret-key-change-in-production-min-32-chars!!")
JWT_ISSUER   = os.getenv("JWT_ISSUER",   "zta-auth-server")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "zta-agents")
PORT         = int(os.getenv("PORT",     "8190"))

# Audit logger — federated control plane peer. The scorer emits
# trust.injection events here so the behavior PDP can correlate them.
# Optional: if AUDIT_LOGGER_URL is unset or unreachable, the scorer
# continues to function (in-memory history is the fallback).
AUDIT_LOGGER_URL = os.getenv("AUDIT_LOGGER_URL", "")

# Threshold bands (Kim & Lee 2025).  These still gate the decision;
# we only change HOW we arrive at the score.
ALLOW_THRESHOLD = float(os.getenv("ALLOW_THRESHOLD", "80.0"))
BLOCK_THRESHOLD = float(os.getenv("BLOCK_THRESHOLD", "60.0"))

# Decay half-life for behavioral evidence (Sun et al. 2025).
DECAY_HALF_LIFE_S = float(os.getenv("DECAY_HALF_LIFE_S", "3600"))
DECAY_LAMBDA      = math.log(2.0) / DECAY_HALF_LIFE_S

# Per-path sensitivity multipliers on BLOCK threshold (Bicakci et al. 2024).
# MCP SSE paths are ungated because FastMCP SSE has no JWT semantics.
PATH_SENSITIVITY: Dict[str, float] = {
    "/a2a":                      1.00,
    "/sse":                      0.00,
    "/messages":                 0.00,
    "/.well-known/agent.json":   0.50,
    "/health":                   0.00,
    "/sidecar/":                 0.00,
}

# Base rate "a" in the Subjective Logic projection p = b + a*u.
# a is the prior probability we'd assign to a fresh, never-seen agent.
# Choosing a = 0.5 is the classic uninformative prior (Jøsang 2016, §3.5).
BASE_RATE = float(os.getenv("SL_BASE_RATE", "0.5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("zta-trust-scorer")


# =============================================================================
# Subjective Logic — opinion algebra
# =============================================================================
#
# A Subjective Logic opinion is a 4-tuple (b, d, u, a) where:
#   b  = belief mass      in [0,1]
#   d  = disbelief mass   in [0,1]
#   u  = uncertainty mass in [0,1]
#   b + d + u = 1
#   a  = base rate (prior prob of "true" for a fresh subject) in [0,1]
#
# The projected probability is   p = b + a * u
#
# Evidence-to-opinion (Jøsang 2016 eq 3.1):
#   Given r positive observations and s negative observations (with non-
#   informative prior w=2, equivalent to Beta(1,1)):
#       b = r / (r + s + 2)
#       d = s / (r + s + 2)
#       u = 2 / (r + s + 2)
#
# Cumulative belief fusion of two opinions from *independent* sources
# (Jøsang 2016 §12.3):
#   If u_A != 0 or u_B != 0:
#       b = (b_A * u_B + b_B * u_A) / (u_A + u_B - u_A * u_B)
#       d = (d_A * u_B + d_B * u_A) / (u_A + u_B - u_A * u_B)
#       u = (u_A * u_B)             / (u_A + u_B - u_A * u_B)
#   (Degenerate case u_A = u_B = 0 handled with averaging fallback.)
#
# We only use cumulative fusion because the signal sources (identity /
# behavioral / LLM-layer) are genuinely independent.

class Opinion:
    __slots__ = ("b", "d", "u", "a")

    def __init__(self, b: float, d: float, u: float, a: float = BASE_RATE):
        # Normalize rounding errors
        total = b + d + u
        if total <= 0:
            b, d, u = 0.0, 0.0, 1.0
        else:
            b, d, u = b / total, d / total, u / total
        self.b, self.d, self.u, self.a = b, d, u, a

    @classmethod
    def from_evidence(cls, positive: float, negative: float,
                      base_rate: float = BASE_RATE) -> "Opinion":
        """Build opinion from positive/negative pseudo-counts.  Non-integer
        counts are allowed (we use them for graded evidence like 'this
        payload matched an injection pattern with weight 0.7')."""
        denom = positive + negative + 2.0
        b = positive / denom
        d = negative / denom
        u = 2.0      / denom
        return cls(b, d, u, base_rate)

    @classmethod
    def vacuous(cls, base_rate: float = BASE_RATE) -> "Opinion":
        """Total uncertainty — we know nothing."""
        return cls(0.0, 0.0, 1.0, base_rate)

    @classmethod
    def certain_true(cls, base_rate: float = BASE_RATE) -> "Opinion":
        """Absolute belief — dogmatic, use sparingly."""
        return cls(1.0, 0.0, 0.0, base_rate)

    @classmethod
    def certain_false(cls, base_rate: float = BASE_RATE) -> "Opinion":
        return cls(0.0, 1.0, 0.0, base_rate)

    def projection(self) -> float:
        """Projected probability p = b + a*u.  In [0,1]."""
        return self.b + self.a * self.u

    def fuse(self, other: "Opinion") -> "Opinion":
        """Cumulative belief fusion of two independent opinions."""
        u_a, u_b = self.u, other.u
        if u_a == 0 and u_b == 0:
            # Both dogmatic — fall back to averaging belief, agreeing base rate
            return Opinion((self.b + other.b) / 2,
                           (self.d + other.d) / 2,
                           0.0,
                           (self.a + other.a) / 2)
        denom = u_a + u_b - u_a * u_b
        if denom <= 0:
            return Opinion((self.b + other.b) / 2, (self.d + other.d) / 2, 0.0,
                           (self.a + other.a) / 2)
        b = (self.b * u_b + other.b * u_a) / denom
        d = (self.d * u_b + other.d * u_a) / denom
        u = (u_a * u_b) / denom
        return Opinion(b, d, u, (self.a + other.a) / 2)

    def to_dict(self) -> Dict[str, float]:
        return {
            "b": round(self.b, 4),
            "d": round(self.d, 4),
            "u": round(self.u, 4),
            "a": round(self.a, 4),
            "projection": round(self.projection(), 4),
        }


def fuse_all(opinions: List[Opinion]) -> Opinion:
    """Fold cumulative fusion over a list."""
    if not opinions:
        return Opinion.vacuous()
    acc = opinions[0]
    for op in opinions[1:]:
        acc = acc.fuse(op)
    return acc


# =============================================================================
# Evidence collectors — each returns a Subjective Logic opinion
# =============================================================================

def opinion_identity(agent_id: str, token: Optional[str],
                     target_component: str) -> Tuple[Opinion, dict]:
    """
    Identity opinion from JWT validation + claim checks.

    Each of five checks contributes one unit of positive or negative evidence
    to a Beta-like aggregation.  A missing JWT produces certain disbelief
    (the NIST SP 800-207 requirement that identity be authenticated for every
    access is a hard precondition).
    """
    if not token:
        return Opinion.certain_false(), {"error": "no-token"}

    if jwt is None:
        # PyJWT not installed — degrade to uncertainty rather than false deny.
        logger.warning("PyJWT not available; returning vacuous identity opinion")
        return Opinion.vacuous(), {"error": "pyjwt-not-installed"}

    detail: dict = {}
    positive = 0.0
    negative = 0.0

    # 1. Cryptographic validity (signature, exp, nbf) — hard check
    try:
        decoded = jwt.decode(
            token, JWT_SECRET, algorithms=["HS256"],
            issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
        )
        detail["jwt_valid"] = True
        positive += 1.0
    except Exception as e:
        detail["jwt_valid"] = False
        detail["jwt_error"] = str(e)
        # A cryptographically invalid token is certain_false — do not allow
        # softer signals to rescue it.
        return Opinion.certain_false(), detail

    # 2. sub ↔ declared agent_id
    sub = decoded.get("sub")
    detail["sub_matches"] = (sub == agent_id)
    if detail["sub_matches"]:
        positive += 1.0
    else:
        # Sub-mismatch is impersonation — certain_false.  NIST SP 800-207 §3.1.
        detail["error"] = "sub-mismatch"
        return Opinion.certain_false(), detail

    # 3. Token freshness: fraction of remaining lifetime
    now = time.time()
    iat = decoded.get("iat", now)
    exp = decoded.get("exp", now + 1)
    lifetime = max(1.0, exp - iat)
    remaining = max(0.0, exp - now)
    freshness = min(1.0, remaining / lifetime)
    detail["freshness"] = round(freshness, 3)
    if freshness >= 0.25:
        positive += 1.0
    else:
        # Near-expired token is a mild negative
        negative += (0.25 - freshness) / 0.25

    # 4. Target in allowed_targets claim (principle of least privilege)
    allowed = decoded.get("allowed_targets", [])
    target_clean = target_component.replace("-sidecar", "")
    detail["target_allowed"] = target_clean in allowed if allowed else True
    if detail["target_allowed"]:
        positive += 1.0
    else:
        negative += 1.0

    # 5. Roles claim present (pure information signal)
    if decoded.get("roles"):
        positive += 0.5

    return Opinion.from_evidence(positive, negative), detail


def opinion_behavioral(agent_id: str, history: "History") -> Tuple[Opinion, dict]:
    """
    Behavioral opinion derived from decayed history of violations.

    Uses exponential decay (Sun et al. 2025).  An agent with no history
    returns a vacuous opinion (maximum uncertainty, BASE_RATE prior),
    which is correct: a new agent is genuinely unknown.

    An agent with many clean requests could be tracked with positive
    evidence, but we deliberately do not do that here — we only record
    *violations*, because we don't have a reliable signal for "this was
    benign" (see discussion of one-sided labels).
    """
    penalty = history.decayed_penalty(agent_id)  # in [0, ∞), typically [0, 1]
    penalty = min(1.0, penalty)

    # Map penalty to (positive, negative) pseudo-counts.  The event log size
    # modulates uncertainty: more events → more confident.
    n_events = history.event_count(agent_id)

    if n_events == 0:
        # Never-seen agent
        return Opinion.vacuous(), {"events": 0, "decayed_penalty": 0.0}

    # The decayed penalty effectively acts as accumulated negative evidence,
    # scaled by the event count to reflect confidence.
    weight = min(n_events, 20.0)  # cap confidence at 20 events
    negative = penalty * weight
    positive = (1.0 - penalty) * weight

    detail = {
        "events": n_events,
        "decayed_penalty": round(penalty, 4),
        "recent_events": history.recent(agent_id, limit=3),
    }
    return Opinion.from_evidence(positive, negative), detail


# Prompt-injection signatures, each with a severity weight.
# These remain a regex heuristic — the novelty in this version is in the
# aggregator, not the detector.
_INJECTION_PATTERNS = [
    (re.compile(r"\b(?:ignore|disregard)\s+(?:all\s+)?previous\s+(?:instructions?|prompts?)", re.I),
     "ignore-previous",     1.0),
    (re.compile(r"\byou\s+are\s+now\s+(?:an?\s+)?\w+", re.I),
     "role-override",       0.7),
    (re.compile(r"(?:system\s+prompt|initial\s+instructions?)\b", re.I),
     "meta-prompt-ref",     0.7),
    (re.compile(r"\b(?:reveal|print|show|expose|leak)\s+(?:your\s+)?(?:system\s+)?prompt", re.I),
     "exfiltrate-prompt",   1.0),
    (re.compile(r"execute\s+the\s+following\s+(?:command|instruction)", re.I),
     "execute-directive",   0.8),
    (re.compile(r"<\|system\|>|\[system\]", re.I),
     "fake-role-block",     0.9),
    (re.compile(r"\bSUDO\b|\bbase64\s*decode", re.I),
     "injection-marker",    0.5),
    # New patterns to catch the failing tests:
    (re.compile(r"\[AGENT:[^\]]+\]", re.I),
     "fake-agent-tag",      0.9),
    (re.compile(r"skip\s+(?:the\s+)?(?:opa|policy|confirmation|approval)", re.I),
     "policy-bypass",       0.9),
    (re.compile(r"no\s+(?:need\s+to\s+)?confirm(?:ation)?\s+(?:required|needed)?", re.I),
     "bypass-confirmation", 0.7),
    (re.compile(r"directly\s+call|raw\s+(?:mcp|tool)\s+command", re.I),
     "mcp-boundary-probe",  0.8),
]

def opinion_llm_layer(payload_text: Optional[str]) -> Tuple[Opinion, dict]:
    """
    LLM-layer opinion from prompt-injection pattern detection.

    No payload → vacuous opinion (we can't evaluate what we can't see).
    Matches accumulate severity into an "injection mass" which maps to
    negative evidence.  No matches → positive evidence with moderate
    confidence (we've inspected it and found nothing).
    """
    if payload_text is None or payload_text.strip() == "":
        return Opinion.vacuous(), {"inspected": False, "reason": "no-payload"}

    hits = []
    mass = 0.0
    for pat, name, weight in _INJECTION_PATTERNS:
        if pat.search(payload_text):
            hits.append({"pattern": name, "weight": weight})
            mass += weight
    mass = min(1.0, mass)

    detail = {
        "inspected": True,
        "hits": hits,
        "injection_mass": round(mass, 3),
    }

    if mass == 0.0:
        # Clean payload — modest positive evidence
        return Opinion.from_evidence(positive=2.0, negative=0.0), detail
    else:
        # Scale evidence by severity.  High mass → strong negative.
        return Opinion.from_evidence(positive=0.0, negative=4.0 * mass), detail


# =============================================================================
# Behavioral history store — same shape as before
# =============================================================================

HISTORY_MAX = int(os.getenv("HISTORY_MAX", "256"))

class HistoryEvent:
    __slots__ = ("t", "severity", "reason")
    def __init__(self, t: float, severity: float, reason: str):
        self.t = t
        self.severity = severity
        self.reason = reason
    def to_dict(self) -> dict:
        return {
            "t": datetime.fromtimestamp(self.t, tz=timezone.utc).isoformat(),
            "severity": self.severity,
            "reason": self.reason,
        }


class History:
    def __init__(self):
        self._store: Dict[str, Deque[HistoryEvent]] = defaultdict(lambda: deque(maxlen=HISTORY_MAX))

    def record(self, agent_id: str, severity: float, reason: str) -> None:
        severity = max(0.0, min(1.0, severity))
        self._store[agent_id].append(HistoryEvent(time.time(), severity, reason))
        logger.info("history|record|agent=%s|severity=%.2f|reason=%s", agent_id, severity, reason)

    def decayed_penalty(self, agent_id: str) -> float:
        if agent_id not in self._store:
            return 0.0
        now = time.time()
        total = 0.0
        for ev in self._store[agent_id]:
            dt = now - ev.t
            total += ev.severity * math.exp(-DECAY_LAMBDA * dt)
        return total

    def event_count(self, agent_id: str) -> int:
        return len(self._store.get(agent_id, []))

    def recent(self, agent_id: str, limit: int = 10) -> List[dict]:
        return [ev.to_dict() for ev in list(self._store.get(agent_id, []))[-limit:]]

    def clear(self, agent_id: Optional[str] = None) -> int:
        """Clear behavioral history. If agent_id is given, clear only that
        agent; otherwise clear everything. Returns the number of events
        deleted. Used by the ablation harness to reset state between
        configurations — production callers should NOT use this for
        anything other than test orchestration."""
        if agent_id is None:
            n = sum(len(d) for d in self._store.values())
            self._store.clear()
            return n
        if agent_id not in self._store:
            return 0
        n = len(self._store[agent_id])
        del self._store[agent_id]
        return n


HISTORY = History()


# =============================================================================
# Audit emission — federated control plane peer
# =============================================================================
#
# When the LLM-layer detector fires, the scorer announces it to the audit
# logger so the behavior PDP can correlate cross-request patterns (R2 in
# the behavior PDP's rule set fires on N injection events in a window).
#
# Emission is fire-and-forget: a slow or unreachable audit logger MUST NOT
# block the scoring decision, because the sidecar is waiting for our
# response on the request critical path. We give it 200ms and walk away.

_audit_client: Optional["httpx.AsyncClient"] = None  # lazy init


async def _emit_audit_injection(agent_id: str, target: str, mass: float,
                                hits: List[dict], path: str) -> None:
    """Emit a trust.injection event to the audit logger. Best-effort."""
    if not AUDIT_LOGGER_URL or httpx is None:
        return
    global _audit_client
    if _audit_client is None:
        _audit_client = httpx.AsyncClient(timeout=httpx.Timeout(0.2))
    try:
        await _audit_client.post(
            f"{AUDIT_LOGGER_URL.rstrip('/')}/events",
            json={
                "event_type": "trust.injection",
                "source":     "trust-scorer",
                "agent_id":   agent_id,
                "target":     target,
                "severity":   round(min(1.0, mass), 3),
                "data":       {
                    "injection_mass": round(mass, 3),
                    "hits":           [h.get("pattern") for h in hits],
                    "path":           path,
                },
            },
        )
    except Exception as e:
        logger.warning("audit emit (trust.injection) failed: %s", e)


# =============================================================================
# Band decision (unchanged — Kim & Lee 2025 thresholds + Bicakci 2024 sensitivity)
# =============================================================================

def band_for(score: float, path: str) -> str:
    sens = 1.0
    matched = None
    for prefix, s in PATH_SENSITIVITY.items():
        if path == prefix or path.startswith(prefix):
            if matched is None or len(prefix) > len(matched):
                matched = prefix
                sens = s
    if sens == 0.0:
        return "allow"
    block_t = BLOCK_THRESHOLD * sens
    allow_t = ALLOW_THRESHOLD * sens
    if score >= allow_t:
        return "allow"
    if score >= block_t:
        return "step_up"
    return "block"


# =============================================================================
# Request/Response models — unchanged contract
# =============================================================================

class ScoreRequest(BaseModel):
    agent_id: str = Field(..., description="Declared source agent id")
    target_component: str
    path: str
    method: str = "POST"
    jwt_token: Optional[str] = None
    payload_text: Optional[str] = None


class ScoreResponse(BaseModel):
    score: float
    band: str
    allow: bool
    components: dict
    weights: dict          # retained for contract compat; explains the aggregation
    thresholds: dict
    timestamp: str


class FeedbackRequest(BaseModel):
    agent_id: str
    severity: float = Field(..., ge=0.0, le=1.0)
    reason: str


# =============================================================================
# FastAPI app
# =============================================================================

app = FastAPI(
    title="ZTA Trust Scorer (Subjective Logic)",
    description="Continuous trust evaluation for multi-agent ZTA control plane",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest):
    # 1. Produce one opinion per evidence source
    op_I, d_I = opinion_identity(req.agent_id, req.jwt_token,
                                 req.target_component)
    op_B, d_B = opinion_behavioral(req.agent_id, HISTORY)
    op_L, d_L = opinion_llm_layer(req.payload_text)

    # 2. Fuse opinions via cumulative belief fusion (Jøsang 2016 §12.3).
    #    Order-independent; no tunable weights.
    fused = fuse_all([op_I, op_B, op_L])
    projection = fused.projection()          # in [0,1]
    score_val = round(100.0 * projection, 2) # map to [0,100] to preserve contract

    b = band_for(score_val, req.path)

    # 3. Hard-floor rules — these are correctness invariants, not soft scoring.
    #    They are preserved because removing them would let the non-linear
    #    aggregator accidentally "rescue" catastrophic signals.
    hard_block_reason = None
    gated = False
    for prefix, s in PATH_SENSITIVITY.items():
        if (req.path == prefix or req.path.startswith(prefix)) and s > 0:
            gated = True
            break

    if gated and d_L.get("injection_mass", 0) >= 0.7:
        b = "block"
        hard_block_reason = f"llm-injection:{[h['pattern'] for h in d_L['hits']]}"

    if gated and (op_I.d >= 0.99):  # certain_false identity
        b = "block"
        hard_block_reason = hard_block_reason or f"identity-failure:{d_I.get('error', 'no-identity')}"

    # 4. Record high-confidence injection into behavioral history
    if d_L.get("injection_mass", 0) >= 0.7:
        HISTORY.record(req.agent_id, severity=min(1.0, d_L["injection_mass"]),
                       reason=f"llm-injection:{[h['pattern'] for h in d_L['hits']]}")
        # Federated peer: notify audit logger so behavior PDP sees the event
        await _emit_audit_injection(
            agent_id=req.agent_id,
            target=req.target_component,
            mass=d_L["injection_mass"],
            hits=d_L.get("hits", []),
            path=req.path,
        )

    logger.info(
        "score|agent=%s|target=%s|path=%s|score=%.2f|band=%s|"
        "p_I=%.2f|p_B=%.2f|p_L=%.2f|u_fused=%.2f%s",
        req.agent_id, req.target_component, req.path, score_val, b,
        op_I.projection(), op_B.projection(), op_L.projection(),
        fused.u,
        f"|hard_block={hard_block_reason}" if hard_block_reason else "",
    )

    return ScoreResponse(
        score=score_val,
        band=b,
        allow=(b != "block"),
        components={
            "identity":    {"opinion": op_I.to_dict(), "detail": d_I},
            "behavioral":  {"opinion": op_B.to_dict(), "detail": d_B},
            "llm_layer":   {"opinion": op_L.to_dict(), "detail": d_L},
            "fused":       fused.to_dict(),
        },
        weights={
            "aggregator":  "subjective-logic-cumulative-fusion",
            "reference":   "Jøsang 2016; Bradatsch et al. 2024 (arXiv:2402.08299)",
            "note":        "No scalar weights: opinions are combined algebraically.",
        },
        thresholds={
            "allow": ALLOW_THRESHOLD,
            "block": BLOCK_THRESHOLD,
            "decay_half_life_s": DECAY_HALF_LIFE_S,
            "base_rate_a": BASE_RATE,
        },
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/feedback")
async def feedback(req: FeedbackRequest):
    HISTORY.record(req.agent_id, req.severity, req.reason)
    return {
        "ok": True,
        "agent_id": req.agent_id,
        "decayed_penalty_after": round(HISTORY.decayed_penalty(req.agent_id), 3),
    }


@app.get("/agents/{agent_id}/history")
async def agent_history(agent_id: str, limit: int = 20):
    return {
        "agent_id": agent_id,
        "decayed_penalty": round(HISTORY.decayed_penalty(agent_id), 3),
        "events": HISTORY.recent(agent_id, limit=limit),
    }


@app.delete("/admin/history")
async def admin_clear_history(agent_id: Optional[str] = None):
    """TEST ONLY: clear behavioral history.
    Use ?agent_id=<id> for a single agent, or omit for all agents.
    The ablation harness calls this between configurations to prevent
    earlier injection events from biasing later trials."""
    n = HISTORY.clear(agent_id)
    logger.info("admin|clear_history|agent=%s|deleted=%d", agent_id or "*", n)
    return {"ok": True, "agent_id": agent_id, "deleted": n}


@app.get("/health")
async def health():
    return {
        "status":  "healthy",
        "service": "zta-trust-scorer",
        "version": "2.1.0",
        "aggregator": "subjective-logic-cumulative-fusion",
        "thresholds": {
            "allow": ALLOW_THRESHOLD,
            "block": BLOCK_THRESHOLD,
            "decay_half_life_s": DECAY_HALF_LIFE_S,
            "base_rate_a": BASE_RATE,
        },
        "citations": {
            "subjective_logic":  "Jøsang 2016; Bradatsch et al. 2024 (arXiv:2402.08299)",
            "threshold_bands":   "Kim & Lee 2025, MDPI Applied Sciences 15(17):9551",
            "exponential_decay": "Sun et al. 2025, IEEE JSAC 43(6):2089",
            "llm_layer_signal":  "He et al. 2025 (arXiv:2506.02546); Liu et al. 2025 (arXiv:2508.19870)",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
