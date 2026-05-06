"""
Travel Planner - Intelligent Multi-Agent Orchestrator
======================================================
Routes user requests to appropriate agents with full context awareness.
Queries the Itinerary DB to understand user's trips and booking history.
"""

import os
import logging
import httpx
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

# OpenTelemetry
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

PORT = int(os.getenv("PORT", "8080"))
PLANNER_ID = os.getenv("PLANNER_ID", "travel-planner")
PLANNER_NAME = os.getenv("PLANNER_NAME", "Travel Planner")

# Agent URLs
AIRLINE_AGENT_URL = os.getenv("AIRLINE_AGENT_URL", "http://airline-agent:8091")
HOTEL_AGENT_URL = os.getenv("HOTEL_AGENT_URL", "http://hotel-agent:8092")
CAR_RENTAL_AGENT_URL = os.getenv("CAR_RENTAL_AGENT_URL", "http://car-rental-agent:8093")

# Itinerary Service URL
ITINERARY_SERVICE_URL = os.getenv("ITINERARY_SERVICE_URL", "http://itinerary-service:8084")

# LLM Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "travel-planner")

# =============================================================================
# OpenTelemetry Setup
# =============================================================================

resource = Resource.create({"service.name": SERVICE_NAME, "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

HTTPXClientInstrumentor().instrument()

# =============================================================================
# Intent Types
# =============================================================================

class IntentType(str, Enum):
    CREATE_TRIP = "create_trip"           # Start planning a new trip
    ADD_TO_TRIP = "add_to_trip"           # Add flight/hotel/car to existing trip
    MODIFY_BOOKING = "modify_booking"      # Change existing booking
    CANCEL_BOOKING = "cancel_booking"      # Cancel something
    QUERY_ITINERARY = "query_itinerary"   # Ask about existing bookings
    SEARCH = "search"                      # General search without booking intent
    GENERAL = "general"                    # General conversation

class DomainType(str, Enum):
    AIRLINE = "airline"
    HOTEL = "hotel"
    CAR_RENTAL = "car-rental"
    MULTI = "multi"                        # Needs multiple agents
    NONE = "none"                          # No specific domain

# =============================================================================
# Models
# =============================================================================

class ChatRequest(BaseModel):
    message: str
    user_id: Optional[str] = Field(default="11111111-1111-1111-1111-111111111111")
    conversation_id: Optional[str] = None
    trip_id: Optional[str] = None

class ChatResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    intent: Optional[str] = None
    domain: Optional[str] = None
    agent_used: Optional[str] = None
    tools_called: List[str] = []
    context_used: bool = False
    error: Optional[str] = None

class Intent(BaseModel):
    type: IntentType
    domain: DomainType
    confidence: float
    entities: Dict[str, Any] = {}

class UserContext(BaseModel):
    user: Optional[Dict[str, Any]] = None
    active_trip: Optional[Dict[str, Any]] = None
    all_trips: List[Dict[str, Any]] = []
    itinerary: List[Dict[str, Any]] = []
    recent_messages: List[Dict[str, Any]] = []

# =============================================================================
# Agent Registry
# =============================================================================

class AgentInfo(BaseModel):
    agent_id: str
    agent_name: str
    domain: str
    url: str
    tools: List[str] = []
    healthy: bool = False

agents: Dict[str, AgentInfo] = {
    "airline": AgentInfo(
        agent_id="airline-agent",
        agent_name="Airline Agent",
        domain="airline",
        url=AIRLINE_AGENT_URL
    ),
    "hotel": AgentInfo(
        agent_id="hotel-agent",
        agent_name="Hotel Agent",
        domain="hotel",
        url=HOTEL_AGENT_URL
    ),
    "car-rental": AgentInfo(
        agent_id="car-rental-agent",
        agent_name="Car Rental Agent",
        domain="car-rental",
        url=CAR_RENTAL_AGENT_URL
    )
}

# =============================================================================
# Lifespan
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {PLANNER_NAME} ({PLANNER_ID})")
    # Discover agent capabilities on startup
    await discover_agents()
    yield
    logger.info(f"Shutting down {PLANNER_NAME}")

# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Travel Planner",
    description="Intelligent multi-agent orchestrator with context awareness",
    version="1.0.0",
    lifespan=lifespan
)

FastAPIInstrumentor.instrument_app(app)

# =============================================================================
# Agent Discovery
# =============================================================================

async def discover_agents():
    """Discover capabilities of all registered agents"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        for domain, agent in agents.items():
            try:
                response = await client.get(f"{agent.url}/tools")
                if response.status_code == 200:
                    data = response.json()
                    # Handle both formats: {"tools": [...]} or [...]
                    if isinstance(data, dict):
                        agent.tools = [t.get("name", t) for t in data.get("tools", [])]
                    elif isinstance(data, list):
                        agent.tools = [t.get("name", t) if isinstance(t, dict) else t for t in data]
                    agent.healthy = True
                    logger.info(f"Discovered {agent.agent_name}: {len(agent.tools)} tools")
            except Exception as e:
                logger.warning(f"Failed to discover {agent.agent_name}: {e}")
                agent.healthy = False

async def check_agent_health(agent: AgentInfo) -> bool:
    """Check if an agent is healthy"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{agent.url}/health")
            return response.status_code == 200
    except:
        return False

