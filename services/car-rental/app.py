"""
Car Rental Service - ZTA Testbed Component
============================================
A mock car rental backend designed for zero-trust architecture testing.

Features:
- RESTful API for vehicle search, rental booking, and management
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
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"car-rental-service","message":"%(message)s"}'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "car-rental-service")
    SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
    
    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./car_rental.db")
    
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


class LocationDB(Base):
    __tablename__ = "locations"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)  # "JFK Airport", "LAX Downtown"
    city = Column(String, nullable=False)
    city_code = Column(String(3), nullable=False)  # Airport IATA code
    address = Column(String, nullable=False)
    location_type = Column(String, nullable=False)  # airport, downtown, suburb
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    vehicles = relationship("VehicleDB", back_populates="location")


class VehicleDB(Base):
    __tablename__ = "vehicles"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    location_id = Column(String, ForeignKey("locations.id"), nullable=False)
    company = Column(String, nullable=False)  # Hertz, Enterprise, etc.
    category = Column(String, nullable=False)  # Economy, Compact, SUV, etc.
    make = Column(String, nullable=False)  # Toyota, Ford, etc.
    model = Column(String, nullable=False)  # Camry, Mustang, etc.
    year = Column(Integer, nullable=False)
    passengers = Column(Integer, default=5)
    luggage = Column(Integer, default=2)  # Number of bags
    features = Column(Text, nullable=True)  # JSON array
    price_per_day = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    
    location = relationship("LocationDB", back_populates="vehicles")
    rentals = relationship("RentalDB", back_populates="vehicle")


class RentalDB(Base):
    __tablename__ = "rentals"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    confirmation_number = Column(String(8), unique=True, nullable=False, index=True)
    status = Column(String(20), default="confirmed")  # confirmed, cancelled, completed, active
    vehicle_id = Column(String, ForeignKey("vehicles.id"), nullable=False)
    pickup_location_id = Column(String, ForeignKey("locations.id"), nullable=False)
    dropoff_location_id = Column(String, ForeignKey("locations.id"), nullable=False)
    pickup_date = Column(Date, nullable=False)
    dropoff_date = Column(Date, nullable=False)
    num_days = Column(Integer, nullable=False)
    driver_name = Column(String, nullable=False)
    driver_email = Column(String, nullable=False)
    driver_phone = Column(String, nullable=True)
    driver_license = Column(String, nullable=True)
    add_ons = Column(Text, nullable=True)  # JSON array: GPS, child seat, etc.
    total_price = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    vehicle = relationship("VehicleDB", back_populates="rentals")


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

request_counter = meter.create_counter("carrental_requests_total", description="Total requests", unit="1")
rental_counter = meter.create_counter("carrental_bookings_total", description="Total rentals", unit="1")
latency_histogram = meter.create_histogram("carrental_request_duration_ms", description="Request duration", unit="ms")


# =============================================================================
# Pydantic Models (API Request/Response)
# =============================================================================

class VehicleSearchRequest(BaseModel):
    pickup_location_code: str = Field(..., min_length=3, max_length=3, description="Pickup city code (airport IATA)")
    dropoff_location_code: Optional[str] = Field(None, description="Dropoff city code (defaults to pickup)")
    pickup_date: str = Field(..., description="Pickup date (YYYY-MM-DD)")
    dropoff_date: str = Field(..., description="Dropoff date (YYYY-MM-DD)")
    category: Optional[str] = Field(None, description="Vehicle category filter")

class Vehicle(BaseModel):
    vehicle_id: str
    company: str
    category: str
    make: str
    model: str
    year: int
    passengers: int
    luggage: int
    features: List[str]
    price_per_day: float
    total_price: float
    pickup_location: str
    dropoff_location: str
    
    model_config = {"from_attributes": True}

class VehicleSearchResponse(BaseModel):
    request_id: str
    vehicles: List[Vehicle]
    search_timestamp: str
    pickup_date: str
    dropoff_date: str
    num_days: int
    total_results: int

class RentalRequest(BaseModel):
    vehicle_id: str
    pickup_date: str
    dropoff_date: str
    pickup_location_code: str
    dropoff_location_code: Optional[str] = None
    driver_name: str
    driver_email: str
    driver_phone: Optional[str] = None
    driver_license: Optional[str] = None
    add_ons: Optional[List[str]] = Field(None, description="Add-ons: GPS, child_seat, insurance, etc.")
    payment_token: Optional[str] = Field(None, description="Payment token for ZTA validation")

class Rental(BaseModel):
    rental_id: str
    confirmation_number: str
    status: str
    vehicle_info: str  # "2024 Toyota Camry"
    company: str
    category: str
    pickup_location: str
    dropoff_location: str
    pickup_date: str
    dropoff_date: str
    num_days: int
    driver_name: str
    add_ons: List[str]
    total_price: float
    currency: str = "USD"
    created_at: str
    
    model_config = {"from_attributes": True}

class RentalResponse(BaseModel):
    success: bool
    rental: Optional[Rental] = None
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

RENTAL_COMPANIES = ["Hertz", "Enterprise", "Avis", "Budget", "National", "Alamo"]

CITIES = {
    "JFK": {"city": "New York", "locations": ["JFK Airport", "Manhattan Downtown", "Brooklyn"]},
    "LAX": {"city": "Los Angeles", "locations": ["LAX Airport", "Hollywood", "Santa Monica"]},
    "ORD": {"city": "Chicago", "locations": ["O'Hare Airport", "Downtown Chicago", "Midway"]},
    "SFO": {"city": "San Francisco", "locations": ["SFO Airport", "Union Square", "Oakland"]},
    "MIA": {"city": "Miami", "locations": ["MIA Airport", "Miami Beach", "Downtown Miami"]},
    "SEA": {"city": "Seattle", "locations": ["SeaTac Airport", "Downtown Seattle", "Bellevue"]},
    "BOS": {"city": "Boston", "locations": ["Logan Airport", "Back Bay", "Cambridge"]},
    "DFW": {"city": "Dallas", "locations": ["DFW Airport", "Downtown Dallas", "Fort Worth"]},
    "ATL": {"city": "Atlanta", "locations": ["ATL Airport", "Downtown Atlanta", "Buckhead"]},
    "DEN": {"city": "Denver", "locations": ["DEN Airport", "Downtown Denver", "Boulder"]},
}

VEHICLE_CATEGORIES = [
    {
        "category": "Economy",
        "vehicles": [
            {"make": "Toyota", "model": "Yaris", "passengers": 4, "luggage": 2},
            {"make": "Nissan", "model": "Versa", "passengers": 4, "luggage": 2},
            {"make": "Kia", "model": "Rio", "passengers": 4, "luggage": 2},
        ],
        "base_price": 35,
        "features": ["AC", "Bluetooth", "USB Charging"]
    },
    {
        "category": "Compact",
        "vehicles": [
            {"make": "Toyota", "model": "Corolla", "passengers": 5, "luggage": 2},
            {"make": "Honda", "model": "Civic", "passengers": 5, "luggage": 2},
            {"make": "Mazda", "model": "3", "passengers": 5, "luggage": 2},
        ],
        "base_price": 45,
        "features": ["AC", "Bluetooth", "USB Charging", "Backup Camera"]
    },
    {
        "category": "Midsize",
        "vehicles": [
            {"make": "Toyota", "model": "Camry", "passengers": 5, "luggage": 3},
            {"make": "Honda", "model": "Accord", "passengers": 5, "luggage": 3},
            {"make": "Nissan", "model": "Altima", "passengers": 5, "luggage": 3},
        ],
        "base_price": 55,
        "features": ["AC", "Bluetooth", "USB Charging", "Backup Camera", "Cruise Control"]
    },
    {
        "category": "Full-size",
        "vehicles": [
            {"make": "Chevrolet", "model": "Impala", "passengers": 5, "luggage": 4},
            {"make": "Dodge", "model": "Charger", "passengers": 5, "luggage": 4},
            {"make": "Chrysler", "model": "300", "passengers": 5, "luggage": 4},
        ],
        "base_price": 70,
        "features": ["AC", "Bluetooth", "USB Charging", "Backup Camera", "Cruise Control", "Leather Seats"]
    },
    {
        "category": "SUV",
        "vehicles": [
            {"make": "Toyota", "model": "RAV4", "passengers": 5, "luggage": 4},
            {"make": "Honda", "model": "CR-V", "passengers": 5, "luggage": 4},
            {"make": "Ford", "model": "Escape", "passengers": 5, "luggage": 4},
        ],
        "base_price": 75,
        "features": ["AC", "Bluetooth", "USB Charging", "Backup Camera", "AWD", "Roof Rails"]
    },
    {
        "category": "Large SUV",
        "vehicles": [
            {"make": "Chevrolet", "model": "Tahoe", "passengers": 7, "luggage": 5},
            {"make": "Ford", "model": "Expedition", "passengers": 7, "luggage": 5},
            {"make": "GMC", "model": "Yukon", "passengers": 7, "luggage": 5},
        ],
        "base_price": 110,
        "features": ["AC", "Bluetooth", "USB Charging", "Backup Camera", "AWD", "Third Row", "Towing Package"]
    },
    {
        "category": "Premium",
        "vehicles": [
            {"make": "BMW", "model": "5 Series", "passengers": 5, "luggage": 3},
            {"make": "Mercedes-Benz", "model": "E-Class", "passengers": 5, "luggage": 3},
            {"make": "Audi", "model": "A6", "passengers": 5, "luggage": 3},
        ],
        "base_price": 120,
        "features": ["AC", "Bluetooth", "USB Charging", "Backup Camera", "Leather Seats", "Navigation", "Premium Sound"]
    },
    {
        "category": "Convertible",
        "vehicles": [
            {"make": "Ford", "model": "Mustang Convertible", "passengers": 4, "luggage": 2},
            {"make": "Chevrolet", "model": "Camaro Convertible", "passengers": 4, "luggage": 2},
            {"make": "BMW", "model": "4 Series Convertible", "passengers": 4, "luggage": 2},
        ],
        "base_price": 95,
        "features": ["AC", "Bluetooth", "USB Charging", "Convertible Top", "Sport Mode"]
    },
]

ADD_ONS = {
    "gps": {"name": "GPS Navigation", "price_per_day": 10},
    "child_seat": {"name": "Child Seat", "price_per_day": 12},
    "additional_driver": {"name": "Additional Driver", "price_per_day": 15},
    "insurance": {"name": "Full Insurance Coverage", "price_per_day": 25},
    "roadside": {"name": "Roadside Assistance", "price_per_day": 8},
    "wifi": {"name": "Mobile WiFi Hotspot", "price_per_day": 15},
}


def seed_database(db: Session):
    """Seed the database with locations and vehicles."""
    existing = db.query(LocationDB).count()
    if existing > 0:
        logger.info(f"Database already has {existing} locations, skipping seed")
        return
    
    logger.info("Seeding database with locations and vehicles...")
    locations_created = 0
    vehicles_created = 0
    
    for city_code, city_info in CITIES.items():
        for location_name in city_info["locations"]:
            # Determine location type
            if "Airport" in location_name:
                location_type = "airport"
            elif "Downtown" in location_name:
                location_type = "downtown"
            else:
                location_type = "suburb"
            
            location = LocationDB(
                name=location_name,
                city=city_info["city"],
                city_code=city_code,
                address=f"{random.randint(100, 999)} {location_name.split()[-1]} Road, {city_info['city']}",
                location_type=location_type,
            )
            db.add(location)
            db.flush()
            locations_created += 1
            
            # Add vehicles for each company at this location
            for company in random.sample(RENTAL_COMPANIES, random.randint(3, 5)):
                # Add 2-4 vehicle categories per company per location
                for cat_info in random.sample(VEHICLE_CATEGORIES, random.randint(2, 4)):
                    vehicle_info = random.choice(cat_info["vehicles"])
                    
                    # Price varies by location type and random factor
                    price_multiplier = 1.0
                    if location_type == "airport":
                        price_multiplier = 1.15  # Airport premium
                    elif location_type == "downtown":
                        price_multiplier = 1.05
                    
                    price_multiplier *= random.uniform(0.9, 1.1)
                    
                    vehicle = VehicleDB(
                        location_id=location.id,
                        company=company,
                        category=cat_info["category"],
                        make=vehicle_info["make"],
                        model=vehicle_info["model"],
                        year=random.randint(2022, 2025),
                        passengers=vehicle_info["passengers"],
                        luggage=vehicle_info["luggage"],
                        features=json.dumps(cat_info["features"]),
                        price_per_day=round(cat_info["base_price"] * price_multiplier, 2),
                    )
                    db.add(vehicle)
                    vehicles_created += 1
    
    db.commit()
    logger.info(f"Seeded {locations_created} locations with {vehicles_created} vehicles")


def get_vehicle_availability(db: Session, vehicle_id: str, pickup_date: datetime, dropoff_date: datetime) -> bool:
    """Check if vehicle is available for the given date range."""
    overlapping = db.query(RentalDB).filter(
        RentalDB.vehicle_id == vehicle_id,
        RentalDB.status.in_(["confirmed", "active"]),
        RentalDB.pickup_date < dropoff_date.date(),
        RentalDB.dropoff_date > pickup_date.date()
    ).count()
    
    return overlapping == 0


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
    title="Car Rental Service",
    description="Mock car rental backend for ZTA testbed",
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


@app.post("/api/v1/vehicles/search", response_model=VehicleSearchResponse, tags=["Vehicles"])
async def search_vehicles(
    search: VehicleSearchRequest,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    x_trace_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Search for available rental vehicles.
    """
    with tracer.start_as_current_span("search_vehicles") as span:
        request_id = x_request_id or str(uuid.uuid4())
        span.set_attribute("request.id", request_id)
        span.set_attribute("search.pickup_location", search.pickup_location_code)
        
        # Validate locations
        if search.pickup_location_code not in CITIES:
            raise HTTPException(status_code=400, detail=f"Unknown pickup location: {search.pickup_location_code}")
        
        dropoff_code = search.dropoff_location_code or search.pickup_location_code
        if dropoff_code not in CITIES:
            raise HTTPException(status_code=400, detail=f"Unknown dropoff location: {dropoff_code}")
        
        # Parse dates
        try:
            pickup_date = datetime.strptime(search.pickup_date, "%Y-%m-%d")
            dropoff_date = datetime.strptime(search.dropoff_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        if dropoff_date <= pickup_date:
            raise HTTPException(status_code=400, detail="Dropoff date must be after pickup date")
        
        num_days = (dropoff_date - pickup_date).days
        
        # Get pickup locations
        pickup_locations = db.query(LocationDB).filter(
            LocationDB.city_code == search.pickup_location_code,
            LocationDB.is_active == True
        ).all()
        
        dropoff_locations = db.query(LocationDB).filter(
            LocationDB.city_code == dropoff_code,
            LocationDB.is_active == True
        ).all()
        
        if not pickup_locations:
            raise HTTPException(status_code=404, detail="No pickup locations found")
        
        # Query vehicles at pickup locations
        query = db.query(VehicleDB).filter(
            VehicleDB.location_id.in_([loc.id for loc in pickup_locations]),
            VehicleDB.is_active == True
        )
        
        if search.category:
            query = query.filter(VehicleDB.category == search.category)
        
        vehicles_db = query.all()
        
        # Filter by availability and build response
        vehicles = []
        for v in vehicles_db:
            if get_vehicle_availability(db, v.id, pickup_date, dropoff_date):
                pickup_loc = next((loc for loc in pickup_locations if loc.id == v.location_id), None)
                dropoff_loc = dropoff_locations[0] if dropoff_locations else pickup_loc
                
                vehicles.append(Vehicle(
                    vehicle_id=v.id,
                    company=v.company,
                    category=v.category,
                    make=v.make,
                    model=v.model,
                    year=v.year,
                    passengers=v.passengers,
                    luggage=v.luggage,
                    features=json.loads(v.features) if v.features else [],
                    price_per_day=v.price_per_day,
                    total_price=round(v.price_per_day * num_days, 2),
                    pickup_location=pickup_loc.name if pickup_loc else "Unknown",
                    dropoff_location=dropoff_loc.name if dropoff_loc else "Unknown"
                ))
        
        # Sort by price
        vehicles.sort(key=lambda x: x.total_price)
        
        logger.info(f"Vehicle search: {search.pickup_location_code}, {num_days} days, found {len(vehicles)} vehicles")
        
        return VehicleSearchResponse(
            request_id=request_id,
            vehicles=vehicles,
            search_timestamp=datetime.utcnow().isoformat(),
            pickup_date=search.pickup_date,
            dropoff_date=search.dropoff_date,
            num_days=num_days,
            total_results=len(vehicles)
        )


@app.post("/api/v1/rentals", response_model=RentalResponse, tags=["Rentals"])
async def create_rental(
    rental_request: RentalRequest,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    x_trace_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """
    Create a new car rental booking.
    """
    with tracer.start_as_current_span("create_rental") as span:
        request_id = x_request_id or str(uuid.uuid4())
        span.set_attribute("request.id", request_id)
        span.set_attribute("vehicle.id", rental_request.vehicle_id)
        
        # Validate vehicle exists
        vehicle = db.query(VehicleDB).filter(VehicleDB.id == rental_request.vehicle_id).first()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Vehicle not found")
        
        # Parse dates
        try:
            pickup_date = datetime.strptime(rental_request.pickup_date, "%Y-%m-%d")
            dropoff_date = datetime.strptime(rental_request.dropoff_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        if dropoff_date <= pickup_date:
            raise HTTPException(status_code=400, detail="Dropoff date must be after pickup date")
        
        num_days = (dropoff_date - pickup_date).days
        
        # Check availability
        if not get_vehicle_availability(db, vehicle.id, pickup_date, dropoff_date):
            raise HTTPException(status_code=409, detail="Vehicle not available for selected dates")
        
        # Get locations
        pickup_location = db.query(LocationDB).filter(
            LocationDB.city_code == rental_request.pickup_location_code,
            LocationDB.is_active == True
        ).first()
        
        dropoff_code = rental_request.dropoff_location_code or rental_request.pickup_location_code
        dropoff_location = db.query(LocationDB).filter(
            LocationDB.city_code == dropoff_code,
            LocationDB.is_active == True
        ).first()
        
        if not pickup_location:
            raise HTTPException(status_code=404, detail="Pickup location not found")
        if not dropoff_location:
            raise HTTPException(status_code=404, detail="Dropoff location not found")
        
        # Calculate total price with add-ons
        base_price = vehicle.price_per_day * num_days
        addon_price = 0
        addon_list = []
        
        if rental_request.add_ons:
            for addon_key in rental_request.add_ons:
                if addon_key in ADD_ONS:
                    addon_info = ADD_ONS[addon_key]
                    addon_price += addon_info["price_per_day"] * num_days
                    addon_list.append(addon_info["name"])
        
        total_price = round(base_price + addon_price, 2)
        
        # Create rental
        confirmation = generate_confirmation_number()
        
        rental_db = RentalDB(
            confirmation_number=confirmation,
            status="confirmed",
            vehicle_id=vehicle.id,
            pickup_location_id=pickup_location.id,
            dropoff_location_id=dropoff_location.id,
            pickup_date=pickup_date.date(),
            dropoff_date=dropoff_date.date(),
            num_days=num_days,
            driver_name=rental_request.driver_name,
            driver_email=rental_request.driver_email,
            driver_phone=rental_request.driver_phone,
            driver_license=rental_request.driver_license,
            add_ons=json.dumps(rental_request.add_ons) if rental_request.add_ons else None,
            total_price=total_price,
        )
        
        db.add(rental_db)
        db.commit()
        db.refresh(rental_db)
        
        rental_counter.add(1, {"company": vehicle.company, "category": vehicle.category})
        logger.info(f"Rental created: {rental_db.id}, confirmation: {confirmation}, ${total_price}")
        
        rental = Rental(
            rental_id=rental_db.id,
            confirmation_number=rental_db.confirmation_number,
            status=rental_db.status,
            vehicle_info=f"{vehicle.year} {vehicle.make} {vehicle.model}",
            company=vehicle.company,
            category=vehicle.category,
            pickup_location=pickup_location.name,
            dropoff_location=dropoff_location.name,
            pickup_date=rental_request.pickup_date,
            dropoff_date=rental_request.dropoff_date,
            num_days=num_days,
            driver_name=rental_db.driver_name,
            add_ons=addon_list,
            total_price=rental_db.total_price,
            currency=rental_db.currency,
            created_at=rental_db.created_at.isoformat()
        )
        
        return RentalResponse(success=True, rental=rental)


@app.get("/api/v1/rentals/{rental_id}", response_model=RentalResponse, tags=["Rentals"])
async def get_rental(
    rental_id: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """Retrieve rental details by ID."""
    with tracer.start_as_current_span("get_rental") as span:
        span.set_attribute("rental.id", rental_id)
        
        rental_db = db.query(RentalDB).filter(RentalDB.id == rental_id).first()
        if not rental_db:
            raise HTTPException(status_code=404, detail="Rental not found")
        
        vehicle = db.query(VehicleDB).filter(VehicleDB.id == rental_db.vehicle_id).first()
        pickup_loc = db.query(LocationDB).filter(LocationDB.id == rental_db.pickup_location_id).first()
        dropoff_loc = db.query(LocationDB).filter(LocationDB.id == rental_db.dropoff_location_id).first()
        
        addon_list = []
        if rental_db.add_ons:
            for addon_key in json.loads(rental_db.add_ons):
                if addon_key in ADD_ONS:
                    addon_list.append(ADD_ONS[addon_key]["name"])
        
        rental = Rental(
            rental_id=rental_db.id,
            confirmation_number=rental_db.confirmation_number,
            status=rental_db.status,
            vehicle_info=f"{vehicle.year} {vehicle.make} {vehicle.model}" if vehicle else "Unknown",
            company=vehicle.company if vehicle else "Unknown",
            category=vehicle.category if vehicle else "Unknown",
            pickup_location=pickup_loc.name if pickup_loc else "Unknown",
            dropoff_location=dropoff_loc.name if dropoff_loc else "Unknown",
            pickup_date=rental_db.pickup_date.isoformat(),
            dropoff_date=rental_db.dropoff_date.isoformat(),
            num_days=rental_db.num_days,
            driver_name=rental_db.driver_name,
            add_ons=addon_list,
            total_price=rental_db.total_price,
            currency=rental_db.currency,
            created_at=rental_db.created_at.isoformat()
        )
        
        return RentalResponse(success=True, rental=rental)


@app.get("/api/v1/rentals/confirmation/{confirmation_number}", response_model=RentalResponse, tags=["Rentals"])
async def get_rental_by_confirmation(
    confirmation_number: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """Retrieve rental details by confirmation number."""
    with tracer.start_as_current_span("get_rental_by_confirmation") as span:
        span.set_attribute("rental.confirmation", confirmation_number)
        
        rental_db = db.query(RentalDB).filter(
            RentalDB.confirmation_number == confirmation_number.upper()
        ).first()
        if not rental_db:
            raise HTTPException(status_code=404, detail="Rental not found")
        
        vehicle = db.query(VehicleDB).filter(VehicleDB.id == rental_db.vehicle_id).first()
        pickup_loc = db.query(LocationDB).filter(LocationDB.id == rental_db.pickup_location_id).first()
        dropoff_loc = db.query(LocationDB).filter(LocationDB.id == rental_db.dropoff_location_id).first()
        
        addon_list = []
        if rental_db.add_ons:
            for addon_key in json.loads(rental_db.add_ons):
                if addon_key in ADD_ONS:
                    addon_list.append(ADD_ONS[addon_key]["name"])
        
        rental = Rental(
            rental_id=rental_db.id,
            confirmation_number=rental_db.confirmation_number,
            status=rental_db.status,
            vehicle_info=f"{vehicle.year} {vehicle.make} {vehicle.model}" if vehicle else "Unknown",
            company=vehicle.company if vehicle else "Unknown",
            category=vehicle.category if vehicle else "Unknown",
            pickup_location=pickup_loc.name if pickup_loc else "Unknown",
            dropoff_location=dropoff_loc.name if dropoff_loc else "Unknown",
            pickup_date=rental_db.pickup_date.isoformat(),
            dropoff_date=rental_db.dropoff_date.isoformat(),
            num_days=rental_db.num_days,
            driver_name=rental_db.driver_name,
            add_ons=addon_list,
            total_price=rental_db.total_price,
            currency=rental_db.currency,
            created_at=rental_db.created_at.isoformat()
        )
        
        return RentalResponse(success=True, rental=rental)


@app.delete("/api/v1/rentals/{rental_id}", tags=["Rentals"])
async def cancel_rental(
    rental_id: str,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    """Cancel an existing rental."""
    with tracer.start_as_current_span("cancel_rental") as span:
        span.set_attribute("rental.id", rental_id)
        
        rental_db = db.query(RentalDB).filter(RentalDB.id == rental_id).first()
        if not rental_db:
            raise HTTPException(status_code=404, detail="Rental not found")
        
        rental_db.status = "cancelled"
        rental_db.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"Rental cancelled: {rental_id}")
        
        return {
            "success": True,
            "message": "Rental cancelled",
            "rental_id": rental_id,
            "confirmation_number": rental_db.confirmation_number
        }


@app.get("/api/v1/locations", tags=["Reference Data"])
async def list_locations(
    city_code: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get list of rental locations."""
    query = db.query(LocationDB).filter(LocationDB.is_active == True)
    
    if city_code:
        query = query.filter(LocationDB.city_code == city_code)
    
    locations = query.all()
    
    return {
        "locations": [
            {
                "id": loc.id,
                "name": loc.name,
                "city": loc.city,
                "city_code": loc.city_code,
                "address": loc.address,
                "type": loc.location_type
            }
            for loc in locations
        ]
    }


@app.get("/api/v1/categories", tags=["Reference Data"])
async def list_categories():
    """Get list of vehicle categories."""
    return {
        "categories": [cat["category"] for cat in VEHICLE_CATEGORIES]
    }


@app.get("/api/v1/add-ons", tags=["Reference Data"])
async def list_add_ons():
    """Get list of available add-ons."""
    return {
        "add_ons": [
            {"key": key, "name": info["name"], "price_per_day": info["price_per_day"]}
            for key, info in ADD_ONS.items()
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
        port=int(os.getenv("PORT", "8003")),
        reload=os.getenv("ENV", "development") == "development",
        log_level="info"
    )
