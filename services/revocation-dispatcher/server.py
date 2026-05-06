"""
ZTA Revocation Dispatcher
==========================
Maintains two deny lists for the federated control plane:

  1. JTI revocation list — specific JWT IDs that must not be honored
     (token leak / explicit logout / detected replay).
  2. Agent quarantine list — agent identities temporarily barred from
     all access (after behavior PDP determines an agent is compromised
     or after operator action).

Both lists support TTL.  An entry expires automatically; the dispatcher
does not need a sweeper because checks evaluate the TTL at read time.

Why a separate service:
  - The trust scorer does *gradual* trust degradation (decayed penalty).
    Revocation is a *binary* hard stop.  Mixing them inside the scorer
    confuses two different control surfaces.
  - The auth gateway issues tokens; revocation invalidates issued ones.
    Keeping these on separate services means a compromised auth gateway
    doesn't auto-grant amnesty to revoked tokens.
  - This was an explicit deliverable in the proposal:
        "decomposed into separate services for PDP, auth gateway,
         trust scorer, revocation dispatcher, and audit logger."

Wire contract (intentionally tiny — sidecars hit it on every request):
  GET  /check/jti/{jti}           -> {"revoked": bool, "reason": ...}
  GET  /check/agent/{agent_id}    -> {"quarantined": bool, "reason": ...}
  GET  /list?since_version=N      -> incremental snapshot for caching
  POST /revoke/jti                -> add a jti to the deny list
  POST /quarantine/agent          -> quarantine an agent (with TTL)
  POST /unquarantine/agent/{id}   -> operator override

All write paths emit an event to the audit logger.  Read paths are
deliberately log-quiet to avoid feedback storms.

References:
    NIST SP 800-207 §2.1 — Tenet 6: "all resource authentication and
    authorization are dynamic and strictly enforced before access is
    allowed."  Revocation is the dynamic-deauthorization control.
    OAuth 2.0 Token Revocation, RFC 7009.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =============================================================================
# Config
# =============================================================================

PORT             = int(os.getenv("PORT", "8196"))
AUDIT_URL        = os.getenv("AUDIT_LOGGER_URL", "http://audit-logger:8195")
DEFAULT_TTL_S    = int(os.getenv("DEFAULT_REVOCATION_TTL_S", "86400"))   # 24h
QUARANTINE_TTL_S = int(os.getenv("DEFAULT_QUARANTINE_TTL_S", "3600"))    # 1h
SERVICE_NAME     = "revocation-dispatcher"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("zta-revocation")


# =============================================================================
# In-memory store with monotonic version counter
# =============================================================================
#
# The version counter lets sidecars cache the deny list and pull only deltas.
# It increments on every mutation (revoke / quarantine / unquarantine /
# expiry-on-read).  Sidecars hold a (snapshot, version) pair; calling
# /list?since_version=V returns entries added or removed after V.
#
# An expiring entry counts as a removal — but we don't bump the version
# when it expires "silently" on read, only on explicit operator action.
# This is a deliberate trade-off: it means a sidecar with a stale snapshot
# might briefly think a revocation is still in force after its natural
# expiry.  That's fail-safe for a deny-list (better to deny one extra
# request than let a revoked token through), and it avoids needing a
# sweeper thread that bumps version on every tick.

class _Entry:
    __slots__ = ("subject", "reason", "added_ts", "expires_ts", "added_by")
    def __init__(self, subject: str, reason: str, expires_ts: float, added_by: str):
        self.subject    = subject
        self.reason     = reason
        self.added_ts   = time.time()
        self.expires_ts = expires_ts
        self.added_by   = added_by

    def is_active(self, now: float) -> bool:
        return now < self.expires_ts

    def to_dict(self) -> dict:
        return {
            "subject":    self.subject,
            "reason":     self.reason,
            "added_ts":   self.added_ts,
            "added_iso":  datetime.fromtimestamp(self.added_ts, tz=timezone.utc).isoformat(),
            "expires_ts": self.expires_ts,
            "expires_iso": datetime.fromtimestamp(self.expires_ts, tz=timezone.utc).isoformat(),
            "added_by":   self.added_by,
        }


class _Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._jti: dict[str, _Entry] = {}
        self._agents: dict[str, _Entry] = {}
        self._version: int = 0

    @property
    def version(self) -> int:
        return self._version

    def revoke_jti(self, jti: str, reason: str, ttl_s: int, added_by: str) -> _Entry:
        with self._lock:
            entry = _Entry(jti, reason, time.time() + ttl_s, added_by)
            self._jti[jti] = entry
            self._version += 1
            return entry

    def quarantine_agent(self, agent_id: str, reason: str, ttl_s: int, added_by: str) -> _Entry:
        with self._lock:
            entry = _Entry(agent_id, reason, time.time() + ttl_s, added_by)
            self._agents[agent_id] = entry
            self._version += 1
            return entry

    def unquarantine(self, agent_id: str) -> bool:
        with self._lock:
            removed = self._agents.pop(agent_id, None)
            if removed is not None:
                self._version += 1
                return True
            return False

    def check_jti(self, jti: str) -> Optional[_Entry]:
        now = time.time()
        with self._lock:
            entry = self._jti.get(jti)
            if entry is None:
                return None
            if not entry.is_active(now):
                # Lazy expiry — silent (no version bump).
                self._jti.pop(jti, None)
                return None
            return entry

    def check_agent(self, agent_id: str) -> Optional[_Entry]:
        now = time.time()
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry is None:
                return None
            if not entry.is_active(now):
                self._agents.pop(agent_id, None)
                return None
            return entry

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            return {
                "version": self._version,
                "jti":    [e.to_dict() for e in self._jti.values()    if e.is_active(now)],
                "agents": [e.to_dict() for e in self._agents.values() if e.is_active(now)],
            }


STORE = _Store()


# =============================================================================
# Audit emission (best-effort, non-blocking failure mode)
# =============================================================================

_audit_client = httpx.Client(timeout=httpx.Timeout(0.5))

def _emit_audit(event_type: str, agent_id: Optional[str], data: dict,
                severity: Optional[float] = None) -> None:
    """Fire-and-forget audit emission. Never blocks a revocation operation."""
    try:
        _audit_client.post(f"{AUDIT_URL}/events", json={
            "event_type": event_type, "source": SERVICE_NAME,
            "agent_id": agent_id, "severity": severity, "data": data,
        })
    except Exception as e:
        logger.warning("audit emit failed: %s", e)


# =============================================================================
# API models
# =============================================================================

class RevokeJtiRequest(BaseModel):
    jti: str
    reason: str = "operator-revoked"
    ttl_seconds: Optional[int] = Field(None, ge=1, le=86400 * 365)
    added_by: str = "operator"


class QuarantineAgentRequest(BaseModel):
    agent_id: str
    reason: str = "operator-quarantine"
    ttl_seconds: Optional[int] = Field(None, ge=1, le=86400 * 365)
    added_by: str = "operator"


class CheckResponse(BaseModel):
    revoked: bool
    reason: Optional[str] = None
    expires_ts: Optional[float] = None


# =============================================================================
# FastAPI app
# =============================================================================

app = FastAPI(
    title="ZTA Revocation Dispatcher",
    description="JTI and agent revocation lists for the federated ZTA control plane",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/revoke/jti")
def revoke_jti(req: RevokeJtiRequest):
    ttl = req.ttl_seconds if req.ttl_seconds is not None else DEFAULT_TTL_S
    entry = STORE.revoke_jti(req.jti, req.reason, ttl, req.added_by)
    logger.info("revoke|jti=%s|reason=%s|ttl=%ds", req.jti, req.reason, ttl)
    _emit_audit("revocation.issued", agent_id=None, data={
        "subject_type": "jti", "subject": req.jti,
        "reason": req.reason, "ttl_seconds": ttl, "added_by": req.added_by,
    }, severity=0.9)
    return {"ok": True, "version": STORE.version, "entry": entry.to_dict()}


@app.post("/quarantine/agent")
def quarantine_agent(req: QuarantineAgentRequest):
    ttl = req.ttl_seconds if req.ttl_seconds is not None else QUARANTINE_TTL_S
    entry = STORE.quarantine_agent(req.agent_id, req.reason, ttl, req.added_by)
    logger.info("quarantine|agent=%s|reason=%s|ttl=%ds", req.agent_id, req.reason, ttl)
    _emit_audit("revocation.issued", agent_id=req.agent_id, data={
        "subject_type": "agent", "subject": req.agent_id,
        "reason": req.reason, "ttl_seconds": ttl, "added_by": req.added_by,
    }, severity=1.0)
    return {"ok": True, "version": STORE.version, "entry": entry.to_dict()}


@app.post("/unquarantine/agent/{agent_id}")
def unquarantine(agent_id: str, reason: str = "operator-cleared"):
    removed = STORE.unquarantine(agent_id)
    if not removed:
        raise HTTPException(status_code=404, detail="agent not in quarantine")
    logger.info("unquarantine|agent=%s|reason=%s", agent_id, reason)
    _emit_audit("revocation.issued", agent_id=agent_id, data={
        "subject_type": "agent_unquarantine", "subject": agent_id,
        "reason": reason,
    })
    return {"ok": True, "version": STORE.version}


@app.get("/check/jti/{jti}", response_model=CheckResponse)
def check_jti(jti: str):
    entry = STORE.check_jti(jti)
    if entry is None:
        return CheckResponse(revoked=False)
    return CheckResponse(revoked=True, reason=entry.reason, expires_ts=entry.expires_ts)


@app.get("/check/agent/{agent_id}", response_model=CheckResponse)
def check_agent(agent_id: str):
    entry = STORE.check_agent(agent_id)
    if entry is None:
        return CheckResponse(revoked=False)
    return CheckResponse(revoked=True, reason=entry.reason, expires_ts=entry.expires_ts)


@app.get("/list")
def list_all(since_version: Optional[int] = None):
    """
    Snapshot of active revocations.  `since_version` is reserved for a future
    delta protocol; today we always return the full active set, plus the
    current version so the caller can tell whether their cache is stale.
    """
    snap = STORE.snapshot()
    snap["since_version_requested"] = since_version
    snap["timestamp"] = datetime.now(timezone.utc).isoformat()
    return snap


@app.get("/health")
def health():
    snap = STORE.snapshot()
    return {
        "status": "healthy", "service": SERVICE_NAME, "version": "1.0.0",
        "list_version": snap["version"],
        "active_jti_count": len(snap["jti"]),
        "active_agent_count": len(snap["agents"]),
        "audit_url": AUDIT_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