# =============================================================================
# Itinerary Service Integration
# =============================================================================

async def get_user_context(user_id: str) -> Optional[UserContext]:
    """Get user context from Itinerary Service"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ITINERARY_SERVICE_URL}/api/v1/users/{user_id}/context"
            )
            if response.status_code == 200:
                data = response.json()
                return UserContext(**data)
            else:
                logger.warning(f"Failed to get user context: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Error getting user context: {e}")
        return None

async def create_trip(user_id: str, destination: str, name: str = None, 
                      start_date: str = None, end_date: str = None) -> Optional[Dict]:
    """Create a new trip in the Itinerary Service"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "user_id": user_id,
                "destination": destination,
                "name": name or f"Trip to {destination}",
            }
            if start_date:
                payload["start_date"] = start_date
            if end_date:
                payload["end_date"] = end_date
                
            response = await client.post(
                f"{ITINERARY_SERVICE_URL}/api/v1/trips",
                json=payload
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Failed to create trip: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Error creating trip: {e}")
        return None

async def add_itinerary_item(trip_id: str, item_type: str, details: Dict,
                             booking_reference: str = None, provider: str = None,
                             check_in: str = None, check_out: str = None,
                             price_cents: int = None) -> Optional[Dict]:
    """Add an item to a trip's itinerary"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "trip_id": trip_id,
                "item_type": item_type,
                "details": details,
                "status": "confirmed" if booking_reference else "pending"
            }
            if booking_reference:
                payload["booking_reference"] = booking_reference
            if provider:
                payload["provider"] = provider
            if check_in:
                payload["check_in"] = check_in
            if check_out:
                payload["check_out"] = check_out
            if price_cents:
                payload["price_cents"] = price_cents
                
            response = await client.post(
                f"{ITINERARY_SERVICE_URL}/api/v1/itinerary",
                json=payload
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Failed to add itinerary item: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Error adding itinerary item: {e}")
        return None

async def save_message(user_id: str, conversation_id: str, role: str, 
                       content: str, metadata: Dict = None) -> Optional[Dict]:
    """Save a message to conversation history"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ITINERARY_SERVICE_URL}/api/v1/conversations/{conversation_id}/messages",
                json={
                    "role": role,
                    "content": content,
                    "metadata": metadata or {}
                }
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        logger.warning(f"Error saving message: {e}")
    return None

# =============================================================================
# Intent Classification
# =============================================================================

def extract_destination(message: str) -> str:
    """
    Extract destination from user message.
    Handles patterns like:
    - "trip to Chicago"
    - "going to New York"
    - "travel to Los Angeles"
    - "vacation in Miami"
    - "visit Paris"
    """
    import re
    
    message_clean = message.strip()
    
    # Common city names to look for
    cities = [
        "New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
        "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
        "Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte",
        "San Francisco", "Indianapolis", "Seattle", "Denver", "Boston",
        "Miami", "Atlanta", "Las Vegas", "Orlando", "Tampa", "Portland",
        "Paris", "London", "Tokyo", "Sydney", "Dubai", "Singapore"
    ]
    
    # Check for known cities first (case-insensitive)
    for city in cities:
        if city.lower() in message.lower():
            return city
    
    # Pattern matching for "to [City]", "in [City]", "visit [City]"
    patterns = [
        r'trip to ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'going to ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'travel to ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'fly to ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'visit ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'vacation in ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'to ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, message_clean)
        if match:
            destination = match.group(1).strip()
            # Filter out common non-destination words
            if destination.lower() not in ['plan', 'book', 'search', 'find', 'help', 'want', 'need']:
                return destination
    
    # Fallback: look for capitalized words that might be cities
    words = message_clean.split()
    for i, word in enumerate(words):
        if word[0].isupper() and word.lower() not in ['i', 'would', 'like', 'want', 'need', 'please', 'can', 'could']:
            # Check if next word is also capitalized (two-word city)
            if i + 1 < len(words) and words[i + 1][0].isupper():
                return f"{word} {words[i + 1]}"
            return word
    
    return "Unknown"


def classify_intent(message: str, context: Optional[UserContext] = None) -> Intent:
    """
    Classify user intent based on message and context.
    Uses keyword matching for now, can be upgraded to LLM-based.
    """
    message_lower = message.lower()
    
    # Extract entities
    entities = {}
    
    # Determine domain
    domain = DomainType.NONE
    if any(word in message_lower for word in ['flight', 'fly', 'airport', 'airline', 'plane']):
        domain = DomainType.AIRLINE
    elif any(word in message_lower for word in ['hotel', 'room', 'stay', 'accommodation', 'lodge']):
        domain = DomainType.HOTEL
    elif any(word in message_lower for word in ['car', 'vehicle', 'rent', 'rental', 'drive']):
        domain = DomainType.CAR_RENTAL
    elif any(word in message_lower for word in ['trip', 'travel', 'vacation', 'journey']):
        domain = DomainType.MULTI
    
    # Determine intent type
    intent_type = IntentType.GENERAL
    
    # Query about existing bookings
    if any(word in message_lower for word in ['my booking', 'my flight', 'my hotel', 'my reservation', 
                                               'my trip', 'my itinerary', 'what time', 'when is',
                                               'show me', 'what do i have']):
        intent_type = IntentType.QUERY_ITINERARY
        
    # Cancel intent
    elif any(word in message_lower for word in ['cancel', 'delete', 'remove']):
        intent_type = IntentType.CANCEL_BOOKING
        
    # Modify intent
    elif any(word in message_lower for word in ['change', 'modify', 'update', 'reschedule']):
        intent_type = IntentType.MODIFY_BOOKING
        
    # Create new trip
    elif any(word in message_lower for word in ['plan a trip', 'planning a trip', 'new trip', 
                                                  'going to', 'want to go', 'need to go',
                                                  'traveling to', 'travel to']):
        intent_type = IntentType.CREATE_TRIP
        
    # Add to existing trip (if user has active trip)
    elif context and context.active_trip and any(word in message_lower for word in 
                                                   ['add', 'book', 'reserve', 'get me', 'find me',
                                                    'i need', 'i want']):
        intent_type = IntentType.ADD_TO_TRIP
        
    # General search
    elif any(word in message_lower for word in ['search', 'find', 'look for', 'show', 'list',
                                                 'available', 'options']):
        intent_type = IntentType.SEARCH
    
    # Book intent (could be add to trip or new)
    elif any(word in message_lower for word in ['book', 'reserve', 'purchase']):
        if context and context.active_trip:
            intent_type = IntentType.ADD_TO_TRIP
        else:
            intent_type = IntentType.CREATE_TRIP
    
    return Intent(
        type=intent_type,
        domain=domain,
        confidence=0.8,  # Placeholder, would be from LLM
        entities=entities
    )

# =============================================================================
# Response Formatting
# =============================================================================

def format_itinerary_response(context: UserContext) -> str:
    """Format itinerary as readable response"""
    if not context.active_trip:
        return "You don't have any active trips. Would you like me to help you plan one?"
    
    trip = context.active_trip
    lines = [f"**{trip.get('name', 'Your Trip')}**"]
    lines.append(f"Destination: {trip.get('destination', 'TBD')}")
    
    if trip.get('start_date') and trip.get('end_date'):
        lines.append(f"Dates: {trip['start_date']} to {trip['end_date']}")
    
    lines.append(f"Status: {trip.get('status', 'planning').title()}")
    lines.append("")
    
    if context.itinerary:
        lines.append("**Itinerary:**")
        for item in context.itinerary:
            item_type = item.get('item_type', '').title()
            status = item.get('status', 'pending').title()
            details = item.get('details', {})
            
            if item_type == 'Flight':
                flight_num = details.get('flight_number', 'N/A')
                origin = details.get('origin', '')
                dest = details.get('destination', '')
                lines.append(f"✈️ Flight {flight_num}: {origin} → {dest} [{status}]")
            elif item_type == 'Hotel':
                hotel_name = details.get('hotel_name', 'Hotel')
                lines.append(f"🏨 {hotel_name} [{status}]")
            elif item_type == 'Car_rental':
                lines.append(f"🚗 Car Rental [{status}]")
    else:
        lines.append("No bookings yet. What would you like to add?")
    
    return "\n".join(lines)

# =============================================================================
# Agent Communication
# =============================================================================

async def call_agent(domain: str, message: str, context: Optional[UserContext] = None) -> Dict:
    """Send a request to a worker agent"""
    agent = agents.get(domain)
    if not agent:
        return {"error": f"Unknown agent domain: {domain}"}
    
    if not agent.healthy:
        agent.healthy = await check_agent_health(agent)
        if not agent.healthy:
            return {"error": f"Agent {agent.agent_name} is not available"}
    
    # Build request with context
    request_data = {
        "message": message
    }
    
    # Add context if available
    if context and context.active_trip:
        # Convert Pydantic models to dicts safely
        trip_dict = context.active_trip.model_dump() if hasattr(context.active_trip, 'model_dump') else dict(context.active_trip)
        itinerary_list = [i.model_dump() if hasattr(i, 'model_dump') else dict(i) for i in context.itinerary]
        user_prefs = {}
        if context.user:
            user_prefs = context.user.preferences if hasattr(context.user, 'preferences') else context.user.get('preferences', {})
        
        request_data["context"] = {
            "trip": trip_dict,
            "itinerary": itinerary_list,
            "user_preferences": user_prefs
        }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{agent.url}/invoke",  # Changed from /process to /invoke
                json=request_data,
                headers={
                    "x-agent-id": PLANNER_ID,  # Source identity (travel-planner)
                    "x-supervisor-id": PLANNER_ID,  # Supervisor identity
                    "x-target-agent": agent.agent_id,  # Target agent
                    "Content-Type": "application/json"
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                # Extract message from agent response
                agent_message = result.get("message", "Request processed")
                agent_data = result.get("data")
                agent_tools = result.get("tools_called", [])
                agent_error = result.get("error")
                
                return {
                    "message": agent_message,
                    "data": agent_data,
                    "tools_called": agent_tools,
                    "error": agent_error,
                    "success": result.get("success", True)
                }
            else:
                return {"error": f"Agent returned status {response.status_code}"}
                
    except Exception as e:
        logger.error(f"Error calling agent {agent.agent_name}: {e}")
        return {"error": str(e)}

# =============================================================================
# Main Chat Endpoint
# =============================================================================

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Main chat endpoint - processes user messages with context awareness.
    """
    with tracer.start_as_current_span("chat") as span:
        span.set_attribute("user_id", request.user_id)
        span.set_attribute("message", request.message[:100])
        
        # 1. Get user context from Itinerary Service
        context = await get_user_context(request.user_id)
        context_used = context is not None
        
        if context:
            logger.info(f"Got context for user {request.user_id}: "
                       f"active_trip={context.active_trip is not None}, "
                       f"itinerary_items={len(context.itinerary)}")
        
        # 2. Classify intent with context
        intent = classify_intent(request.message, context)
        logger.info(f"Classified intent: type={intent.type}, domain={intent.domain}")
        
        span.set_attribute("intent_type", intent.type.value)
        span.set_attribute("intent_domain", intent.domain.value)
        
        # 3. Handle based on intent type
        
        # Query itinerary - no agent needed
        if intent.type == IntentType.QUERY_ITINERARY:
            if context:
                response_text = format_itinerary_response(context)
                return ChatResponse(
                    success=True,
                    message=response_text,
                    intent=intent.type.value,
                    domain=intent.domain.value,
                    context_used=True
                )
            else:
                return ChatResponse(
                    success=True,
                    message="I couldn't find any trip information. Would you like to plan a new trip?",
                    intent=intent.type.value,
                    context_used=False
                )
        
        # Create new trip
        if intent.type == IntentType.CREATE_TRIP:
            # Extract destination from message using better parsing
            destination = extract_destination(request.message)
            
            # Create trip in database
            trip = await create_trip(request.user_id, destination)
            
            if trip:
                return ChatResponse(
                    success=True,
                    message=f"I've started planning your trip to {destination}! "
                           f"Would you like me to search for flights, hotels, or both?",
                    data={"trip": trip},
                    intent=intent.type.value,
                    domain=intent.domain.value,
                    context_used=context_used
                )
            else:
                return ChatResponse(
                    success=False,
                    message="I had trouble creating your trip. Please try again.",
                    error="Failed to create trip",
                    intent=intent.type.value
                )
        
        # Route to appropriate agent
        if intent.domain in [DomainType.AIRLINE, DomainType.HOTEL, DomainType.CAR_RENTAL]:
            domain_str = intent.domain.value
            result = await call_agent(domain_str, request.message, context)
            
            # Check for actual errors (not None)
            if result.get("error"):
                return ChatResponse(
                    success=False,
                    message=f"I encountered an issue: {result['error']}",
                    error=result["error"],
                    intent=intent.type.value,
                    domain=domain_str,
                    agent_used=domain_str
                )
            
            # Check if agent indicated failure
            if not result.get("success", True):
                error_msg = result.get("error") or "Agent request failed"
                return ChatResponse(
                    success=False,
                    message=result.get("message", f"I encountered an issue: {error_msg}"),
                    error=error_msg,
                    data=result.get("data"),
                    intent=intent.type.value,
                    domain=domain_str,
                    agent_used=domain_str,
                    tools_called=result.get("tools_called", [])
                )
            
            # If this was a booking and we have an active trip, save to itinerary
            if intent.type == IntentType.ADD_TO_TRIP and context and context.active_trip:
                if result.get("booking"):
                    trip_id = context.active_trip.trip_id if hasattr(context.active_trip, 'trip_id') else context.active_trip.get("trip_id")
                    await add_itinerary_item(
                        trip_id=str(trip_id),
                        item_type=domain_str.replace("-", "_"),
                        details=result.get("booking", {}),
                        booking_reference=result.get("booking", {}).get("confirmation_code")
                    )
            
            return ChatResponse(
                success=True,
                message=result.get("message", "Request processed"),
                data=result.get("data"),
                intent=intent.type.value,
                domain=domain_str,
                agent_used=domain_str,
                tools_called=result.get("tools_called", []),
                context_used=context_used
            )
        
        # Multi-domain or general
        if intent.domain == DomainType.MULTI:
            return ChatResponse(
                success=True,
                message="I can help you plan your trip! What would you like to start with - flights, hotels, or car rental?",
                intent=intent.type.value,
                domain="multi",
                context_used=context_used
            )
        
        # General conversation
        return ChatResponse(
            success=True,
            message="I'm your Travel Planner! I can help you:\n"
                   "- Plan new trips\n"
                   "- Search for flights, hotels, and car rentals\n"
                   "- Manage your bookings\n"
                   "- View your itinerary\n\n"
                   "What would you like to do?",
            intent=intent.type.value,
            context_used=context_used
        )

# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/health")
async def health_check():
    """Health check with agent status"""
    agent_status = {}
    for domain, agent in agents.items():
        agent.healthy = await check_agent_health(agent)
        agent_status[domain] = {
            "healthy": agent.healthy,
            "tools_count": len(agent.tools)
        }
    
    # Check itinerary service
    itinerary_healthy = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{ITINERARY_SERVICE_URL}/health")
            itinerary_healthy = response.status_code == 200
    except:
        pass
    
    return {
        "status": "healthy",
        "planner_id": PLANNER_ID,
        "planner_name": PLANNER_NAME,
        "agents": agent_status,
        "itinerary_service": {"healthy": itinerary_healthy},
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/agents")
async def list_agents():
    """List all registered agents"""
    return {
        "planner_id": PLANNER_ID,
        "agents": {
            domain: {
                "agent_id": agent.agent_id,
                "agent_name": agent.agent_name,
                "domain": agent.domain,
                "tools": agent.tools,
                "healthy": agent.healthy
            }
            for domain, agent in agents.items()
        }
    }

@app.get("/context/{user_id}")
async def get_context(user_id: str):
    """Get user context (for debugging)"""
    context = await get_user_context(user_id)
    if context:
        return context.model_dump()
    raise HTTPException(status_code=404, detail="User context not found")

# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
