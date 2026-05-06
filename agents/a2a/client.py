"""
A2A Protocol Client
====================
Async HTTP client for interacting with A2A-compliant agents.

Provides:
- Agent Card discovery via GET /.well-known/agent.json
- Task submission via tasks/send
- Task querying via tasks/get
- Task cancellation via tasks/cancel

Propagates ZTA identity headers on every request.

Usage:
    from agents.a2a import A2AClient, Message, TextPart

    async with A2AClient(base_url="http://airline-agent:8091") as client:
        card = await client.get_agent_card()
        task = await client.send_task(
            message=Message(role="user", parts=[TextPart(text="Search flights JFK→LAX")])
        )
        print(task.status.state)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx

from agents.a2a.models import (
    AgentCard,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    Message,
    Task,
    TaskCancelParams,
    TaskQueryParams,
    TaskSendParams,
)

logger = logging.getLogger("a2a.client")


class A2AClientError(Exception):
    """Raised when an A2A operation fails."""

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class A2AClient:
    """
    Async A2A protocol client.
    
    Handles Agent Card discovery, task lifecycle operations,
    and ZTA header propagation. Optionally acquires JWT from auth service.
    
    Args:
        base_url: Base URL of the remote A2A agent
        agent_id: This client's agent identity (for ZTA headers)
        agent_name: Human-readable name (for ZTA headers)
        timeout: Request timeout in seconds
        extra_headers: Additional headers to send on every request
        auth_url: Optional URL of the ZTA auth service for JWT tokens
        auth_secret: Shared secret for authenticating with the auth service
    """

    def __init__(
        self,
        base_url: str,
        agent_id: str = "a2a-client",
        agent_name: str = "A2A Client",
        timeout: float = 60.0,
        extra_headers: Optional[Dict[str, str]] = None,
        auth_url: Optional[str] = None,
        auth_secret: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.timeout = timeout
        self.auth_url = auth_url
        self.auth_secret = auth_secret
        self._jwt_token: Optional[str] = None

        # Default headers — ZTA identity propagation
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-agent-id": agent_id,
            "x-agent-name": agent_name,
            "x-a2a-protocol-version": "0.2",
        }
        if extra_headers:
            self._headers.update(extra_headers)

        self._client: Optional[httpx.AsyncClient] = None
        self._agent_card: Optional[AgentCard] = None

    async def acquire_token(self) -> Optional[str]:
        """Acquire a JWT token from the auth service."""
        if not self.auth_url or not self.auth_secret:
            return None
        
        await self._ensure_client()
        try:
            response = await self._client.post(
                f"{self.auth_url}/token",
                json={"agent_id": self.agent_id, "secret": self.auth_secret},
            )
            response.raise_for_status()
            data = response.json()
            self._jwt_token = data["access_token"]
            self._headers["Authorization"] = f"Bearer {self._jwt_token}"
            # Recreate client with updated headers
            if self._client:
                await self._client.aclose()
                self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers)
            logger.info(f"JWT acquired for {self.agent_id} (expires_in={data['expires_in']}s)")
            return self._jwt_token
        except Exception as e:
            logger.warning(f"Failed to acquire JWT from {self.auth_url}: {e}")
            return None

    # =========================================================================
    # Context Manager
    # =========================================================================

    async def __aenter__(self) -> A2AClient:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._headers,
        )
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self):
        """Lazily create the HTTP client if not using context manager."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers=self._headers,
            )

    async def close(self):
        """Explicitly close the client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # =========================================================================
    # Agent Card Discovery
    # =========================================================================

    async def get_agent_card(self) -> AgentCard:
        """Fetch the Agent Card from /.well-known/agent.json.
        
        Returns:
            AgentCard with the remote agent's capabilities.
        
        Raises:
            A2AClientError: If the card cannot be fetched or parsed.
        """
        await self._ensure_client()
        url = f"{self.base_url}/.well-known/agent.json"
        
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            self._agent_card = AgentCard(**data)
            logger.info(
                f"Discovered agent: {self._agent_card.name} "
                f"({len(self._agent_card.skills)} skills)"
            )
            return self._agent_card
        except httpx.HTTPStatusError as e:
            raise A2AClientError(
                f"Failed to fetch agent card from {url}: HTTP {e.response.status_code}",
                code=e.response.status_code,
            )
        except Exception as e:
            raise A2AClientError(f"Failed to fetch agent card from {url}: {e}")

    @property
    def agent_card(self) -> Optional[AgentCard]:
        """Return the cached Agent Card (None if not yet fetched)."""
        return self._agent_card

    # =========================================================================
    # JSON-RPC Helper
    # =========================================================================

    async def _rpc_call(
        self,
        method: str,
        params: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Make a JSON-RPC 2.0 call to the A2A endpoint.
        
        Returns:
            The 'result' field from the JSON-RPC response.
        
        Raises:
            A2AClientError: On transport or JSON-RPC errors.
        """
        await self._ensure_client()
        url = f"{self.base_url}/a2a"

        rpc_request = JSONRPCRequest(
            method=method,
            params=params,
        )

        headers = {}
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = await self._client.post(
                url,
                json=rpc_request.model_dump(exclude_none=True),
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as e:
            raise A2AClientError(
                f"A2A request failed: HTTP {e.response.status_code}",
                code=e.response.status_code,
            )
        except Exception as e:
            raise A2AClientError(f"A2A request failed: {e}")

        # Parse JSON-RPC response
        rpc_response = JSONRPCResponse(**body)

        if rpc_response.error:
            raise A2AClientError(
                message=rpc_response.error.message,
                code=rpc_response.error.code,
                data=rpc_response.error.data,
            )

        return rpc_response.result

    # =========================================================================
    # Task Operations
    # =========================================================================

    async def send_task(
        self,
        message: Message,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """Send a task to the remote agent (tasks/send).
        
        If task_id is provided and the task exists in input-required state,
        this continues the task with additional input.
        
        Args:
            message: The user message to send
            task_id: Optional task ID (generated if not provided)
            session_id: Optional session grouping
            metadata: Optional metadata (ZTA context, etc.)
        
        Returns:
            Task with updated status and any artifacts.
        """
        if task_id is None:
            task_id = str(uuid.uuid4())

        params = TaskSendParams(
            id=task_id,
            sessionId=session_id,
            message=message,
            metadata=metadata,
        )

        result = await self._rpc_call(
            "tasks/send",
            params.model_dump(exclude_none=True),
        )

        return Task(**result)

    async def get_task(
        self,
        task_id: str,
        history_length: Optional[int] = None,
    ) -> Task:
        """Query the status of a task (tasks/get).
        
        Args:
            task_id: The task to query
            history_length: Optional — limit history entries returned
        
        Returns:
            Task with current status and artifacts.
        """
        params = TaskQueryParams(
            id=task_id,
            historyLength=history_length,
        )

        result = await self._rpc_call(
            "tasks/get",
            params.model_dump(exclude_none=True),
        )

        return Task(**result)

    async def cancel_task(self, task_id: str) -> Task:
        """Cancel a running task (tasks/cancel).
        
        Args:
            task_id: The task to cancel
        
        Returns:
            Task with canceled status.
        """
        params = TaskCancelParams(id=task_id)

        result = await self._rpc_call(
            "tasks/cancel",
            params.model_dump(exclude_none=True),
        )

        return Task(**result)

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def send_text(
        self,
        text: str,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """Convenience — send a plain text message as a task.
        
        Wraps the text in a Message with a single TextPart.
        """
        from agents.a2a.models import TextPart

        message = Message(
            role="user",
            parts=[TextPart(text=text)],
        )
        return await self.send_task(
            message=message,
            task_id=task_id,
            session_id=session_id,
            metadata=metadata,
        )

    async def discover_and_check(self) -> bool:
        """Discover the agent and verify it's healthy.
        
        Returns True if the agent card was fetched and the
        /a2a/health endpoint responds.
        """
        try:
            await self.get_agent_card()
            await self._ensure_client()
            response = await self._client.get(f"{self.base_url}/a2a/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Agent at {self.base_url} not reachable: {e}")
            return False

    def get_skills_summary(self) -> List[Dict[str, Any]]:
        """Return a summary of the remote agent's skills.
        
        Requires get_agent_card() to have been called first.
        """
        if not self._agent_card:
            return []
        return [
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "tags": skill.tags,
            }
            for skill in self._agent_card.skills
        ]