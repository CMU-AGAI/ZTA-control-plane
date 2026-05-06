"""
Airline Reservation Service - ZTA Testbed Component
=====================================================
A mock airline booking backend designed for zero-trust architecture testing.

Features:
- RESTful API for flight search, booking, and management
- SQLite database for persistent storage
- OpenTelemetry instrumentation for distributed tracing
- Chaos engineering hooks (latency injection, failure simulation)
- Health endpoints for Kubernetes probes
- Structured logging for security audit trails
"""

import os
import random
import uuid
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# Database imports
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import json

# OpenTelemetry imports
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"airline-service","message":"%(message)s"}'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "airline-service")
    SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
    
    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./airline.db")
    
    # Chaos engineering toggles
    CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
    CHAOS_LATENCY_MS = int(os.getenv("CHAOS_LATENCY_MS", "0"))
    CHAOS_FAILURE_RATE = float(os.getenv("CHAOS_FAILURE_RATE", "0.0"))
    
    # Rate limiting (for load shedding tests)
    MAX_REQUESTS_PER_SECOND = int(os.getenv("MAX_RPS", "100"))

# =============================================================================
# Database Setup
# =============================================================================

engine = create_engine(
    Config.DATABASE_URL, 
    connect_args={"check_same_thread": False}  # Needed for SQLite
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Database Models
class FlightDB(Base):
    __tablename__ = "flights"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    airline = Column(String, nullable=False)
    airline_code = Column(String(2), nullable=False)
    flight_number = Column(String(10), nullable=False)
    origin = Column(String(3), nullable=False)
    destination = Column(String(3), nullable=False)
    departure_time = Column(DateTime, nullable=False)
    arrival_time = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    base_price = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    cabin_class = Column(String(20), nullable=False)
    total_seats = Column(Integer, default=150)
    aircraft = Column(String(50), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    bookings = relationship("BookingDB", back_populates="flight")


class BookingDB(Base):
    __tablename__ = "bookings"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    pnr = Column(String(6), unique=True, nullable=False, index=True)
    status = Column(String(20), default="confirmed")  # confirmed, cancelled, completed
    flight_id = Column(String, ForeignKey("flights.id"), nullable=False)
    passengers_json = Column(Text, nullable=False)  # JSON string of passenger list
    contact_email = Column(String, nullable=False)
    contact_phone = Column(String, nullable=True)
    total_price = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    
    flight = relationship("FlightDB", back_populates="bookings")


def get_db():
    """Dependency for getting database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def generate_pnr() -> str:
    """Generate a 6-character PNR code."""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choices(chars, k=6))

# =============================================================================
# OpenTelemetry Setup
# =============================================================================

def setup_telemetry():
    """Initialize OpenTelemetry tracing and metrics."""
    resource = Resource.create({
        "service.name": Config.SERVICE_NAME,
        "service.version": Config.SERVICE_VERSION,
    })
    
    # Tracing
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(trace_provider)
    
    # Metrics
    metric_reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=60000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    
    return trace.get_tracer(Config.SERVICE_NAME), metrics.get_meter(Config.SERVICE_NAME)

tracer, meter = setup_telemetry()

# Custom metrics
request_counter = meter.create_counter(
    "airline_requests_total",
    description="Total number of requests",
    unit="1"
)
booking_counter = meter.create_counter(
    "airline_bookings_total",
    description="Total number of bookings created",
    unit="1"
)
latency_histogram = meter.create_histogram(
    "airline_request_duration_ms",
    description="Request duration in milliseconds",
    unit="ms"
)

# =============================================================================
# Pydantic Models (API Request/Response)
# =============================================================================

class FlightSearchRequest(BaseModel):
    origin: str = Field(..., min_length=3, max_length=3, description="Origin airport code (IATA)")
    destination: str = Field(..., min_length=3, max_length=3, description="Destination airport code (IATA)")
    departure_date: str = Field(..., description="Departure date (YYYY-MM-DD)")
    return_date: Optional[str] = Field(None, description="Return date for round trips")
    passengers: int = Field(1, ge=1, le=9, description="Number of passengers")
    cabin_class: str = Field("economy", description="Cabin class: economy, business, first")

class Flight(BaseModel):
    flight_id: str
    airline: str
    airline_code: str
    flight_number: str
    origin: str
    destination: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    price: float
    currency: str = "USD"
    cabin_class: str
    seats_available: int
    aircraft: str
    
    model_config = {"from_attributes": True}

class FlightSearchResponse(BaseModel):
    request_id: str
    flights: List[Flight]
    search_timestamp: str
    total_results: int

class BookingRequest(BaseModel):
    flight_id: str
    passengers: List[dict] = Field(..., description="List of passenger details")
    contact_email: str
    contact_phone: Optional[str] = None
    payment_token: Optional[str] = Field(None, description="Payment token for ZTA validation")

class Booking(BaseModel):
    booking_id: str
    pnr: str
    status: str
    flight_id: str
    flight_details: Optional[Flight] = None
    passengers: List[dict]
    total_price: float
    currency: str = "USD"
    created_at: str
    expires_at: Optional[str] = None
    
    model_config = {"from_attributes": True}

class BookingResponse(BaseModel):
    success: bool
    booking: Optional[Booking] = None
    error: Optional[str] = None

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    timestamp: str
    checks: dict

# =============================================================================
# Reference Data & Database Seeding
# =============================================================================

AIRLINES = [
    {"name": "SkyWings Airlines", "code": "SW", "aircraft": ["Boeing 737-800", "Airbus A320"]},
    {"name": "Global Express", "code": "GX", "aircraft": ["Boeing 787-9", "Airbus A350"]},
    {"name": "Pacific Air", "code": "PA", "aircraft": ["Boeing 777-300", "Airbus A330"]},
    {"name": "Continental Connect", "code": "CC", "aircraft": ["Embraer E190", "Boeing 737 MAX"]},
]

AIRPORTS = {
    "JFK": "New York JFK",
    "LAX": "Los Angeles",
    "ORD": "Chicago O'Hare",
    "SFO": "San Francisco",
    "MIA": "Miami",
    "SEA": "Seattle",
    "BOS": "Boston",
    "DFW": "Dallas/Fort Worth",
    "ATL": "Atlanta",
    "DEN": "Denver",
}

# Common routes for seeding
ROUTES = [
    ("JFK", "LAX"), ("LAX", "JFK"),
    ("JFK", "SFO"), ("SFO", "JFK"),
    ("ORD", "LAX"), ("LAX", "ORD"),
    ("JFK", "MIA"), ("MIA", "JFK"),
    ("SEA", "LAX"), ("LAX", "SEA"),
    ("BOS", "DFW"), ("DFW", "BOS"),
    ("ATL", "DEN"), ("DEN", "ATL"),
    ("SFO", "ORD"), ("ORD", "SFO"),
]


def seed_flights(db: Session):
    """Seed the database with flights for the next 30 days."""
    # Check if we already have flights
    existing = db.query(FlightDB).count()
    if existing > 0:
        logger.info(f"Database already has {existing} flights, skipping seed")
        return
    
    logger.info("Seeding database with flights...")
    base_prices = {"economy": 150, "business": 450, "first": 900}
    
    # Generate flights for the next 30 days
    start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    flights_created = 0
    for day_offset in range(30):
        flight_date = start_date + timedelta(days=day_offset)
        
        for origin, destination in ROUTES:
            # Generate 3-5 flights per route per day
            num_flights = random.randint(3, 5)
            
            for _ in range(num_flights):
                airline = random.choice(AIRLINES)
                
                for cabin_class, base_price in base_prices.items():
                    # Random departure time between 6am and 10pm
                    hour = random.randint(6, 22)
                    minute = random.choice([0, 15, 30, 45])
                    departure = flight_date.replace(hour=hour, minute=minute)
                    
                    # Flight duration 2-6 hours
                    duration = random.randint(120, 360)
                    arrival = departure + timedelta(minutes=duration)
                    
                    # Price variation
                    price = base_price * (1 + random.uniform(-0.2, 0.4))
                    
                    flight = FlightDB(
                        airline=airline["name"],
                        airline_code=airline["code"],
                        flight_number=f"{airline['code']}{random.randint(100, 999)}",
                        origin=origin,
                        destination=destination,
                        departure_time=departure,
                        arrival_time=arrival,
                        duration_minutes=duration,
                        base_price=round(price, 2),
                        cabin_class=cabin_class,
                        total_seats=random.randint(100, 200),
                        aircraft=random.choice(airline["aircraft"]),
                    )
                    db.add(flight)
                    flights_created += 1
    
    db.commit()
    logger.info(f"Seeded {flights_created} flights")


def get_seats_available(db: Session, flight_id: str) -> int:
    """Calculate available seats for a flight."""
    flight = db.query(FlightDB).filter(FlightDB.id == flight_id).first()
    if not flight:
        return 0
    
    # Count booked passengers
    booked = db.query(BookingDB).filter(
        BookingDB.flight_id == flight_id,
        BookingDB.status == "confirmed"
    ).all()
    
    booked_seats = sum(len(json.loads(b.passengers_json)) for b in booked)
    return max(0, flight.total_seats - booked_seats)


def flight_db_to_response(flight: FlightDB, seats_available: int) -> Flight:
    """Convert database flight to API response model."""
    return Flight(
        flight_id=flight.id,
        airline=flight.airline,
        airline_code=flight.airline_code,
        flight_number=flight.flight_number,
        origin=flight.origin,
        destination=flight.destination,
        departure_time=flight.departure_time.isoformat(),
        arrival_time=flight.arrival_time.isoformat(),
        duration_minutes=flight.duration_minutes,
        price=flight.base_price,
        currency=flight.currency,
        cabin_class=flight.cabin_class,
        seats_available=seats_available,
        aircraft=flight.aircraft,
    )

# =============================================================================
# Chaos Engineering Middleware
# =============================================================================

async def chaos_middleware(request: Request, call_next):
    """Inject latency and failures for chaos engineering tests."""
    if Config.CHAOS_ENABLED:
        # Latency injection
        if Config.CHAOS_LATENCY_MS > 0:
            jitter = random.uniform(0.8, 1.2)
            delay = (Config.CHAOS_LATENCY_MS * jitter) / 1000
            time.sleep(delay)
            logger.info(f"Chaos: Injected {delay*1000:.0f}ms latency")
        
        # Random failure injection
        if random.random() < Config.CHAOS_FAILURE_RATE:
            logger.warning("Chaos: Injecting random failure")
            return Response(
                content='{"error": "Chaos engineering: simulated failure"}',
                status_code=503,
                media_type="application/json"
            )
    
    start_time = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000
    
    # Record metrics
    request_counter.add(1, {"endpoint": request.url.path, "method": request.method})
    latency_histogram.record(duration_ms, {"endpoint": request.url.path})
    
    return response

# =============================================================================
# Application Lifecycle
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    logger.info(f"Starting {Config.SERVICE_NAME} v{Config.SERVICE_VERSION}")
    logger.info(f"Database: {Config.DATABASE_URL}")
    logger.info(f"Chaos engineering: {'ENABLED' if Config.CHAOS_ENABLED else 'DISABLED'}")
    
    # Create tables
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")
    
    # Seed flights
    db = SessionLocal()
    try:
        seed_flights(db)
    finally:
        db.close()
    
    yield
    logger.info(f"Shutting down {Config.SERVICE_NAME}")

# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="Airline Reservation Service",
    description="Mock airline booking backend for ZTA testbed",
    version=Config.SERVICE_VERSION,
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add chaos middleware
app.middleware("http")(chaos_middleware)

# Instrument with OpenTelemetry
FastAPIInstrumentor.instrument_app(app)

# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check(db: Session = Depends(get_db)):
    """
    Health check endpoint for Kubernetes probes.
    Returns service status and diagnostic information.
    """
    # Check database connectivity
    db_status = "healthy"
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    return HealthResponse(
        status="healthy" if db_status == "healthy" else "degraded",
        service=Config.SERVICE_NAME,
        version=Config.SERVICE_VERSION,
        timestamp=datetime.utcnow().isoformat(),
        checks={
            "database": db_status,
            "chaos_mode": Config.CHAOS_ENABLED
        }
    )

@app.get("/ready", tags=["Health"])
async def readiness_check():
    """Readiness probe for Kubernetes."""
    return {"ready": True}

@app.get("/live", tags=["Health"])
async def liveness_check():
    """Liveness probe for Kubernetes."""
    return {"alive": True}


@app.post("/api/v1/flights/search", response_model=FlightSearchResponse, tags=["Flights"])
async def search_flights(
    search: FlightSearchRequest,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    x_trace_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Search for available flights.
    
    This endpoint supports distributed tracing via X-Request-ID and X-Trace-ID headers,
    which are essential for ZTA audit logging.
    """
    with tracer.start_as_current_span("search_flights") as span:
        request_id = x_request_id or str(uuid.uuid4())
        span.set_attribute("request.id", request_id)
        span.set_attribute("search.origin", search.origin)
        span.set_attribute("search.destination", search.destination)
        
        # Validate airports
        if search.origin not in AIRPORTS:
            raise HTTPException(status_code=400, detail=f"Unknown origin airport: {search.origin}")
        if search.destination not in AIRPORTS:
            raise HTTPException(status_code=400, detail=f"Unknown destination airport: {search.destination}")
        
        # Parse search date
        try:
            search_date = datetime.strptime(search.departure_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        # Query flights from database
        flights_db = db.query(FlightDB).filter(
            FlightDB.origin == search.origin,
            FlightDB.destination == search.destination,
            FlightDB.cabin_class == search.cabin_class,
            FlightDB.departure_time >= search_date,
            FlightDB.departure_time < search_date + timedelta(days=1),
            FlightDB.is_active == True
        ).order_by(FlightDB.base_price).all()
        
        # Convert to response format with availability
        flights = []
        for f in flights_db:
            seats = get_seats_available(db, f.id)
            if seats >= search.passengers:
                flights.append(flight_db_to_response(f, seats))
        
        logger.info(f"Flight search: {search.origin}->{search.destination} on {search.departure_date}, found {len(flights)} flights")
        
        return FlightSearchResponse(
            request_id=request_id,
            flights=flights,
            search_timestamp=datetime.utcnow().isoformat(),
            total_results=len(flights)
        )


@app.post("/api/v1/bookings", response_model=BookingResponse, tags=["Bookings"])
async def create_booking(
    booking_request: BookingRequest,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    x_trace_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Create a new flight booking.
    
    Requires a valid flight_id from a previous search.
    The payment_token field is used for ZTA validation testing.
    """
    with tracer.start_as_current_span("create_booking") as span:
        request_id = x_request_id or str(uuid.uuid4())
        span.set_attribute("request.id", request_id)
        span.set_attribute("flight.id", booking_request.flight_id)
        
        # Validate flight exists
        flight = db.query(FlightDB).filter(FlightDB.id == booking_request.flight_id).first()
        if not flight:
            logger.warning(f"Booking attempt for unknown flight: {booking_request.flight_id}")
            raise HTTPException(status_code=404, detail="Flight not found")
        
        # Check availability
        seats_available = get_seats_available(db, flight.id)
        if seats_available < len(booking_request.passengers):
            raise HTTPException(status_code=409, detail=f"Not enough seats available. Requested: {len(booking_request.passengers)}, Available: {seats_available}")
        
        # Create booking
        pnr = generate_pnr()
        total_price = flight.base_price * len(booking_request.passengers)
        
        booking_db = BookingDB(
            pnr=pnr,
            status="confirmed",
            flight_id=flight.id,
            passengers_json=json.dumps(booking_request.passengers),
            contact_email=booking_request.contact_email,
            contact_phone=booking_request.contact_phone,
            total_price=round(total_price, 2),
            expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        
        db.add(booking_db)
        db.commit()
        db.refresh(booking_db)
        
        booking_counter.add(1, {"cabin_class": flight.cabin_class})
        logger.info(f"Booking created: {booking_db.id}, PNR: {pnr}, amount: ${total_price:.2f}")
        
        # Build response
        booking = Booking(
            booking_id=booking_db.id,
            pnr=booking_db.pnr,
            status=booking_db.status,
            flight_id=booking_db.flight_id,
            flight_details=flight_db_to_response(flight, seats_available - len(booking_request.passengers)),
            passengers=booking_request.passengers,
            total_price=booking_db.total_price,
            currency=booking_db.currency,
            created_at=booking_db.created_at.isoformat(),
            expires_at=booking_db.expires_at.isoformat() if booking_db.expires_at else None
        )
        
        return BookingResponse(success=True, booking=booking)


@app.get("/api/v1/bookings/{booking_id}", response_model=BookingResponse, tags=["Bookings"])
async def get_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Retrieve booking details by ID.
    """
    with tracer.start_as_current_span("get_booking") as span:
        span.set_attribute("booking.id", booking_id)
        
        booking_db = db.query(BookingDB).filter(BookingDB.id == booking_id).first()
        if not booking_db:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        flight = db.query(FlightDB).filter(FlightDB.id == booking_db.flight_id).first()
        seats_available = get_seats_available(db, flight.id) if flight else 0
        
        booking = Booking(
            booking_id=booking_db.id,
            pnr=booking_db.pnr,
            status=booking_db.status,
            flight_id=booking_db.flight_id,
            flight_details=flight_db_to_response(flight, seats_available) if flight else None,
            passengers=json.loads(booking_db.passengers_json),
            total_price=booking_db.total_price,
            currency=booking_db.currency,
            created_at=booking_db.created_at.isoformat(),
            expires_at=booking_db.expires_at.isoformat() if booking_db.expires_at else None
        )
        
        return BookingResponse(success=True, booking=booking)


@app.get("/api/v1/bookings/pnr/{pnr}", response_model=BookingResponse, tags=["Bookings"])
async def get_booking_by_pnr(
    pnr: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Retrieve booking details by PNR code.
    """
    with tracer.start_as_current_span("get_booking_by_pnr") as span:
        span.set_attribute("booking.pnr", pnr)
        
        booking_db = db.query(BookingDB).filter(BookingDB.pnr == pnr.upper()).first()
        if not booking_db:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        flight = db.query(FlightDB).filter(FlightDB.id == booking_db.flight_id).first()
        seats_available = get_seats_available(db, flight.id) if flight else 0
        
        booking = Booking(
            booking_id=booking_db.id,
            pnr=booking_db.pnr,
            status=booking_db.status,
            flight_id=booking_db.flight_id,
            flight_details=flight_db_to_response(flight, seats_available) if flight else None,
            passengers=json.loads(booking_db.passengers_json),
            total_price=booking_db.total_price,
            currency=booking_db.currency,
            created_at=booking_db.created_at.isoformat(),
            expires_at=booking_db.expires_at.isoformat() if booking_db.expires_at else None
        )
        
        return BookingResponse(success=True, booking=booking)


@app.delete("/api/v1/bookings/{booking_id}", tags=["Bookings"])
async def cancel_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Cancel an existing booking.
    """
    with tracer.start_as_current_span("cancel_booking") as span:
        span.set_attribute("booking.id", booking_id)
        
        booking_db = db.query(BookingDB).filter(BookingDB.id == booking_id).first()
        if not booking_db:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        booking_db.status = "cancelled"
        booking_db.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"Booking cancelled: {booking_id}")
        
        return {"success": True, "message": "Booking cancelled", "booking_id": booking_id, "pnr": booking_db.pnr}


@app.get("/api/v1/airports", tags=["Reference Data"])
async def list_airports():
    """
    Get list of supported airports.
    """
    return {"airports": [{"code": k, "name": v} for k, v in AIRPORTS.items()]}


@app.get("/api/v1/airlines", tags=["Reference Data"])
async def list_airlines():
    """
    Get list of airlines.
    """
    return {"airlines": AIRLINES}


# =============================================================================
# Chaos Engineering Endpoints (for testing)
# =============================================================================

@app.post("/chaos/enable", tags=["Chaos Engineering"])
async def enable_chaos(
    latency_ms: int = 0,
    failure_rate: float = 0.0,
    authorization: Optional[str] = Header(None)
):
    """
    Enable chaos engineering mode.
    This endpoint should be protected by the ZTA control plane.
    """
    Config.CHAOS_ENABLED = True
    Config.CHAOS_LATENCY_MS = latency_ms
    Config.CHAOS_FAILURE_RATE = min(1.0, max(0.0, failure_rate))
    
    logger.warning(f"Chaos mode ENABLED: latency={latency_ms}ms, failure_rate={failure_rate}")
    
    return {
        "chaos_enabled": True,
        "latency_ms": Config.CHAOS_LATENCY_MS,
        "failure_rate": Config.CHAOS_FAILURE_RATE
    }


@app.post("/chaos/disable", tags=["Chaos Engineering"])
async def disable_chaos(authorization: Optional[str] = Header(None)):
    """
    Disable chaos engineering mode.
    """
    Config.CHAOS_ENABLED = False
    logger.info("Chaos mode DISABLED")
    return {"chaos_enabled": False}


@app.get("/chaos/status", tags=["Chaos Engineering"])
async def chaos_status():
    """
    Get current chaos engineering status.
    """
    return {
        "chaos_enabled": Config.CHAOS_ENABLED,
        "latency_ms": Config.CHAOS_LATENCY_MS,
        "failure_rate": Config.CHAOS_FAILURE_RATE
    }


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        reload=os.getenv("ENV", "development") == "development",
        log_level="info"
    )
