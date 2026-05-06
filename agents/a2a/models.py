"""
A2A Protocol Models
====================
Pydantic v2 models implementing the Agent2Agent Protocol specification.
Reference: https://a2aprotocol.org/latest/specification/

Key types:
- AgentCard: Capability advertisement (served at /.well-known/agent.json)
- Task: Unit of work with lifecycle states
- Message: Container for conversation turns (role = "user" | "agent")
- Part: Content block inside a Message (text, data, or file)
- Artifact: Output produced by a task
- JSON-RPC 2.0: Transport envelope
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# =============================================================================
# Agent Card — Capability Discovery
# =============================================================================

class AgentProvider(BaseModel):
    """Organization that hosts the agent."""
    organization: str
    url: Optional[str] = None


class AgentAuthentication(BaseModel):
    """Authentication requirements for the agent.
    
    The A2A spec defines authentication schemes the caller must use.
    For ZTA, we extend with optional fields for JWT/mTLS.
    """
    schemes: List[str] = Field(
        default_factory=lambda: ["none"],
        description="Supported auth schemes: 'none', 'bearer', 'oauth2', 'mtls'"
    )
    credentials: Optional[str] = Field(
        default=None,
        description="URL or instructions for obtaining credentials"
    )


class AgentSkill(BaseModel):
    """A specific capability the agent advertises.
    
    Skills map to the domain expertise the agent offers.
    The supervisor uses these to route tasks.
    """
    id: str = Field(..., description="Unique skill identifier")
    name: str = Field(..., description="Human-readable skill name")
    description: str = Field(..., description="What this skill does")
    tags: List[str] = Field(default_factory=list, description="Searchable tags")
    examples: List[str] = Field(
        default_factory=list,
        description="Example natural-language queries this skill handles"
    )


class AgentCapabilities(BaseModel):
    """What the agent can do — feature flags for the A2A server."""
    streaming: bool = Field(default=False, description="Supports SSE streaming")
    pushNotifications: bool = Field(default=False, description="Supports push callbacks")
    stateTransitionHistory: bool = Field(
        default=True,
        description="Task status includes full history"
    )


class AgentCard(BaseModel):
    """Agent Card — the A2A capability advertisement.
    
    Served at GET /.well-known/agent.json
    This is how agents discover each other's capabilities.
    The supervisor fetches Agent Cards to build its registry.
    """
    name: str = Field(..., description="Agent display name")
    description: str = Field(..., description="What the agent does")
    url: str = Field(..., description="Base URL of the A2A endpoint")
    version: str = Field(default="1.0.0", description="Agent version")
    
    # A2A protocol version
    protocolVersion: str = Field(
        default="0.2",
        description="A2A protocol version supported"
    )
    
    # Provider info
    provider: Optional[AgentProvider] = None
    
    # Capabilities
    capabilities: AgentCapabilities = Field(
        default_factory=AgentCapabilities
    )
    
    # Authentication
    authentication: AgentAuthentication = Field(
        default_factory=AgentAuthentication
    )
    
    # Skills
    skills: List[AgentSkill] = Field(
        default_factory=list,
        description="Capabilities this agent advertises"
    )
    
    # ZTA extensions — not in the base A2A spec but needed for our sidecar
    defaultInputModes: List[str] = Field(
        default_factory=lambda: ["text/plain"],
        description="Accepted input content types"
    )
    defaultOutputModes: List[str] = Field(
        default_factory=lambda: ["text/plain"],
        description="Output content types the agent produces"
    )

    # Metadata
    documentationUrl: Optional[str] = None
    supportsAuthenticatedExtendedCard: bool = Field(
        default=False,
        description="Whether agent exposes more details after auth"
    )


# =============================================================================
# Message Parts — Content blocks
# =============================================================================

class TextPart(BaseModel):
    """Plain text content."""
    type: Literal["text"] = "text"
    text: str


class FileContent(BaseModel):
    """File content — inline bytes or a URI reference."""
    name: Optional[str] = None
    mimeType: Optional[str] = None
    # One of these must be set
    bytes: Optional[str] = Field(default=None, description="base64-encoded bytes")
    uri: Optional[str] = Field(default=None, description="URI to fetch the file")


class FilePart(BaseModel):
    """File attachment."""
    type: Literal["file"] = "file"
    file: FileContent


class DataPart(BaseModel):
    """Structured JSON data."""
    type: Literal["data"] = "data"
    data: Dict[str, Any]


# Union type for all parts
Part = Union[TextPart, DataPart, FilePart]


# =============================================================================
# Message — A conversation turn
# =============================================================================

class Message(BaseModel):
    """A single conversation turn in a Task.
    
    role: "user"  → sent by the caller (supervisor / client)
    role: "agent" → sent by the agent handling the task
    """
    role: Literal["user", "agent"]
    parts: List[Part]
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# Artifact — Output produced by a task
# =============================================================================

class Artifact(BaseModel):
    """An output artifact produced during task execution.
    
    Artifacts accumulate as the agent works on a task.
    They represent the "deliverables" of the task.
    """
    name: Optional[str] = None
    description: Optional[str] = None
    parts: List[Part]
    index: int = Field(default=0, description="Ordering index for multiple artifacts")
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# Task — Unit of Work
# =============================================================================

class TaskState(str, Enum):
    """Task lifecycle states per A2A spec."""
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED)


class TaskStatus(BaseModel):
    """Current status of a task."""
    state: TaskState
    message: Optional[Message] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Task(BaseModel):
    """A2A Task — the fundamental unit of work.
    
    Lifecycle:  submitted → working → completed | failed | canceled
                                   ↘ input-required → (user sends more) → working → ...
    
    The task ID is generated by the caller. The agent updates status and
    appends artifacts as it progresses.
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique task ID (caller-generated)"
    )
    
    # Optional session grouping
    sessionId: Optional[str] = Field(
        default=None,
        description="Groups related tasks into a session"
    )
    
    # Current status
    status: TaskStatus = Field(
        default_factory=lambda: TaskStatus(state=TaskState.SUBMITTED)
    )
    
    # Full status history (if agent supports stateTransitionHistory)
    history: Optional[List[TaskStatus]] = None
    
    # Artifacts produced
    artifacts: Optional[List[Artifact]] = None
    
    # Metadata (ZTA context, tracing, etc.)
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# JSON-RPC 2.0 — Params for A2A methods
# =============================================================================

