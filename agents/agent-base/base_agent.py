"""
ZTA Agent Base Class
Provides common functionality for all worker agents:
- HTTP API exposure for supervisor communication
- MCP client connection management
- Health checks and metrics
- Identity header propagation for ZTA
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
import httpx
import os
import logging
from datetime import datetime
import asyncio

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# =============================================================================
# Data Models
# =============================================================================

class AgentRequest(BaseModel):
    """Request from supervisor to agent"""
    message: str
    context: Optional[Dict[str, Any]] = {}
    conversation_id: Optional[str] = None

class AgentResponse(BaseModel):
    """Response from agent to supervisor"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    tools_called: List[str] = []
    error: Optional[str] = None

class ToolCall(BaseModel):
    """Record of a tool call"""
    tool_name: str
    arguments: Dict[str, Any]
    result: Any
    duration_ms: float

# =============================================================================
# Base Agent Class
# =============================================================================

class BaseAgent(ABC):
    """
    Abstract base class for all worker agents.
    
    Subclasses must implement:
    - agent_id: Unique identifier for ZTA
    - agent_name: Human-readable name
    - process_request(): Main request handling logic
    - get_tools(): List of available tools
    """
    
    def __init__(self):
        self.logger = logging.getLogger(self.agent_id)
        self.mcp_client = None
        self.tools = []
        self.metrics = {
            "requests_total": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "tools_called": 0,
            "start_time": datetime.utcnow().isoformat()
        }
    
    @property
    @abstractmethod
    def agent_id(self) -> str:
        """Unique agent identifier for ZTA headers"""
        pass
    
    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Human-readable agent name"""
        pass
    
    @property
    @abstractmethod
    def mcp_server_url(self) -> str:
        """URL of the MCP server this agent connects to"""
        pass
    
    @abstractmethod
    async def process_request(self, request: AgentRequest) -> AgentResponse:
        """Process a request from the supervisor"""
        pass
    
    @abstractmethod
    def get_tools(self) -> List[Dict[str, Any]]:
        """Return list of available tools"""
        pass
    
    async def initialize(self):
        """Initialize agent - connect to MCP server"""
        self.logger.info(f"Initializing {self.agent_name}...")
        try:
            await self._connect_to_mcp()
            self.logger.info(f"{self.agent_name} initialized with {len(self.tools)} tools")
        except Exception as e:
            self.logger.error(f"Failed to initialize {self.agent_name}: {e}")
            raise
    
    async def shutdown(self):
        """Cleanup on shutdown"""
        self.logger.info(f"Shutting down {self.agent_name}...")
        if self.mcp_client:
            await self.mcp_client.aclose()
    
    async def _connect_to_mcp(self):
        """Connect to MCP server and discover tools"""
        self.mcp_client = httpx.AsyncClient(
            base_url=self.mcp_server_url,
            timeout=30.0,
            headers={
                "x-agent-id": self.agent_id,
                "x-agent-name": self.agent_name
            }
        )
        # Verify connection
        response = await self.mcp_client.get("/health")
        if response.status_code != 200:
            raise ConnectionError(f"MCP server health check failed: {response.status_code}")
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool via MCP server"""
        start_time = datetime.utcnow()
        
        try:
            # MCP tool call via HTTP
            response = await self.mcp_client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments
                    },
                    "id": f"{self.agent_id}-{datetime.utcnow().timestamp()}"
                },
                headers={
                    "x-agent-id": self.agent_id
                }
            )
            
            duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            self.metrics["tools_called"] += 1
            
            if response.status_code == 200:
                result = response.json()
                self.logger.info(f"Tool {tool_name} called successfully in {duration_ms:.2f}ms")
                return result.get("result")
            else:
                self.logger.error(f"Tool {tool_name} failed: {response.status_code}")
                raise Exception(f"Tool call failed: {response.text}")
                
        except Exception as e:
            self.logger.error(f"Error calling tool {tool_name}: {e}")
            raise
    
    def health_check(self) -> Dict[str, Any]:
        """Return health status"""
        return {
            "status": "healthy",
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "mcp_server": self.mcp_server_url,
            "tools_count": len(self.tools),
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def get_metrics(self) -> Dict[str, Any]:
        """Return agent metrics"""
        return {
            **self.metrics,
            "agent_id": self.agent_id,
            "uptime_seconds": (
                datetime.utcnow() - datetime.fromisoformat(self.metrics["start_time"])
            ).total_seconds()
        }


# =============================================================================
# HTTP API Factory
# =============================================================================

def create_agent_app(agent: BaseAgent) -> FastAPI:
    """
    Create FastAPI application for an agent.
    Wraps the agent with HTTP endpoints.
    """
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        await agent.initialize()
        yield
        # Shutdown
        await agent.shutdown()
    
    app = FastAPI(
        title=f"{agent.agent_name} API",
        description=f"HTTP API for {agent.agent_name} - ZTA Multi-Agent Testbed",
        version="1.0.0",
        lifespan=lifespan
    )
    
    @app.get("/health")
    async def health():
        """Health check endpoint"""
        return agent.health_check()
    
    @app.get("/metrics")
    async def metrics():
        """Metrics endpoint"""
        return agent.get_metrics()
    
    @app.get("/tools")
    async def tools():
        """List available tools"""
        return {
            "agent_id": agent.agent_id,
            "tools": agent.get_tools()
        }
    
    @app.post("/invoke", response_model=AgentResponse)
    async def invoke(request: AgentRequest, http_request: Request):
        """
        Main endpoint - invoke agent to process a request.
        Called by the supervisor.
        """
        agent.metrics["requests_total"] += 1
        
        # Log incoming request with ZTA context
        supervisor_id = http_request.headers.get("x-supervisor-id", "unknown")
        agent.logger.info(
            f"Received request from supervisor={supervisor_id}: {request.message[:100]}..."
        )
        
        try:
            response = await agent.process_request(request)
            if response.success:
                agent.metrics["requests_success"] += 1
            else:
                agent.metrics["requests_failed"] += 1
            return response
            
        except Exception as e:
            agent.metrics["requests_failed"] += 1
            agent.logger.exception(f"Error processing request: {e}")
            return AgentResponse(
                success=False,
                message="Agent error",
                error=str(e)
            )
    
    @app.get("/identity")
    async def identity():
        """Return agent identity for ZTA verification"""
        return {
            "agent_id": agent.agent_id,
            "agent_name": agent.agent_name,
            "agent_type": "worker",
            "capabilities": [tool["name"] for tool in agent.get_tools()]
        }
    
    return app
