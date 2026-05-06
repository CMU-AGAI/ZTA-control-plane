"""
ZTA Auth Service — JWT Token Issuer
=====================================
Issues JWT tokens for registered agents in the multi-agent system.

Endpoints:
  POST /token          — Issue a JWT for an agent (requires agent_id + secret)
  POST /token/verify   — Verify a JWT and return claims
  GET  /health         — Health check
  GET  /.well-known/jwks.json — Public key (for asymmetric; returns signing info for symmetric)

Each agent calls POST /token at startup with its agent_id and a shared secret.
The returned JWT contains: agent_id, agent_type, allowed_targets, exp, iss, aud.
Sidecars validate the JWT on every request.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import jwt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =============================================================================
# Configuration
# =============================================================================

JWT_SECRET = os.getenv("JWT_SECRET", "zta-dev-secret-key-change-in-production-min-32-chars!!")
JWT_ISSUER = os.getenv("JWT_ISSUER", "zta-auth-server")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "zta-agents")
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
AGENT_SECRET = os.getenv("AGENT_SECRET", "zta-agent-shared-secret")
PORT = int(os.getenv("PORT", "8180"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("zta-auth")

# =============================================================================
# Agent Registry — defines which agents can get tokens and their permissions
# =============================================================================

AGENT_REGISTRY = {
    "supervisor-agent": {
        "type": "supervisor",
        "allowed_targets": ["airline-agent", "hotel-agent", "car-rental-agent"],
        "roles": ["supervisor", "orchestrator"],
    },
    "airline-agent": {
        "type": "worker",
        "domain": "airline",
        "allowed_targets": ["airline-mcp"],
        "roles": ["worker"],
    },
    "hotel-agent": {
        "type": "worker",
        "domain": "hotel",
        "allowed_targets": ["hotel-mcp"],
        "roles": ["worker"],
    },
    "car-rental-agent": {
        "type": "worker",
        "domain": "car-rental",
        "allowed_targets": ["car-rental-mcp"],
        "roles": ["worker"],
    },
}

# =============================================================================
# Models
# =============================================================================

class TokenRequest(BaseModel):
    agent_id: str
    secret: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    agent_id: str
    agent_type: str

class VerifyRequest(BaseModel):
    token: str

class VerifyResponse(BaseModel):
    valid: bool
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    allowed_targets: Optional[List[str]] = None
    expires_at: Optional[str] = None
    error: Optional[str] = None

# =============================================================================
# Token helpers
# =============================================================================

def create_token(agent_id: str) -> tuple[str, int]:
    """Create a JWT for the given agent."""
    agent_info = AGENT_REGISTRY[agent_id]
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=JWT_EXPIRY_HOURS)
    expires_in = int(JWT_EXPIRY_HOURS * 3600)

    payload = {
        "sub": agent_id,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": expiry,
        "agent_id": agent_id,
        "agent_type": agent_info["type"],
        "allowed_targets": agent_info["allowed_targets"],
        "roles": agent_info["roles"],
    }

    if "domain" in agent_info:
        payload["domain"] = agent_info["domain"]

    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return token, expires_in


def verify_token(token: str) -> dict:
    """Verify and decode a JWT."""
    return jwt.decode(
        token,
        JWT_SECRET,
        algorithms=["HS256"],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
    )

# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(title="ZTA Auth Service", description="JWT Token Issuer for ZTA Multi-Agent System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/token", response_model=TokenResponse)
async def issue_token(request: TokenRequest):
    """Issue a JWT token for a registered agent."""
    # Verify agent is registered
    if request.agent_id not in AGENT_REGISTRY:
        logger.warning(f"Token request for unknown agent: {request.agent_id}")
        raise HTTPException(status_code=404, detail=f"Agent '{request.agent_id}' not registered")

    # Verify shared secret
    if request.secret != AGENT_SECRET:
        logger.warning(f"Invalid secret for agent: {request.agent_id}")
        raise HTTPException(status_code=401, detail="Invalid secret")

    # Issue token
    token, expires_in = create_token(request.agent_id)
    agent_info = AGENT_REGISTRY[request.agent_id]

    logger.info(f"Token issued for {request.agent_id} (type={agent_info['type']}, expires_in={expires_in}s)")

    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        agent_id=request.agent_id,
        agent_type=agent_info["type"],
    )


@app.post("/token/verify", response_model=VerifyResponse)
async def verify(request: VerifyRequest):
    """Verify a JWT token and return claims."""
    try:
        claims = verify_token(request.token)
        return VerifyResponse(
            valid=True,
            agent_id=claims.get("agent_id"),
            agent_type=claims.get("agent_type"),
            allowed_targets=claims.get("allowed_targets"),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc).isoformat(),
        )
    except jwt.ExpiredSignatureError:
        return VerifyResponse(valid=False, error="Token expired")
    except jwt.InvalidTokenError as e:
        return VerifyResponse(valid=False, error=str(e))


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "zta-auth-server",
        "registered_agents": list(AGENT_REGISTRY.keys()),
        "jwt_issuer": JWT_ISSUER,
        "jwt_expiry_hours": JWT_EXPIRY_HOURS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/.well-known/jwks.json")
async def jwks():
    """Return signing key info (for symmetric HS256, just metadata)."""
    return {
        "keys": [{
            "kty": "oct",
            "alg": "HS256",
            "use": "sig",
            "kid": "zta-signing-key-1",
        }],
        "issuer": JWT_ISSUER,
        "audience": JWT_AUDIENCE,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)