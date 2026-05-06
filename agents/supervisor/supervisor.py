"""
Travel Supervisor — ZTA Multi-Agent Testbed
=============================================
Orchestrator that discovers domain agents via A2A Agent Cards,
classifies user intent with an LLM, and delegates tasks via A2A protocol.

Architecture:
  - A2A Client: Fetches Agent Cards from domain agents at startup
  - LangGraph StateGraph: Intent classification → agent delegation → response assembly
  - A2A Tasks: Sends tasks/send to domain agents, collects artifacts
  - ZTA: Propagates identity headers through the A2A client
"""

import os
import sys
import logging
from typing import Dict, Any, List, Optional, TypedDict, Annotated
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

# LangGraph
from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage, SystemMessage

# A2A protocol
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.a2a import (
    A2AClient, AgentCard, Message, TextPart, TaskState,
)

# =============================================================================
# Configuration
# =============================================================================

SUPERVISOR_ID = os.getenv("SUPERVISOR_ID", "supervisor-agent")
SUPERVISOR_NAME = os.getenv("SUPERVISOR_NAME", "Travel Supervisor")
PORT = int(os.getenv("PORT", "8080"))

# Agent discovery URLs — each exposes /.well-known/agent.json
AGENT_URLS = {
    "airline": os.getenv("AIRLINE_AGENT_URL", "http://airline-agent:8091"),
    "hotel": os.getenv("HOTEL_AGENT_URL", "http://hotel-agent:8092"),
    "car-rental": os.getenv("CAR_RENTAL_AGENT_URL", "http://car-rental-agent:8093"),
}

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ZTA Auth Service
AUTH_URL = os.getenv("ZTA_AUTH_URL", "")  # e.g., http://zta-auth:8180
AUTH_SECRET = os.getenv("ZTA_AGENT_SECRET", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(SUPERVISOR_ID)

# =============================================================================
# LLM Setup
# =============================================================================

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


# =============================================================================
# Agent Registry (populated at startup via A2A discovery)
# =============================================================================

class AgentEntry:
    """Registry entry for a discovered agent."""
    def __init__(self, domain: str, url: str):
        self.domain = domain
        self.url = url
        self.card: Optional[AgentCard] = None
        self.client: Optional[A2AClient] = None
        self.healthy: bool = False


agent_registry: Dict[str, AgentEntry] = {}
llm = None


# =============================================================================
# LangGraph State
# =============================================================================

class SupervisorState(TypedDict):
    user_message: str
    intent: str
    agent_domain: str
    agent_response: str
    tools_called: List[str]
    success: bool
    error: str


# =============================================================================
# LangGraph Nodes
# =============================================================================

async def classify_intent(state: SupervisorState) -> SupervisorState:
    """Classify user intent → which domain agent to use."""
    user_msg = state["user_message"]

    if llm:
        # LLM-based classification
        system = """You are an intent classifier for a travel booking system.
Classify the user's message into exactly one category:
- airline: flights, airports, boarding passes, airlines
- hotel: hotels, rooms, accommodations, lodging
- car-rental: car rentals, vehicles, pickup/dropoff
- general: unclear or multi-domain

Also consider the skills available from each agent:
""" + _build_skills_context() + """

Respond with ONLY the category name, nothing else."""

        try:
            response = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user_msg)])
            intent = response.content.strip().lower()
            if intent in agent_registry:
                state["intent"] = intent
                state["agent_domain"] = intent
            else:
                state["intent"] = "general"
                state["agent_domain"] = ""
        except Exception as e:
            logger.error(f"LLM classification failed: {e}")
            state["intent"] = _keyword_classify(user_msg)
            state["agent_domain"] = state["intent"] if state["intent"] != "general" else ""
    else:
        state["intent"] = _keyword_classify(user_msg)
        state["agent_domain"] = state["intent"] if state["intent"] != "general" else ""

    logger.info(f"Intent: {state['intent']} → agent: {state['agent_domain'] or 'none'}")
    return state


