"""
Hotel Booking Service - ZTA Testbed Component
==============================================
A mock hotel reservation backend designed for zero-trust architecture testing.

Features:
- RESTful API for hotel search, room booking, and management
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
import json
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# Database imports
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey, Date
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

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
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"hotel-service","message":"%(message)s"}'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "hotel-service")
    SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
    
    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./hotel.db")
    
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
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class HotelDB(Base):
    __tablename__ = "hotels"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    chain = Column(String, nullable=True)  # Hotel chain (Marriott, Hilton, etc.)
    city = Column(String, nullable=False)
    city_code = Column(String(3), nullable=False)  # Airport code for the city
    address = Column(String, nullable=False)
    star_rating = Column(Integer, nullable=False)  # 1-5 stars
    amenities = Column(Text, nullable=True)  # JSON array of amenities
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    rooms = relationship("RoomTypeDB", back_populates="hotel")


class RoomTypeDB(Base):
    __tablename__ = "room_types"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    hotel_id = Column(String, ForeignKey("hotels.id"), nullable=False)
    name = Column(String, nullable=False)  # Standard, Deluxe, Suite, etc.
    description = Column(Text, nullable=True)
    base_price_per_night = Column(Float, nullable=False)
    max_occupancy = Column(Integer, default=2)
    total_rooms = Column(Integer, default=20)
    amenities = Column(Text, nullable=True)  # JSON array
    is_active = Column(Boolean, default=True)
    
    hotel = relationship("HotelDB", back_populates="rooms")
    bookings = relationship("BookingDB", back_populates="room_type")


class BookingDB(Base):
    __tablename__ = "bookings"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    confirmation_number = Column(String(8), unique=True, nullable=False, index=True)
    status = Column(String(20), default="confirmed")  # confirmed, cancelled, completed
    room_type_id = Column(String, ForeignKey("room_types.id"), nullable=False)
    check_in_date = Column(Date, nullable=False)
    check_out_date = Column(Date, nullable=False)
    num_nights = Column(Integer, nullable=False)
    num_guests = Column(Integer, default=1)
    guest_name = Column(String, nullable=False)
    guest_email = Column(String, nullable=False)
    guest_phone = Column(String, nullable=True)
    special_requests = Column(Text, nullable=True)
    total_price = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    room_type = relationship("RoomTypeDB", back_populates="bookings")


def get_db():
    """Dependency for getting database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def generate_confirmation_number() -> str:
    """Generate an 8-character confirmation number."""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choices(chars, k=8))


# =============================================================================
# OpenTelemetry Setup
# =============================================================================

def setup_telemetry():
    """Initialize OpenTelemetry tracing and metrics."""
    resource = Resource.create({
        "service.name": Config.SERVICE_NAME,
        "service.version": Config.SERVICE_VERSION,
    })
    
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(trace_provider)
    
    metric_reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=60000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    
    return trace.get_tracer(Config.SERVICE_NAME), metrics.get_meter(Config.SERVICE_NAME)

tracer, meter = setup_telemetry()

request_counter = meter.create_counter("hotel_requests_total", description="Total requests", unit="1")
booking_counter = meter.create_counter("hotel_bookings_total", description="Total bookings", unit="1")
latency_histogram = meter.create_histogram("hotel_request_duration_ms", description="Request duration", unit="ms")


# =============================================================================
# Pydantic Models (API Request/Response)
# =============================================================================

class HotelSearchRequest(BaseModel):
    city_code: str = Field(..., min_length=3, max_length=3, description="City code (airport IATA)")
    check_in_date: str = Field(..., description="Check-in date (YYYY-MM-DD)")
    check_out_date: str = Field(..., description="Check-out date (YYYY-MM-DD)")
    guests: int = Field(1, ge=1, le=10, description="Number of guests")
    min_stars: int = Field(1, ge=1, le=5, description="Minimum star rating")

class RoomType(BaseModel):
    room_type_id: str
    name: str
    description: Optional[str]
    price_per_night: float
    total_price: float
    max_occupancy: int
    amenities: List[str]
    rooms_available: int
    
    model_config = {"from_attributes": True}

class Hotel(BaseModel):
    hotel_id: str
    name: str
    chain: Optional[str]
    city: str
    city_code: str
    address: str
    star_rating: int
    amenities: List[str]
    room_types: List[RoomType]
    
    model_config = {"from_attributes": True}

class HotelSearchResponse(BaseModel):
    request_id: str
    hotels: List[Hotel]
    search_timestamp: str
    check_in_date: str
    check_out_date: str
    num_nights: int
    total_results: int

