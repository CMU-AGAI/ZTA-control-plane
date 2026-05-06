"""
A2A Protocol Server
====================
FastAPI-based server that implements the A2A JSON-RPC endpoints.

Endpoints:
  GET  /.well-known/agent.json  → Agent Card (capability discovery)
  POST /a2a                     → JSON-RPC 2.0 dispatch for:
       - tasks/send             → Submit or continue a task
       - tasks/get              → Query task status
       - tasks/cancel           → Cancel a running task

The server holds an in-memory task store and delegates actual work
to a user-provided handler coroutine.

Usage:
    from agents.a2a import A2AServer, AgentCard, AgentSkill

    card = AgentCard(
        name="Airline Agent",
        url="http://airline-agent:8091",
        skills=[AgentSkill(id="flights", name="Flight Search", ...)]
    )

    async def handle_task(task):
        # your logic here — call MCP tools, LLMs, etc.
        return task  # with updated status + artifacts

    server = A2AServer(card=card, handler=handle_task)
    app.include_router(server.router)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agents.a2a.models import (
    A2AError,
    AgentCard,
    Artifact,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    Message,
    Task,
    TaskCancelParams,
    TaskQueryParams,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TextPart,
)

logger = logging.getLogger("a2a.server")

# Type alias for the handler function
TaskHandler = Callable[[Task, Message], Coroutine[Any, Any, Task]]


class A2AServer:
    """
    A2A Protocol Server.
    
    Manages Agent Card serving, JSON-RPC dispatch, and in-memory task store.
    Delegates actual task processing to the provided handler.
    
    Args:
        card: The Agent Card describing this agent's capabilities
        handler: Async function (task, message) → updated task
                 Called when tasks/send is invoked.
    """

    def __init__(self, card: AgentCard, handler: TaskHandler):
        self.card = card
        self.handler = handler
        self.tasks: Dict[str, Task] = {}
        self.router = APIRouter()
        self._register_routes()

    def _register_routes(self):
        """Register the A2A HTTP endpoints."""

        @self.router.get("/.well-known/agent.json")
        async def get_agent_card():
            """Serve the Agent Card for capability discovery."""
            return JSONResponse(
                content=self.card.model_dump(exclude_none=True),
                media_type="application/json",
            )

        @self.router.post("/a2a")
        async def jsonrpc_dispatch(request: Request):
            """JSON-RPC 2.0 dispatch for A2A methods."""
            try:
                body = await request.json()
            except Exception:
                return self._error_response(
                    None,
                    JSONRPCError(
                        code=A2AError.PARSE_ERROR,
                        message="Invalid JSON"
                    ),
                )

            # Parse JSON-RPC request
            try:
                rpc_request = JSONRPCRequest(**body)
            except Exception as e:
                return self._error_response(
                    body.get("id"),
                    JSONRPCError(
                        code=A2AError.INVALID_REQUEST,
                        message=f"Invalid JSON-RPC request: {e}"
                    ),
                )

            # Extract ZTA headers for metadata propagation
            zta_metadata = self._extract_zta_headers(request)

            # Dispatch to method handler
            method = rpc_request.method
            params = rpc_request.params or {}

            if method == "tasks/send":
                result = await self._handle_tasks_send(params, zta_metadata)
            elif method == "tasks/get":
                result = await self._handle_tasks_get(params)
            elif method == "tasks/cancel":
                result = await self._handle_tasks_cancel(params)
            else:
                return self._error_response(
                    rpc_request.id,
                    A2AError.method_not_found(method),
                )

            # Return result or error
            if isinstance(result, JSONRPCError):
                return self._error_response(rpc_request.id, result)

            return JSONResponse(
                content=JSONRPCResponse(
                    id=rpc_request.id,
                    result=result,
                ).model_dump(exclude_none=True)
            )

        # Health endpoint (extends agent's existing /health)
        @self.router.get("/a2a/health")
        async def a2a_health():
            """A2A-specific health check."""
            return {
                "status": "healthy",
                "protocol": "a2a",
                "protocolVersion": self.card.protocolVersion,
                "agent": self.card.name,
                "activeTasks": len(
                    [t for t in self.tasks.values() if not t.status.state.is_terminal]
                ),
                "totalTasks": len(self.tasks),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    # =========================================================================
    # JSON-RPC Method Handlers
    # =========================================================================

    async def _handle_tasks_send(
        self, params: Dict[str, Any], zta_metadata: Dict[str, str]
    ) -> Any:
        """Handle tasks/send — submit or continue a task."""
        try:
            send_params = TaskSendParams(**params)
        except Exception as e:
            return A2AError.invalid_params(str(e))

        task_id = send_params.id

        # Merge ZTA metadata
        task_metadata = send_params.metadata or {}
        task_metadata.update(zta_metadata)

        # Check if this is a continuation of an existing task
        if task_id in self.tasks:
            task = self.tasks[task_id]

            # Can only continue tasks in input-required state
            if task.status.state == TaskState.INPUT_REQUIRED:
                task.status = TaskStatus(state=TaskState.WORKING)
                if task.history is not None:
                    task.history.append(task.status)
            elif task.status.state.is_terminal:
                return A2AError.invalid_params(
                    f"Task {task_id} is in terminal state: {task.status.state.value}"
                )
            # else: task is submitted/working — append additional input
        else:
            # New task
            task = Task(
                id=task_id,
                sessionId=send_params.sessionId,
                status=TaskStatus(state=TaskState.SUBMITTED),
                history=[TaskStatus(state=TaskState.SUBMITTED)],
                artifacts=[],
                metadata=task_metadata,
            )
            self.tasks[task_id] = task

        # Transition to working
        task.status = TaskStatus(state=TaskState.WORKING)
        if task.history is not None:
            task.history.append(task.status)

        # Delegate to the agent's handler
        try:
            updated_task = await self.handler(task, send_params.message)
            self.tasks[task_id] = updated_task

            # Record state transition
            if updated_task.history is not None:
                updated_task.history.append(updated_task.status)

            return updated_task.model_dump(exclude_none=True)

        except Exception as e:
            logger.exception(f"Handler error for task {task_id}: {e}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message=Message(
                    role="agent",
                    parts=[TextPart(text=f"Internal error: {str(e)}")]
                ),
            )
            if task.history is not None:
                task.history.append(task.status)
            self.tasks[task_id] = task
            return task.model_dump(exclude_none=True)

    async def _handle_tasks_get(self, params: Dict[str, Any]) -> Any:
        """Handle tasks/get — query task status."""
        try:
            query_params = TaskQueryParams(**params)
        except Exception as e:
            return A2AError.invalid_params(str(e))

        task = self.tasks.get(query_params.id)
        if not task:
            return A2AError.task_not_found(query_params.id)

        # Optionally trim history
        result = task.model_dump(exclude_none=True)
        if query_params.historyLength is not None and task.history:
            result["history"] = [
                h.model_dump(exclude_none=True)
                for h in task.history[-query_params.historyLength:]
            ]

        return result

    async def _handle_tasks_cancel(self, params: Dict[str, Any]) -> Any:
        """Handle tasks/cancel — cancel a running task."""
        try:
            cancel_params = TaskCancelParams(**params)
        except Exception as e:
            return A2AError.invalid_params(str(e))

        task = self.tasks.get(cancel_params.id)
        if not task:
            return A2AError.task_not_found(cancel_params.id)

        if task.status.state.is_terminal:
            return JSONRPCError(
                code=A2AError.TASK_NOT_CANCELABLE,
                message=f"Task {cancel_params.id} already in terminal state: {task.status.state.value}",
            )

        task.status = TaskStatus(state=TaskState.CANCELED)
        if task.history is not None:
            task.history.append(task.status)
        self.tasks[cancel_params.id] = task

        return task.model_dump(exclude_none=True)

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _extract_zta_headers(request: Request) -> Dict[str, str]:
        """Extract ZTA-relevant headers for metadata propagation."""
        zta_headers = {}
        prefix = "x-"
        for key, value in request.headers.items():
            if key.lower().startswith(prefix):
                zta_headers[key.lower()] = value
        return zta_headers

    @staticmethod
    def _error_response(
        rpc_id: Optional[Any], error: JSONRPCError
    ) -> JSONResponse:
        """Build a JSON-RPC error response."""
        return JSONResponse(
            content=JSONRPCResponse(
                id=rpc_id,
                error=error,
            ).model_dump(exclude_none=True),
            status_code=200,  # JSON-RPC always returns 200; errors are in the body
        )

    # =========================================================================
    # Task Store Utilities (for external use)
    # =========================================================================

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID (for internal use by the agent)."""
        return self.tasks.get(task_id)

    def update_task_status(
        self,
        task_id: str,
        state: TaskState,
        message: Optional[Message] = None,
    ) -> Optional[Task]:
        """Update a task's status (for use during long-running processing)."""
        task = self.tasks.get(task_id)
        if not task:
            return None
        task.status = TaskStatus(state=state, message=message)
        if task.history is not None:
            task.history.append(task.status)
        return task

    def add_artifact(
        self,
        task_id: str,
        artifact: Artifact,
    ) -> Optional[Task]:
        """Add an artifact to a task."""
        task = self.tasks.get(task_id)
        if not task:
            return None
        if task.artifacts is None:
            task.artifacts = []
        artifact.index = len(task.artifacts)
        task.artifacts.append(artifact)
        return task