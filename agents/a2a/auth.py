"""
A2A workload-identity helper
=============================
Each agent process is a "workload" in the SPIFFE sense: it has a stable
identity (its agent_id), is provisioned with a shared secret, and trades
that secret for short-lived JWTs at the auth gateway.

This module is a thin client over the existing ZTA auth service so every
agent can:

    1. Acquire a token at startup
    2. Cache it with a TTL-aware refresh
    3. Hand back valid Bearer headers to anything that needs to call out
       (other A2A peers, MCP servers, etc.)

Why a separate module:
    - The A2AClient already has acquire_token logic, but it's bound to a
      single A2AClient instance. Worker agents need the same identity for
      *outbound MCP calls*, which don't go through A2AClient.
    - SPIFFE delegation pattern: caller's token authenticates inbound;
      our own token authenticates outbound. Mixing them up is the most
      common implementation bug in a service mesh.

Reference:
    SPIFFE/SPIRE Workload API specification, §4 (Workload Identity).
    NIST SP 800-207 §2.1 Tenet 6 (dynamic per-request authn).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("a2a.auth")


class WorkloadIdentity:
    """
    Holds an agent's JWT, refreshes it before expiry, exposes Bearer headers.

    Usage:
        identity = WorkloadIdentity(
            agent_id="airline-agent",
            auth_url="http://zta-auth:8180",
            shared_secret="zta-agent-shared-secret",
        )
        await identity.bootstrap()           # acquire first token
        headers = await identity.headers()   # always returns a fresh Bearer
    """

    # Refresh once we're within REFRESH_LEEWAY_S of the token's exp.  The
    # auth service issues tokens with a 24h lifetime by default, so a 60s
    # leeway is plenty.  We also refresh proactively on any 401 from a peer.
    REFRESH_LEEWAY_S = 60.0

    def __init__(
        self,
        agent_id: str,
        auth_url: str,
        shared_secret: str,
        timeout_s: float = 5.0,
    ):
        self.agent_id = agent_id
        self.auth_url = auth_url.rstrip("/")
        self.shared_secret = shared_secret
        self.timeout_s = timeout_s

        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None

    # =========================================================================
    # Public API
    # =========================================================================

    async def bootstrap(self) -> bool:
        """Acquire the first token. Returns True on success.
        Failure is logged but not raised; the caller can decide whether to
        proceed without a token (e.g. in a permissive testbed)."""
        try:
            await self._refresh()
            return self._token is not None
        except Exception as e:
            logger.warning("workload-identity bootstrap failed for %s: %s",
                           self.agent_id, e)
            return False

    async def headers(self) -> dict[str, str]:
        """Return identity-bearing headers ready to attach to an outbound
        request. If no token has been acquired or the current one is near
        expiry, this triggers a synchronous refresh.

        On refresh failure, returns headers with only `x-agent-id` set —
        callers should treat this as "no JWT available" and let the
        downstream sidecar deny the request with a useful error rather
        than have us raise here. Raising would leak auth-service outages
        into every outbound RPC, which is exactly what we want to avoid.
        """
        async with self._lock:
            if self._needs_refresh():
                try:
                    await self._refresh()
                except Exception as e:
                    logger.warning("workload-identity refresh failed for %s: %s",
                                   self.agent_id, e)
                    self._token = None
                    self._expires_at = 0.0
        if not self._token:
            return {"x-agent-id": self.agent_id}
        return {
            "x-agent-id": self.agent_id,
            "Authorization": f"Bearer {self._token}",
        }

    async def force_refresh(self) -> None:
        """Drop the cached token and acquire a new one. Call this on a 401
        from a peer (defensive — exp-aware refresh handles the common case).
        On failure, falls back to no-token state."""
        async with self._lock:
            self._token = None
            self._expires_at = 0.0
            try:
                await self._refresh()
            except Exception as e:
                logger.warning("workload-identity force_refresh failed for %s: %s",
                               self.agent_id, e)

    @property
    def has_token(self) -> bool:
        return self._token is not None and time.time() < self._expires_at

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # =========================================================================
    # Internals
    # =========================================================================

    def _needs_refresh(self) -> bool:
        if self._token is None:
            return True
        return time.time() + self.REFRESH_LEEWAY_S >= self._expires_at

    async def _ensure_http(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_s)

    async def _refresh(self) -> None:
        """Hit POST /token. Caller holds self._lock."""
        await self._ensure_http()
        resp = await self._http.post(
            f"{self.auth_url}/token",
            json={"agent_id": self.agent_id, "secret": self.shared_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # The auth service returns expires_in in seconds; convert to absolute.
        ttl = float(data.get("expires_in", 3600))
        self._expires_at = time.time() + ttl
        logger.info("workload-identity refreshed for %s (ttl=%.0fs)",
                    self.agent_id, ttl)