class BookingRequest(BaseModel):
    room_type_id: str
    check_in_date: str
    check_out_date: str
    num_guests: int = Field(1, ge=1, le=10)
    guest_name: str
    guest_email: str
    guest_phone: Optional[str] = None
    special_requests: Optional[str] = None
    payment_token: Optional[str] = Field(None, description="Payment token for ZTA validation")

class Booking(BaseModel):
    booking_id: str
    confirmation_number: str
    status: str
    hotel_name: str
    room_type: str
    check_in_date: str
    check_out_date: str
    num_nights: int
    num_guests: int
    guest_name: str
    total_price: float
    currency: str = "USD"
    created_at: str
    
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

HOTEL_CHAINS = [
    {"name": "Marriott", "prefix": "Marriott"},
    {"name": "Hilton", "prefix": "Hilton"},
    {"name": "Hyatt", "prefix": "Hyatt"},
    {"name": "IHG", "prefix": "Holiday Inn"},
    {"name": "Best Western", "prefix": "Best Western"},
    {"name": None, "prefix": "Grand"},  # Independent hotels
]

CITIES = {
    "JFK": {"city": "New York", "areas": ["Manhattan", "Times Square", "Brooklyn", "Midtown"]},
    "LAX": {"city": "Los Angeles", "areas": ["Hollywood", "Santa Monica", "Downtown", "Beverly Hills"]},
    "ORD": {"city": "Chicago", "areas": ["Downtown", "Magnificent Mile", "River North", "Loop"]},
    "SFO": {"city": "San Francisco", "areas": ["Union Square", "Fisherman's Wharf", "SOMA", "Financial District"]},
    "MIA": {"city": "Miami", "areas": ["South Beach", "Downtown", "Brickell", "Coconut Grove"]},
    "SEA": {"city": "Seattle", "areas": ["Downtown", "Pike Place", "Capitol Hill", "Belltown"]},
    "BOS": {"city": "Boston", "areas": ["Back Bay", "Downtown", "Beacon Hill", "Seaport"]},
    "DFW": {"city": "Dallas", "areas": ["Downtown", "Uptown", "Deep Ellum", "Arts District"]},
    "ATL": {"city": "Atlanta", "areas": ["Downtown", "Midtown", "Buckhead", "Decatur"]},
    "DEN": {"city": "Denver", "areas": ["Downtown", "LoDo", "Cherry Creek", "RiNo"]},
}

ROOM_TYPES = [
    {"name": "Standard Room", "base_price": 120, "max_occupancy": 2, "amenities": ["WiFi", "TV", "Coffee Maker"]},
    {"name": "Deluxe Room", "base_price": 180, "max_occupancy": 2, "amenities": ["WiFi", "TV", "Coffee Maker", "Mini Bar", "City View"]},
    {"name": "Executive Suite", "base_price": 280, "max_occupancy": 3, "amenities": ["WiFi", "TV", "Coffee Maker", "Mini Bar", "Living Area", "City View", "Lounge Access"]},
    {"name": "Family Room", "base_price": 220, "max_occupancy": 4, "amenities": ["WiFi", "TV", "Coffee Maker", "Two Beds", "Sofa Bed"]},
    {"name": "Presidential Suite", "base_price": 550, "max_occupancy": 4, "amenities": ["WiFi", "TV", "Kitchen", "Mini Bar", "Living Area", "Dining Area", "Panoramic View", "Butler Service"]},
]

HOTEL_AMENITIES = [
    "Free WiFi", "Pool", "Fitness Center", "Spa", "Restaurant", "Bar",
    "Room Service", "Business Center", "Concierge", "Valet Parking",
    "Airport Shuttle", "Pet Friendly", "EV Charging"
]