async def delegate_to_agent(state: SupervisorState) -> SupervisorState:
    """Send the task to the appropriate domain agent via A2A."""
    domain = state["agent_domain"]
    entry = agent_registry.get(domain)

    if not entry or not entry.client:
        state["success"] = False
        state["error"] = f"Agent '{domain}' not available"
        return state

    try:
        task = await entry.client.send_text(
            text=state["user_message"],
            metadata={
                "x-supervisor-id": SUPERVISOR_ID,
                "x-intent": state["intent"],
            },
        )

        if task.status.state == TaskState.COMPLETED:
            # Extract response from status message or artifacts
            if task.status.message and task.status.message.parts:
                text_parts = [p.text for p in task.status.message.parts if hasattr(p, "text")]
                state["agent_response"] = " ".join(text_parts)
            elif task.artifacts:
                text_parts = [p.text for a in task.artifacts for p in a.parts if hasattr(p, "text")]
                state["agent_response"] = " ".join(text_parts)
            else:
                state["agent_response"] = "Task completed (no text response)"

            # Extract tools called from artifact metadata
            if task.artifacts and task.artifacts[0].metadata:
                state["tools_called"] = task.artifacts[0].metadata.get("tools_called", [])

            state["success"] = True
        else:
            state["success"] = False
            err_msg = ""
            if task.status.message and task.status.message.parts:
                err_msg = " ".join(p.text for p in task.status.message.parts if hasattr(p, "text"))
            state["error"] = f"Task {task.status.state.value}: {err_msg}"

    except Exception as e:
        logger.exception(f"A2A task to {domain} failed: {e}")
        state["success"] = False
        state["error"] = str(e)

    return state


async def handle_general(state: SupervisorState) -> SupervisorState:
    """Handle general/unclear requests."""
    available = [f"- {d}: {e.card.description}" for d, e in agent_registry.items() if e.card]
    state["agent_response"] = (
        f"I'm the Travel Supervisor. I can help you with:\n"
        + "\n".join(available)
        + "\n\nPlease specify what you'd like help with!"
    )
    state["success"] = True
    return state


def route_by_intent(state: SupervisorState) -> str:
    """Route to the appropriate node based on intent."""
    if state.get("agent_domain") and state["agent_domain"] in agent_registry:
        return "delegate"
    return "general"


# Build the LangGraph
def build_supervisor_graph() -> Any:
    graph = StateGraph(SupervisorState)
    graph.add_node("classify", classify_intent)
    graph.add_node("delegate", delegate_to_agent)
    graph.add_node("general", handle_general)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", route_by_intent, {"delegate": "delegate", "general": "general"})
    graph.add_edge("delegate", END)
    graph.add_edge("general", END)

    return graph.compile()


# =============================================================================
# Helpers
# =============================================================================

def _keyword_classify(message: str) -> str:
    msg = message.lower()
    if any(kw in msg for kw in ["flight", "airport", "airline", "fly", "plane", "boarding"]):
        return "airline"
    if any(kw in msg for kw in ["hotel", "room", "stay", "accommodation", "lodge", "resort"]):
        return "hotel"
    if any(kw in msg for kw in ["car", "vehicle", "rental", "rent", "drive", "pickup"]):
        return "car-rental"
    return "general"


def _build_skills_context() -> str:
    lines = []
    for domain, entry in agent_registry.items():
        if entry.card:
            skills = ", ".join(s.name for s in entry.card.skills)
            lines.append(f"  {domain}: {skills}")
    return "\n".join(lines) if lines else "  (no agents discovered yet)"


# =============================================================================
# FastAPI Application
# =============================================================================

supervisor_graph = None


class UserRequest(BaseModel):
    message: str
    context: Optional[Dict[str, Any]] = {}


class SupervisorResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    agent_used: Optional[str] = None
    tools_called: List[str] = []
    error: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_registry, llm, supervisor_graph

    logger.info(f"Starting {SUPERVISOR_NAME}")

    # Setup LLM
    llm = get_llm()
    if llm:
        logger.info("LLM configured for intent classification")

    # Discover agents via A2A Agent Cards
    for domain, url in AGENT_URLS.items():
        entry = AgentEntry(domain=domain, url=url)
        entry.client = A2AClient(
            base_url=url,
            agent_id=SUPERVISOR_ID,
            agent_name=SUPERVISOR_NAME,
            auth_url=AUTH_URL if AUTH_URL else None,
            auth_secret=AUTH_SECRET if AUTH_SECRET else None,
        )
        # Acquire JWT if auth service is configured
        if AUTH_URL:
            await entry.client.acquire_token()
        try:
            entry.card = await entry.client.get_agent_card()
            entry.healthy = True
            skills = [s.name for s in entry.card.skills]
            logger.info(f"Discovered {domain} agent: {entry.card.name} — skills: {skills}")
        except Exception as e:
            logger.warning(f"Could not discover {domain} agent at {url}: {e}")
            entry.healthy = False
        agent_registry[domain] = entry

    # Build the LangGraph supervisor graph
    supervisor_graph = build_supervisor_graph()
    logger.info(f"{SUPERVISOR_NAME} initialized — {sum(1 for e in agent_registry.values() if e.healthy)} agents discovered")

    yield

    # Shutdown — close A2A clients
    for entry in agent_registry.values():
        if entry.client:
            await entry.client.close()
    logger.info(f"{SUPERVISOR_NAME} shut down")


app = FastAPI(
    title=f"{SUPERVISOR_NAME} API",
    description="Travel Supervisor — LangGraph + A2A orchestrator | ZTA Multi-Agent Testbed",
    version="2.0.0",
    lifespan=lifespan,
)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.post("/chat", response_model=SupervisorResponse)
async def chat(request: UserRequest):
    """Main endpoint — process user request through the LangGraph supervisor."""
    logger.info(f"Chat request: {request.message[:100]}")

    if supervisor_graph is None:
        return SupervisorResponse(success=False, message="Supervisor not initialized", error="startup_pending")

    try:
        initial_state: SupervisorState = {
            "user_message": request.message,
            "intent": "",
            "agent_domain": "",
            "agent_response": "",
            "tools_called": [],
            "success": False,
            "error": "",
        }

        result = await supervisor_graph.ainvoke(initial_state)

        return SupervisorResponse(
            success=result["success"],
            message=result["agent_response"],
            agent_used=result["agent_domain"] or None,
            tools_called=result["tools_called"],
            error=result["error"] if not result["success"] else None,
            data={"intent": result["intent"]},
        )

    except Exception as e:
        logger.exception(f"Error: {e}")
        return SupervisorResponse(success=False, message="An error occurred", error=str(e))


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "supervisor_id": SUPERVISOR_ID,
        "agents": {
            domain: {"healthy": e.healthy, "skills": len(e.card.skills) if e.card else 0}
            for domain, e in agent_registry.items()
        },
        "llm_available": llm is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/agents")
async def list_agents():
    """List discovered agents and their capabilities."""
    return {
        "supervisor_id": SUPERVISOR_ID,
        "agents": {
            domain: {
                "name": e.card.name if e.card else "unknown",
                "url": e.url,
                "healthy": e.healthy,
                "skills": [{"id": s.id, "name": s.name, "description": s.description}
                           for s in e.card.skills] if e.card else [],
            }
            for domain, e in agent_registry.items()
        },
    }


@app.get("/identity")
async def identity():
    return {
        "agent_id": SUPERVISOR_ID,
        "agent_name": SUPERVISOR_NAME,
        "agent_type": "supervisor",
        "protocol": "a2a",
        "managed_agents": list(agent_registry.keys()),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)