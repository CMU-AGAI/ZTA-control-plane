"""
ZTA Audit Logger Service
==========================
Append-only event sink for the federated control plane.  Every sidecar,
PDP, scorer, and dispatcher emits events here; the behavior PDP and
revocation dispatcher both read from it as their primary signal source.

This is the missing service from the proposal:
    "decomposed into separate services for PDP, auth gateway, trust
     scorer, revocation dispatcher, and audit logger."
                — Independent Study Proposal (Spring 2026), §1

Design choices:
  - SQLite WAL mode: durable, queryable with SQL, single-file deploy.
    Replaceable with Postgres or a real event store (ClickHouse / Loki)
    without changing the wire contract.
  - Append-only: events are never updated or deleted by the API.
    Retention is handled out-of-band (cron / volume mount rotation).
  - Schema-on-read: the `data` column is JSON.  Each event_type defines
    its own contract; new types don't require schema migrations.
  - No auth on /events POST (yet).  In the testbed, only sidecars on
    the zta-network can reach this port.  Production deployment would
    front this with the auth gateway.

References:
    NIST SP 800-207 §3.4.1 — "the enterprise should collect as much
    information as possible about the current state of network traffic
    [and] use it to improve policy creation and enforcement."
    OWASP LLM Top-10 (2025) — LLM08 Excessive Agency mitigation:
    audit every action taken by an agent on behalf of a user.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =============================================================================
# Config
# =============================================================================

PORT       = int(os.getenv("PORT", "8195"))
DB_PATH    = os.getenv("AUDIT_DB_PATH", "/data/audit.db")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("zta-audit-logger")

# =============================================================================
# Storage — SQLite WAL with a single writer thread
# =============================================================================
#
# We serialize writes through a lock because SQLite WAL handles concurrent
# readers well but only a single writer.  For testbed traffic this is fine;
# typical sidecar emission rate is well under 1k events/sec.

_db_lock = threading.Lock()


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL is durable enough
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,           -- unix epoch seconds
                event_type  TEXT    NOT NULL,
                source      TEXT    NOT NULL,           -- emitter (sidecar name, pdp name)
                agent_id    TEXT,                       -- subject of the event, if any
                target      TEXT,                       -- target component, if any
                severity    REAL,                       -- in [0, 1] for evaluable events
                data        TEXT    NOT NULL            -- json blob
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent_ts ON events(agent_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts  ON events(event_type, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts)")
        conn.commit()


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


# =============================================================================
# Event types
# =============================================================================
# Defined as a closed set so the behavior PDP can rely on stable categories.
# New event types should be added here with a one-line semantics comment.

KNOWN_EVENT_TYPES = {
    # Sidecar pipeline outcomes
    "request.allowed",        # full pipeline passed; request forwarded upstream
    "request.denied",         # any middleware blocked the request
    # Trust scorer outputs
    "trust.score",            # composite score recorded for an (agent, target, path)
    "trust.injection",        # high-confidence prompt-injection detected
    # OPA / authz PDP
    "policy.deny",            # OPA returned allow=false
    # WAF
    "waf.block",              # WAF rule fired (sqli, xss, rate-limit)
    # Micro-segmentation
    "microseg.deny",          # source agent not in ALLOWED_SOURCES
    # JWT / auth
    "auth.fail",              # invalid / expired / mismatched token
    "auth.revoked_use",       # caller presented a revoked jti or quarantined agent
    # Revocation dispatcher
    "revocation.issued",      # a jti or agent_id was added to the deny list
    # Behavior PDP
    "behavior.deny",          # behavior PDP returned block
    "behavior.step_up",       # behavior PDP returned step_up
    # DLP
    "dlp.hit",                # response body matched a sensitive-data pattern
}


# =============================================================================
# API models
# =============================================================================

class EventIn(BaseModel):
    event_type: str = Field(..., description="One of KNOWN_EVENT_TYPES")
    source: str     = Field(..., description="Emitter id (e.g. 'airline-agent-sidecar')")
    agent_id: Optional[str] = None
    target: Optional[str]   = None
    severity: Optional[float] = Field(None, ge=0.0, le=1.0)
    data: dict      = Field(default_factory=dict)
    # Optional client-supplied timestamp (unix seconds).  We accept it so the
    # sidecar's audit time matches its decision time, not the moment we
    # happen to receive the POST.  If absent, we stamp it server-side.
    ts: Optional[float] = None


class EventOut(BaseModel):
    id: int
    ts: float
    iso: str
    event_type: str
    source: str
    agent_id: Optional[str]
    target: Optional[str]
    severity: Optional[float]
    data: dict


class AgentSummary(BaseModel):
    agent_id: str
    window_seconds: int
    total_events: int
    by_type: dict
    deny_count: int
    last_event_ts: Optional[float]


# =============================================================================
# FastAPI app
# =============================================================================

app = FastAPI(
    title="ZTA Audit Logger",
    description="Append-only event sink for the federated ZTA control plane",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    _init_db()
    logger.info("audit-logger ready: db=%s retention=%dd", DB_PATH, RETENTION_DAYS)


@app.post("/events", status_code=201)
def post_event(ev: EventIn):
    if ev.event_type not in KNOWN_EVENT_TYPES:
        # We accept it but warn — better to capture an unknown event than drop it.
        logger.warning("unknown event_type=%s from source=%s", ev.event_type, ev.source)
    ts = ev.ts if ev.ts is not None else time.time()
    payload = json.dumps(ev.data, default=str)
    with _db_lock, _db() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, event_type, source, agent_id, target, severity, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, ev.event_type, ev.source, ev.agent_id, ev.target, ev.severity, payload),
        )
        event_id = cur.lastrowid
    return {"id": event_id, "ts": ts}


@app.post("/events/batch", status_code=201)
def post_events_batch(events: list[EventIn]):
    """Bulk ingestion path — sidecars can buffer and flush on a timer."""
    if not events:
        return {"ingested": 0}
    rows = []
    for ev in events:
        ts = ev.ts if ev.ts is not None else time.time()
        rows.append((ts, ev.event_type, ev.source, ev.agent_id, ev.target,
                     ev.severity, json.dumps(ev.data, default=str)))
    with _db_lock, _db() as conn:
        conn.executemany(
            "INSERT INTO events (ts, event_type, source, agent_id, target, severity, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return {"ingested": len(rows)}


@app.get("/events", response_model=list[EventOut])
def list_events(
    agent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    since: Optional[float] = Query(None, description="Unix epoch seconds; events with ts >= since"),
    limit: int = Query(100, ge=1, le=10000),
):
    """Query events. Used by the behavior PDP and by operators for forensics."""
    sql = "SELECT id, ts, event_type, source, agent_id, target, severity, data FROM events WHERE 1=1"
    params: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id = ?"; params.append(agent_id)
    if event_type is not None:
        sql += " AND event_type = ?"; params.append(event_type)
    if source is not None:
        sql += " AND source = ?"; params.append(source)
    if since is not None:
        sql += " AND ts >= ?"; params.append(since)
    sql += " ORDER BY ts DESC LIMIT ?"; params.append(limit)

    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()

    out = []
    for r in rows:
        out.append(EventOut(
            id=r[0], ts=r[1],
            iso=datetime.fromtimestamp(r[1], tz=timezone.utc).isoformat(),
            event_type=r[2], source=r[3], agent_id=r[4], target=r[5],
            severity=r[6], data=json.loads(r[7]) if r[7] else {},
        ))
    return out


@app.get("/agents/{agent_id}/summary", response_model=AgentSummary)
def agent_summary(
    agent_id: str,
    window_seconds: int = Query(3600, ge=1, le=86400 * 7),
):
    """Behavior-PDP-friendly aggregation: counts per event_type within a window."""
    cutoff = time.time() - window_seconds
    with _db() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) FROM events "
            "WHERE agent_id = ? AND ts >= ? GROUP BY event_type",
            (agent_id, cutoff),
        ).fetchall()
        last_ts_row = conn.execute(
            "SELECT MAX(ts) FROM events WHERE agent_id = ?", (agent_id,)
        ).fetchone()

    by_type = {r[0]: r[1] for r in rows}
    deny_types = {"request.denied", "policy.deny", "waf.block",
                  "microseg.deny", "auth.fail", "behavior.deny",
                  "trust.injection", "dlp.hit"}
    deny_count = sum(c for t, c in by_type.items() if t in deny_types)
    total = sum(by_type.values())

    return AgentSummary(
        agent_id=agent_id, window_seconds=window_seconds,
        total_events=total, by_type=by_type, deny_count=deny_count,
        last_event_ts=last_ts_row[0] if last_ts_row else None,
    )


@app.delete("/admin/events")
def admin_delete_events(agent_id: Optional[str] = None,
                        event_type: Optional[str] = None):
    """TEST ONLY: delete events. Filter by agent_id and/or event_type.
    The ablation harness uses this between configurations to prevent
    earlier-config injection events from biasing the behavior PDP's
    R2 rule (which queries audit, not trust scorer history)."""
    sql = "DELETE FROM events WHERE 1=1"
    params: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id = ?"; params.append(agent_id)
    if event_type is not None:
        sql += " AND event_type = ?"; params.append(event_type)
    with _db_lock, _db() as conn:
        cur = conn.execute(sql, params)
        deleted = cur.rowcount
    logger.info("admin|delete|agent=%s|type=%s|deleted=%d",
                agent_id or "*", event_type or "*", deleted)
    return {"ok": True, "agent_id": agent_id, "event_type": event_type,
            "deleted": deleted}


@app.get("/health")
def health():
    try:
        with _db() as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return {"status": "healthy", "service": "zta-audit-logger",
                "version": "1.0.0", "events_total": count,
                "db_path": DB_PATH,
                "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unhealthy: {e}")


@app.get("/stats")
def stats():
    """Operational: counts by event_type over the last hour, day, and all-time."""
    now = time.time()
    out = {}
    with _db() as conn:
        for label, secs in (("last_hour", 3600), ("last_day", 86400), ("all_time", None)):
            if secs is None:
                rows = conn.execute(
                    "SELECT event_type, COUNT(*) FROM events GROUP BY event_type"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event_type, COUNT(*) FROM events WHERE ts >= ? GROUP BY event_type",
                    (now - secs,),
                ).fetchall()
            out[label] = {r[0]: r[1] for r in rows}
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