def seed_database(db: Session):
    """Seed the database with hotels and room types."""
    existing = db.query(HotelDB).count()
    if existing > 0:
        logger.info(f"Database already has {existing} hotels, skipping seed")
        return
    
    logger.info("Seeding database with hotels...")
    hotels_created = 0
    rooms_created = 0
    
    for city_code, city_info in CITIES.items():
        # Create 4-6 hotels per city
        num_hotels = random.randint(4, 6)
        
        for i in range(num_hotels):
            chain_info = random.choice(HOTEL_CHAINS)
            area = random.choice(city_info["areas"])
            star_rating = random.randint(3, 5)
            
            # Generate hotel name
            if chain_info["name"]:
                hotel_name = f"{chain_info['prefix']} {city_info['city']} {area}"
            else:
                hotel_name = f"{chain_info['prefix']} {area} Hotel"
            
            # Random amenities (5-10)
            num_amenities = random.randint(5, 10)
            amenities = random.sample(HOTEL_AMENITIES, num_amenities)
            
            hotel = HotelDB(
                name=hotel_name,
                chain=chain_info["name"],
                city=city_info["city"],
                city_code=city_code,
                address=f"{random.randint(100, 999)} {area} Street, {city_info['city']}",
                star_rating=star_rating,
                amenities=json.dumps(amenities),
            )
            db.add(hotel)
            db.flush()  # Get the hotel ID
            hotels_created += 1
            
            # Add room types (3-5 per hotel)
            available_room_types = random.sample(ROOM_TYPES, random.randint(3, 5))
            for room_type in available_room_types:
                # Price varies by star rating and random factor
                price_multiplier = 0.8 + (star_rating - 3) * 0.2 + random.uniform(-0.1, 0.2)
                
                room = RoomTypeDB(
                    hotel_id=hotel.id,
                    name=room_type["name"],
                    description=f"{room_type['name']} at {hotel_name}",
                    base_price_per_night=round(room_type["base_price"] * price_multiplier, 2),
                    max_occupancy=room_type["max_occupancy"],
                    total_rooms=random.randint(10, 30),
                    amenities=json.dumps(room_type["amenities"]),
                )
                db.add(room)
                rooms_created += 1
    
    db.commit()
    logger.info(f"Seeded {hotels_created} hotels with {rooms_created} room types")


def get_rooms_available(db: Session, room_type_id: str, check_in: datetime, check_out: datetime) -> int:
    """Calculate available rooms for a room type on given dates."""
    room_type = db.query(RoomTypeDB).filter(RoomTypeDB.id == room_type_id).first()
    if not room_type:
        return 0
    
    # Count overlapping confirmed bookings
    overlapping = db.query(BookingDB).filter(
        BookingDB.room_type_id == room_type_id,
        BookingDB.status == "confirmed",
        BookingDB.check_in_date < check_out.date(),
        BookingDB.check_out_date > check_in.date()
    ).count()
    
    return max(0, room_type.total_rooms - overlapping)


# =============================================================================
# Chaos Engineering Middleware
# =============================================================================

async def chaos_middleware(request: Request, call_next):
    """Inject latency and failures for chaos engineering tests."""
    if Config.CHAOS_ENABLED:
        if Config.CHAOS_LATENCY_MS > 0:
            jitter = random.uniform(0.8, 1.2)
            delay = (Config.CHAOS_LATENCY_MS * jitter) / 1000
            time.sleep(delay)
            logger.info(f"Chaos: Injected {delay*1000:.0f}ms latency")
        
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
    
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")
    
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    
    yield
    logger.info(f"Shutting down {Config.SERVICE_NAME}")


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="Hotel Booking Service",
    description="Mock hotel reservation backend for ZTA testbed",
    version=Config.SERVICE_VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(chaos_middleware)
FastAPIInstrumentor.instrument_app(app)


# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check(db: Session = Depends(get_db)):
    """Health check endpoint for Kubernetes probes."""
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
        checks={"database": db_status, "chaos_mode": Config.CHAOS_ENABLED}
    )


@app.get("/ready", tags=["Health"])
async def readiness_check():
    """Readiness probe for Kubernetes."""
    return {"ready": True}


@app.get("/live", tags=["Health"])
async def liveness_check():
    """Liveness probe for Kubernetes."""
    return {"alive": True}