class TaskSendParams(BaseModel):
    """Parameters for tasks/send and tasks/sendSubscribe.
    
    The caller provides the task ID, an optional session, and the message.
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Task ID"
    )
    sessionId: Optional[str] = None
    message: Message
    
    # Optional: accepted output content types
    acceptedOutputModes: Optional[List[str]] = None
    
    # Optional: push notification config
    pushNotification: Optional[Dict[str, Any]] = None
    
    # ZTA metadata — propagated through the sidecar
    metadata: Optional[Dict[str, Any]] = None


class TaskQueryParams(BaseModel):
    """Parameters for tasks/get."""
    id: str
    historyLength: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class TaskCancelParams(BaseModel):
    """Parameters for tasks/cancel."""
    id: str
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# JSON-RPC 2.0 — Transport Envelope
# =============================================================================

class JSONRPCRequest(BaseModel):
    """JSON-RPC 2.0 request."""
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    method: str
    params: Optional[Dict[str, Any]] = None


class JSONRPCError(BaseModel):
    """JSON-RPC 2.0 error object."""
    code: int
    message: str
    data: Optional[Any] = None


class JSONRPCResponse(BaseModel):
    """JSON-RPC 2.0 response."""
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Any] = None
    error: Optional[JSONRPCError] = None


# =============================================================================
# Standard A2A Error Codes (extending JSON-RPC -32000 range)
# =============================================================================

class A2AError:
    """Standard error codes for A2A protocol."""
    
    # JSON-RPC standard errors
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # A2A-specific errors (-32000 to -32099)
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
    UNSUPPORTED_CONTENT_TYPE = -32004
    
    # ZTA-specific errors (-32050 to -32099)
    AUTHENTICATION_REQUIRED = -32050
    AUTHORIZATION_DENIED = -32051
    TRUST_SCORE_TOO_LOW = -32052
    POLICY_VIOLATION = -32053
    RATE_LIMITED = -32054

    @classmethod
    def task_not_found(cls, task_id: str) -> JSONRPCError:
        return JSONRPCError(
            code=cls.TASK_NOT_FOUND,
            message=f"Task not found: {task_id}"
        )

    @classmethod
    def method_not_found(cls, method: str) -> JSONRPCError:
        return JSONRPCError(
            code=cls.METHOD_NOT_FOUND,
            message=f"Method not found: {method}"
        )

    @classmethod
    def invalid_params(cls, detail: str) -> JSONRPCError:
        return JSONRPCError(
            code=cls.INVALID_PARAMS,
            message=f"Invalid parameters: {detail}"
        )

    @classmethod
    def internal_error(cls, detail: str) -> JSONRPCError:
        return JSONRPCError(
            code=cls.INTERNAL_ERROR,
            message=f"Internal error: {detail}"
        )

    @classmethod
    def auth_required(cls) -> JSONRPCError:
        return JSONRPCError(
            code=cls.AUTHENTICATION_REQUIRED,
            message="Authentication required"
        )

    @classmethod
    def authorization_denied(cls, reason: str = "") -> JSONRPCError:
        return JSONRPCError(
            code=cls.AUTHORIZATION_DENIED,
            message=f"Authorization denied{': ' + reason if reason else ''}"
        )

    @classmethod
    def policy_violation(cls, policy: str) -> JSONRPCError:
        return JSONRPCError(
            code=cls.POLICY_VIOLATION,
            message=f"Policy violation: {policy}"
        )