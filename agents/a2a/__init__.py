"""
A2A (Agent-to-Agent) Protocol Library
======================================
Implementation of Google's Agent2Agent Protocol for the ZTA Multi-Agent Testbed.

Spec reference: https://a2aprotocol.org/latest/specification/
Implements: Agent Cards, Task lifecycle, Message/Part types, JSON-RPC 2.0 transport.

Components:
- models.py:  Pydantic models for all A2A types (AgentCard, Task, Message, Part, etc.)
- server.py:  A2AServer — FastAPI router that exposes the A2A JSON-RPC endpoints
- client.py:  A2AClient — async HTTP client for sending/receiving A2A tasks
"""

from agents.a2a.models import (
    AgentCard,
    AgentSkill,
    AgentCapabilities,
    AgentProvider,
    AgentAuthentication,
    Task,
    TaskState,
    TaskStatus,
    TaskSendParams,
    TaskQueryParams,
    TaskCancelParams,
    Message,
    Part,
    TextPart,
    DataPart,
    FilePart,
    FileContent,
    Artifact,
    JSONRPCRequest,
    JSONRPCResponse,
    JSONRPCError,
    A2AError,
)

from agents.a2a.server import A2AServer
from agents.a2a.client import A2AClient

__all__ = [
    # Models
    "AgentCard",
    "AgentSkill",
    "AgentCapabilities",
    "AgentProvider",
    "AgentAuthentication",
    "Task",
    "TaskState",
    "TaskStatus",
    "TaskSendParams",
    "TaskQueryParams",
    "TaskCancelParams",
    "Message",
    "Part",
    "TextPart",
    "DataPart",
    "FilePart",
    "FileContent",
    "Artifact",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCError",
    "A2AError",
    # Server & Client
    "A2AServer",
    "A2AClient",
]