@app.post("/api/v1/hotels/search", response_model=HotelSearchResponse, tags=["Hotels"])
async def search_hotels(
    search: HotelSearchRequest,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    x_trace_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Search for available hotels and rooms.
    """
    with tracer.start_as_current_span("search_hotels") as span:
        request_id = x_request_id or str(uuid.uuid4())
        span.set_attribute("request.id", request_id)
        span.set_attribute("search.city_code", search.city_code)
        
        # Validate city
        if search.city_code not in CITIES:
            raise HTTPException(status_code=400, detail=f"Unknown city code: {search.city_code}")
        
        # Parse dates
        try:
            check_in = datetime.strptime(search.check_in_date, "%Y-%m-%d")
            check_out = datetime.strptime(search.check_out_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        if check_out <= check_in:
            raise HTTPException(status_code=400, detail="Check-out must be after check-in")
        
        num_nights = (check_out - check_in).days
        
        # Query hotels
        hotels_db = db.query(HotelDB).filter(
            HotelDB.city_code == search.city_code,
            HotelDB.star_rating >= search.min_stars,
            HotelDB.is_active == True
        ).all()
        
        hotels = []
        for hotel_db in hotels_db:
            # Get room types with availability
            room_types = []
            for room_db in hotel_db.rooms:
                if room_db.max_occupancy >= search.guests and room_db.is_active:
                    available = get_rooms_available(db, room_db.id, check_in, check_out)
                    if available > 0:
                        room_types.append(RoomType(
                            room_type_id=room_db.id,
                            name=room_db.name,
                            description=room_db.description,
                            price_per_night=room_db.base_price_per_night,
                            total_price=round(room_db.base_price_per_night * num_nights, 2),
                            max_occupancy=room_db.max_occupancy,
                            amenities=json.loads(room_db.amenities) if room_db.amenities else [],
                            rooms_available=available
                        ))
            
            # Only include hotels with available rooms
            if room_types:
                hotels.append(Hotel(
                    hotel_id=hotel_db.id,
                    name=hotel_db.name,
                    chain=hotel_db.chain,
                    city=hotel_db.city,
                    city_code=hotel_db.city_code,
                    address=hotel_db.address,
                    star_rating=hotel_db.star_rating,
                    amenities=json.loads(hotel_db.amenities) if hotel_db.amenities else [],
                    room_types=sorted(room_types, key=lambda x: x.price_per_night)
                ))
        
        # Sort by star rating descending
        hotels.sort(key=lambda x: x.star_rating, reverse=True)
        
        logger.info(f"Hotel search: {search.city_code}, {num_nights} nights, found {len(hotels)} hotels")
        
        return HotelSearchResponse(
            request_id=request_id,
            hotels=hotels,
            search_timestamp=datetime.utcnow().isoformat(),
            check_in_date=search.check_in_date,
            check_out_date=search.check_out_date,
            num_nights=num_nights,
            total_results=len(hotels)
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
    Create a new hotel booking.
    """
    with tracer.start_as_current_span("create_booking") as span:
        request_id = x_request_id or str(uuid.uuid4())
        span.set_attribute("request.id", request_id)
        span.set_attribute("room_type.id", booking_request.room_type_id)
        
        # Validate room type exists
        room_type = db.query(RoomTypeDB).filter(RoomTypeDB.id == booking_request.room_type_id).first()
        if not room_type:
            raise HTTPException(status_code=404, detail="Room type not found")
        
        # Parse dates
        try:
            check_in = datetime.strptime(booking_request.check_in_date, "%Y-%m-%d")
            check_out = datetime.strptime(booking_request.check_out_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        if check_out <= check_in:
            raise HTTPException(status_code=400, detail="Check-out must be after check-in")
        
        num_nights = (check_out - check_in).days
        
        # Check occupancy
        if booking_request.num_guests > room_type.max_occupancy:
            raise HTTPException(
                status_code=400, 
                detail=f"Room max occupancy is {room_type.max_occupancy}, requested {booking_request.num_guests}"
            )
        
        # Check availability
        available = get_rooms_available(db, room_type.id, check_in, check_out)
        if available < 1:
            raise HTTPException(status_code=409, detail="No rooms available for selected dates")
        
        # Get hotel info
        hotel = db.query(HotelDB).filter(HotelDB.id == room_type.hotel_id).first()
        
        # Create booking
        confirmation = generate_confirmation_number()
        total_price = round(room_type.base_price_per_night * num_nights, 2)
        
        booking_db = BookingDB(
            confirmation_number=confirmation,
            status="confirmed",
            room_type_id=room_type.id,
            check_in_date=check_in.date(),
            check_out_date=check_out.date(),
            num_nights=num_nights,
            num_guests=booking_request.num_guests,
            guest_name=booking_request.guest_name,
            guest_email=booking_request.guest_email,
            guest_phone=booking_request.guest_phone,
            special_requests=booking_request.special_requests,
            total_price=total_price,
        )
        
        db.add(booking_db)
        db.commit()
        db.refresh(booking_db)
        
        booking_counter.add(1, {"hotel": hotel.name if hotel else "unknown"})
        logger.info(f"Booking created: {booking_db.id}, confirmation: {confirmation}, ${total_price}")
        
        booking = Booking(
            booking_id=booking_db.id,
            confirmation_number=booking_db.confirmation_number,
            status=booking_db.status,
            hotel_name=hotel.name if hotel else "Unknown",
            room_type=room_type.name,
            check_in_date=booking_request.check_in_date,
            check_out_date=booking_request.check_out_date,
            num_nights=num_nights,
            num_guests=booking_db.num_guests,
            guest_name=booking_db.guest_name,
            total_price=booking_db.total_price,
            currency=booking_db.currency,
            created_at=booking_db.created_at.isoformat()
        )
        
        return BookingResponse(success=True, booking=booking)


@app.get("/api/v1/bookings/{booking_id}", response_model=BookingResponse, tags=["Bookings"])
async def get_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """Retrieve booking details by ID."""
    with tracer.start_as_current_span("get_booking") as span:
        span.set_attribute("booking.id", booking_id)
        
        booking_db = db.query(BookingDB).filter(BookingDB.id == booking_id).first()
        if not booking_db:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        room_type = db.query(RoomTypeDB).filter(RoomTypeDB.id == booking_db.room_type_id).first()
        hotel = db.query(HotelDB).filter(HotelDB.id == room_type.hotel_id).first() if room_type else None
        
        booking = Booking(
            booking_id=booking_db.id,
            confirmation_number=booking_db.confirmation_number,
            status=booking_db.status,
            hotel_name=hotel.name if hotel else "Unknown",
            room_type=room_type.name if room_type else "Unknown",
            check_in_date=booking_db.check_in_date.isoformat(),
            check_out_date=booking_db.check_out_date.isoformat(),
            num_nights=booking_db.num_nights,
            num_guests=booking_db.num_guests,
            guest_name=booking_db.guest_name,
            total_price=booking_db.total_price,
            currency=booking_db.currency,
            created_at=booking_db.created_at.isoformat()
        )
        
        return BookingResponse(success=True, booking=booking)


@app.get("/api/v1/bookings/confirmation/{confirmation_number}", response_model=BookingResponse, tags=["Bookings"])
async def get_booking_by_confirmation(
    confirmation_number: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """Retrieve booking details by confirmation number."""
    with tracer.start_as_current_span("get_booking_by_confirmation") as span:
        span.set_attribute("booking.confirmation", confirmation_number)
        
        booking_db = db.query(BookingDB).filter(
            BookingDB.confirmation_number == confirmation_number.upper()
        ).first()
        if not booking_db:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        room_type = db.query(RoomTypeDB).filter(RoomTypeDB.id == booking_db.room_type_id).first()
        hotel = db.query(HotelDB).filter(HotelDB.id == room_type.hotel_id).first() if room_type else None
        
        booking = Booking(
            booking_id=booking_db.id,
            confirmation_number=booking_db.confirmation_number,
            status=booking_db.status,
            hotel_name=hotel.name if hotel else "Unknown",
            room_type=room_type.name if room_type else "Unknown",
            check_in_date=booking_db.check_in_date.isoformat(),
            check_out_date=booking_db.check_out_date.isoformat(),
            num_nights=booking_db.num_nights,
            num_guests=booking_db.num_guests,
            guest_name=booking_db.guest_name,
            total_price=booking_db.total_price,
            currency=booking_db.currency,
            created_at=booking_db.created_at.isoformat()
        )
        
        return BookingResponse(success=True, booking=booking)


@app.delete("/api/v1/bookings/{booking_id}", tags=["Bookings"])
async def cancel_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """Cancel an existing booking."""
    with tracer.start_as_current_span("cancel_booking") as span:
        span.set_attribute("booking.id", booking_id)
        
        booking_db = db.query(BookingDB).filter(BookingDB.id == booking_id).first()
        if not booking_db:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        booking_db.status = "cancelled"
        booking_db.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"Booking cancelled: {booking_id}")
        
        return {
            "success": True, 
            "message": "Booking cancelled", 
            "booking_id": booking_id,
            "confirmation_number": booking_db.confirmation_number
        }


@app.get("/api/v1/cities", tags=["Reference Data"])
async def list_cities():
    """Get list of supported cities."""
    return {
        "cities": [
            {"code": code, "city": info["city"], "areas": info["areas"]} 
            for code, info in CITIES.items()
        ]
    }


# =============================================================================
# Chaos Engineering Endpoints
# =============================================================================

@app.post("/chaos/enable", tags=["Chaos Engineering"])
async def enable_chaos(
    latency_ms: int = 0,
    failure_rate: float = 0.0,
    authorization: Optional[str] = Header(None)
):
    """Enable chaos engineering mode."""
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
    """Disable chaos engineering mode."""
    Config.CHAOS_ENABLED = False
    logger.info("Chaos mode DISABLED")
    return {"chaos_enabled": False}


@app.get("/chaos/status", tags=["Chaos Engineering"])
async def chaos_status():
    """Get current chaos engineering status."""
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
        port=int(os.getenv("PORT", "8002")),
        reload=os.getenv("ENV", "development") == "development",
        log_level="info"
    )
