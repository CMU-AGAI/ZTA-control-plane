"""
Hotel Agent — ZTA Multi-Agent Testbed
======================================
A2A Server + LangGraph ReAct + MCP Client (SSE) + Groq LLM.
Creates a fresh ReAct agent per request to ensure MCP tools are properly bound.
"""

import os, sys, logging
from typing import Optional
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import HumanMessage, SystemMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.a2a import (
    A2AServer, AgentCard, AgentSkill, AgentCapabilities, AgentAuthentication,
    Task, TaskState, TaskStatus, Message, TextPart, Artifact,
)
from agents.a2a.auth import WorkloadIdentity

MCP_SERVER_URL = os.getenv("HOTEL_MCP_URL", "http://hotel-mcp:8011")
AGENT_ID = os.getenv("AGENT_ID", "hotel-agent")
AGENT_NAME = os.getenv("AGENT_NAME", "Hotel Agent")
PORT = int(os.getenv("PORT", "8092"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ZTA workload identity (Option B: agent acquires its own JWT at startup
# and forwards it to its MCP server on every call)
ZTA_AUTH_URL = os.getenv("ZTA_AUTH_URL", "")
ZTA_AGENT_SECRET = os.getenv("ZTA_AGENT_SECRET", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(AGENT_ID)

def get_llm():
    if GROQ_API_KEY:
        from langchain_groq import ChatGroq
        return ChatGroq(model="meta-llama/llama-4-scout-17b-16e-instruct", api_key=GROQ_API_KEY, temperature=0)
    elif ANTHROPIC_API_KEY:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model="claude-sonnet-4-20250514", api_key=ANTHROPIC_API_KEY)
    elif OPENAI_API_KEY:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o-mini", api_key=OPENAI_API_KEY)
    return None

AGENT_CARD = AgentCard(
    name=AGENT_NAME, description="Searches hotels, books rooms, retrieves and cancels reservations.",
    url=f"http://{AGENT_ID}:{PORT}", version="2.0.0",
    capabilities=AgentCapabilities(streaming=False, stateTransitionHistory=True),
    authentication=AgentAuthentication(schemes=["bearer"]),
    skills=[
        AgentSkill(id="hotel-search", name="Hotel Search",
            description="Search for available hotels in a city for given dates",
            tags=["hotels", "accommodation", "search", "travel"],
            examples=["Find hotels in New York from July 1 to July 5"]),
        AgentSkill(id="hotel-booking", name="Hotel Booking",
            description="Book a hotel room, retrieve booking details, or cancel a reservation",
            tags=["booking", "reservation", "cancel"],
            examples=["Book a standard room at the Hilton for Jane Doe"]),
        AgentSkill(id="city-info", name="City Information",
            description="List supported cities for hotel search",
            tags=["cities", "locations"], examples=["What cities do you cover?"]),
    ],
)

mcp_config = None
llm = None
mcp_ready = False
_workload_identity: Optional[WorkloadIdentity] = None


SYSTEM_PROMPT = """You are the Hotel Agent, a specialist in hotel search and booking.
You have tools: search_hotels, book_hotel, get_hotel_booking, get_hotel_booking_by_confirmation, cancel_hotel_booking, list_cities.
Use the appropriate tool for each request. Be concise and helpful."""

async def _build_mcp_config() -> dict:
    """Construct MCP connection config with the agent's current Bearer token.

    SPIFFE delegation pattern: the inbound A2A request was authenticated by
    the caller's token (validated by our sidecar). For OUTBOUND calls to MCP
    we present OUR OWN token — never replay the caller's. The MCP sidecar's
    micro-segmentation rule (`ALLOWED_SOURCES=<this-agent>`) requires this
    agent's identity, not the supervisor's.
    """
    headers: dict[str, str] = {}
    if _workload_identity is not None:
        headers = await _workload_identity.headers()
    else:
        headers = {"x-agent-id": AGENT_ID}
    return {
        "hotel-mcp": {
            "url": f"{MCP_SERVER_URL}/sse",
            "transport": "sse",
            "headers": headers,
        }
    }


async def handle_task(task: Task, message: Message) -> Task:
    text_parts = [p.text for p in message.parts if isinstance(p, TextPart)]
    user_text = " ".join(text_parts)
    logger.info(f"Task {task.id[:8]}: '{user_text[:100]}'")
    if not llm:
        task.status = TaskStatus(state=TaskState.FAILED,
            message=Message(role="agent", parts=[TextPart(text="No LLM configured.")]))
        return task
    try:
        # Build MCP config fresh per request so the JWT in the Authorization
        # header is current (workload identity refreshes ahead of expiry).
        cfg = await _build_mcp_config()
        client = MultiServerMCPClient(cfg)
        tools = await client.get_tools()
        logger.info(f"Tool names: {[t.name for t in tools]}")
        agent = create_react_agent(llm, tools)
        result = await agent.ainvoke({"messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_text)]})
        final = result["messages"][-1]
        response_text = final.content if hasattr(final, "content") else str(final)
        tools_called = [tc.get("name", "?") for msg in result["messages"]
                        if hasattr(msg, "tool_calls") and msg.tool_calls for tc in msg.tool_calls]
        task.status = TaskStatus(state=TaskState.COMPLETED,
            message=Message(role="agent", parts=[TextPart(text=response_text)]))
        task.artifacts = [Artifact(name="response", parts=[TextPart(text=response_text)],
                                   metadata={"tools_called": tools_called, "agent_id": AGENT_ID})]
    except Exception as e:
        logger.exception(f"Task {task.id[:8]} failed: {e}")
        task.status = TaskStatus(state=TaskState.FAILED,
            message=Message(role="agent", parts=[TextPart(text=f"Error: {e}")]))
    return task

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_config, llm, mcp_ready, _workload_identity
    logger.info(f"Starting {AGENT_NAME}")

    if ZTA_AUTH_URL and ZTA_AGENT_SECRET:
        _workload_identity = WorkloadIdentity(
            agent_id=AGENT_ID,
            auth_url=ZTA_AUTH_URL,
            shared_secret=ZTA_AGENT_SECRET,
        )
        ok = await _workload_identity.bootstrap()
        if ok:
            logger.info(f"workload identity acquired for {AGENT_ID}")
        else:
            logger.warning(f"workload identity bootstrap FAILED for {AGENT_ID}")
    else:
        logger.warning("ZTA_AUTH_URL/ZTA_AGENT_SECRET not set — running without JWT")

    mcp_config = await _build_mcp_config()
    try:
        test = MultiServerMCPClient(mcp_config)
        tools = await test.get_tools()
        logger.info(f"MCP reachable: {len(tools)} tools")
        mcp_ready = True
    except Exception as e:
        logger.error(f"MCP not reachable: {e}")
    llm = get_llm()
    if llm: logger.info("LLM configured")
    yield
    if _workload_identity is not None:
        await _workload_identity.aclose()

a2a_server = A2AServer(card=AGENT_CARD, handler=handle_task)
app = FastAPI(title=f"{AGENT_NAME} API", version="2.0.0", lifespan=lifespan)
app.include_router(a2a_server.router)

@app.get("/health")
async def health():
    return {"status": "healthy", "agent_id": AGENT_ID, "mcp_connected": mcp_ready,
            "llm_available": llm is not None, "protocol": "a2a",
            "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/identity")
async def identity():
    return {"agent_id": AGENT_ID, "agent_name": AGENT_NAME, "agent_type": "worker",
            "domain": "hotel", "protocol": "a2a"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)