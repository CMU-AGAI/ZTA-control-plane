"""
Car Rental MCP Server - ZTA Testbed Component
==============================================
MCP server that wraps the Car Rental Service, exposing vehicle search
and rental booking capabilities as tools for LLM agents.
"""

import os
import httpx
import logging
from typing import Optional, List
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"car-rental-mcp","message":"%(message)s"}'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

CAR_RENTAL_SERVICE_URL = os.getenv("CAR_RENTAL_SERVICE_URL", "http://localhost:8003")

# =============================================================================
# MCP Server Setup
# =============================================================================

mcp = FastMCP("Car Rental MCP Server")

http_client = httpx.AsyncClient(timeout=30.0)


# =============================================================================
# Helper Functions
# =============================================================================

async def call_car_rental_service(
    method: str,
    endpoint: str,
    json_data: dict = None,
    params: dict = None
) -> dict:
    """Make HTTP request to the car rental service."""
    url = f"{CAR_RENTAL_SERVICE_URL}{endpoint}"
    
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": f"mcp-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
    }
    
    try:
        if method.upper() == "GET":
            response = await http_client.get(url, params=params, headers=headers)
        elif method.upper() == "POST":
            response = await http_client.post(url, json=json_data, headers=headers)
        elif method.upper() == "DELETE":
            response = await http_client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.json()
    
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error calling {endpoint}: {e.response.status_code}")
        error_detail = e.response.json().get("detail", str(e)) if e.response.content else str(e)
        return {"error": error_detail, "status_code": e.response.status_code}
    
    except httpx.RequestError as e:
        logger.error(f"Request error calling {endpoint}: {str(e)}")
        return {"error": f"Failed to connect to car rental service: {str(e)}"}


# =============================================================================
# MCP Tools
# =============================================================================

@mcp.tool()
async def search_vehicles(
    pickup_location_code: str,
    pickup_date: str,
    dropoff_date: str,
    dropoff_location_code: Optional[str] = None,
    category: Optional[str] = None
) -> str:
    """
    Search for available rental vehicles.
    
    Args:
        pickup_location_code: Pickup city code (3-letter airport IATA code, e.g., 'LAX', 'JFK')
        pickup_date: Pickup date in YYYY-MM-DD format
        dropoff_date: Dropoff date in YYYY-MM-DD format
        dropoff_location_code: Dropoff city code (optional, defaults to pickup location for round-trip)
        category: Vehicle category filter (optional): Economy, Compact, Midsize, Full-size, SUV, Large SUV, Premium, Convertible
    
    Returns:
        List of available vehicles with prices and features
    """
    logger.info(f"Searching vehicles: {pickup_location_code}, {pickup_date} to {dropoff_date}")
    
    request_data = {
        "pickup_location_code": pickup_location_code.upper(),
        "pickup_date": pickup_date,
        "dropoff_date": dropoff_date
    }
    if dropoff_location_code:
        request_data["dropoff_location_code"] = dropoff_location_code.upper()
    if category:
        request_data["category"] = category
    
    result = await call_car_rental_service("POST", "/api/v1/vehicles/search", json_data=request_data)
    
    if "error" in result:
        return f"Error searching vehicles: {result['error']}"
    
    vehicles = result.get("vehicles", [])
    num_days = result.get("num_days", 0)
    
    if not vehicles:
        return f"No vehicles found in {pickup_location_code} for {pickup_date} to {dropoff_date}."
    
    output = f"Found {len(vehicles)} vehicle(s) for {num_days} day(s):\n\n"
    
    for v in vehicles:
        output += f"🚗 {v['year']} {v['make']} {v['model']} ({v['category']})\n"
        output += f"   Company: {v['company']}\n"
        output += f"   Price: ${v['price_per_day']:.2f}/day (${v['total_price']:.2f} total)\n"
        output += f"   Passengers: {v['passengers']}, Luggage: {v['luggage']} bags\n"
        output += f"   Features: {', '.join(v.get('features', []))}\n"
        output += f"   Pickup: {v['pickup_location']}\n"
        output += f"   Dropoff: {v['dropoff_location']}\n"
        output += f"   Vehicle ID: {v['vehicle_id']}\n\n"
    
    return output


@mcp.tool()
async def book_vehicle(
    vehicle_id: str,
    pickup_date: str,
    dropoff_date: str,
    pickup_location_code: str,
    driver_name: str,
    driver_email: str,
    dropoff_location_code: Optional[str] = None,
    driver_phone: Optional[str] = None,
    driver_license: Optional[str] = None,
    add_ons: Optional[List[str]] = None
) -> str:
    """
    Book a rental vehicle.
    
    Args:
        vehicle_id: The vehicle ID from search results
        pickup_date: Pickup date in YYYY-MM-DD format
        dropoff_date: Dropoff date in YYYY-MM-DD format
        pickup_location_code: Pickup city code
        driver_name: Driver's full name
        driver_email: Driver's email address
        dropoff_location_code: Dropoff city code (optional, defaults to pickup)
        driver_phone: Driver's phone number (optional)
        driver_license: Driver's license number (optional)
        add_ons: List of add-ons (optional): 'gps', 'child_seat', 'additional_driver', 'insurance', 'roadside', 'wifi'
    
    Returns:
        Booking confirmation with confirmation number and details
    """
    logger.info(f"Booking vehicle {vehicle_id} for {driver_name}")
    
    request_data = {
        "vehicle_id": vehicle_id,
        "pickup_date": pickup_date,
        "dropoff_date": dropoff_date,
        "pickup_location_code": pickup_location_code.upper(),
        "driver_name": driver_name,
        "driver_email": driver_email
    }
    if dropoff_location_code:
        request_data["dropoff_location_code"] = dropoff_location_code.upper()
    if driver_phone:
        request_data["driver_phone"] = driver_phone
    if driver_license:
        request_data["driver_license"] = driver_license
    if add_ons:
        request_data["add_ons"] = add_ons
    
    result = await call_car_rental_service("POST", "/api/v1/rentals", json_data=request_data)
    
    if "error" in result:
        return f"Error booking vehicle: {result['error']}"
    
    if not result.get("success"):
        return f"Booking failed: {result.get('error', 'Unknown error')}"
    
    rental = result["rental"]
    
    output = "✅ Vehicle Booked Successfully!\n\n"
    output += f"Confirmation Number: {rental['confirmation_number']}\n"
    output += f"Rental ID: {rental['rental_id']}\n"
    output += f"Status: {rental['status']}\n\n"
    output += f"Vehicle: {rental['vehicle_info']}\n"
    output += f"Category: {rental['category']}\n"
    output += f"Company: {rental['company']}\n"
    output += f"Pickup: {rental['pickup_location']} on {rental['pickup_date']}\n"
    output += f"Dropoff: {rental['dropoff_location']} on {rental['dropoff_date']}\n"
    output += f"Duration: {rental['num_days']} day(s)\n\n"
    output += f"Driver: {rental['driver_name']}\n"
    if rental.get('add_ons'):
        output += f"Add-ons: {', '.join(rental['add_ons'])}\n"
    output += f"Total Price: ${rental['total_price']:.2f} {rental['currency']}\n"
    
    return output


@mcp.tool()
async def get_rental(rental_id: str) -> str:
    """
    Retrieve rental details by rental ID.
    
    Args:
        rental_id: The rental ID (UUID)
    
    Returns:
        Rental details
    """
    logger.info(f"Retrieving rental: {rental_id}")
    
    result = await call_car_rental_service("GET", f"/api/v1/rentals/{rental_id}")
    
    if "error" in result:
        return f"Error retrieving rental: {result['error']}"
    
    if not result.get("success"):
        return f"Rental not found: {result.get('error', 'Unknown error')}"
    
    rental = result["rental"]
    
    output = f"Rental Details\n"
    output += f"==============\n"
    output += f"Confirmation: {rental['confirmation_number']}\n"
    output += f"Status: {rental['status']}\n"
    output += f"Vehicle: {rental['vehicle_info']}\n"
    output += f"Company: {rental['company']}\n"
    output += f"Pickup: {rental['pickup_location']} on {rental['pickup_date']}\n"
    output += f"Dropoff: {rental['dropoff_location']} on {rental['dropoff_date']}\n"
    output += f"Driver: {rental['driver_name']}\n"
    output += f"Total: ${rental['total_price']:.2f}\n"
    
    return output


@mcp.tool()
async def get_rental_by_confirmation(confirmation_number: str) -> str:
    """
    Retrieve rental details by confirmation number.
    
    Args:
        confirmation_number: The 8-character confirmation number
    
    Returns:
        Rental details
    """
    logger.info(f"Retrieving rental by confirmation: {confirmation_number}")
    
    result = await call_car_rental_service("GET", f"/api/v1/rentals/confirmation/{confirmation_number.upper()}")
    
    if "error" in result:
        return f"Error retrieving rental: {result['error']}"
    
    if not result.get("success"):
        return f"Rental not found: {result.get('error', 'Unknown error')}"
    
    rental = result["rental"]
    
    output = f"Rental Details (Confirmation: {confirmation_number.upper()})\n"
    output += f"==========================================\n"
    output += f"Status: {rental['status']}\n"
    output += f"Vehicle: {rental['vehicle_info']}\n"
    output += f"Company: {rental['company']}\n"
    output += f"Pickup: {rental['pickup_location']} on {rental['pickup_date']}\n"
    output += f"Dropoff: {rental['dropoff_location']} on {rental['dropoff_date']}\n"
    output += f"Driver: {rental['driver_name']}\n"
    output += f"Total: ${rental['total_price']:.2f}\n"
    
    return output


@mcp.tool()
async def cancel_rental(rental_id: str) -> str:
    """
    Cancel a vehicle rental.
    
    Args:
        rental_id: The rental ID to cancel
    
    Returns:
        Cancellation confirmation
    """
    logger.info(f"Cancelling rental: {rental_id}")
    
    result = await call_car_rental_service("DELETE", f"/api/v1/rentals/{rental_id}")
    
    if "error" in result:
        return f"Error cancelling rental: {result['error']}"
    
    if result.get("success"):
        return f"✅ Rental {rental_id} has been cancelled.\nConfirmation: {result.get('confirmation_number', 'N/A')}"
    else:
        return f"Failed to cancel rental: {result.get('message', 'Unknown error')}"


@mcp.tool()
async def list_locations(city_code: Optional[str] = None) -> str:
    """
    Get list of rental locations.
    
    Args:
        city_code: Optional city code to filter locations (e.g., 'LAX', 'JFK')
    
    Returns:
        List of rental locations
    """
    logger.info(f"Listing locations: {city_code or 'all'}")
    
    params = {"city_code": city_code.upper()} if city_code else {}
    result = await call_car_rental_service("GET", "/api/v1/locations", params=params)
    
    if "error" in result:
        return f"Error listing locations: {result['error']}"
    
    locations = result.get("locations", [])
    
    output = "Rental Locations:\n"
    output += "=================\n"
    for loc in locations:
        output += f"  {loc['name']} ({loc['city_code']})\n"
        output += f"    Type: {loc['type']}, City: {loc['city']}\n"
        output += f"    Address: {loc['address']}\n"
    
    return output


@mcp.tool()
async def list_vehicle_categories() -> str:
    """
    Get list of available vehicle categories.
    
    Returns:
        List of vehicle category names
    """
    logger.info("Listing vehicle categories")
    
    result = await call_car_rental_service("GET", "/api/v1/categories")
    
    if "error" in result:
        return f"Error listing categories: {result['error']}"
    
    categories = result.get("categories", [])
    
    output = "Vehicle Categories:\n"
    output += "==================\n"
    for cat in categories:
        output += f"  • {cat}\n"
    
    return output


@mcp.tool()
async def list_add_ons() -> str:
    """
    Get list of available rental add-ons with prices.
    
    Returns:
        List of add-ons with descriptions and daily prices
    """
    logger.info("Listing add-ons")
    
    result = await call_car_rental_service("GET", "/api/v1/add-ons")
    
    if "error" in result:
        return f"Error listing add-ons: {result['error']}"
    
    add_ons = result.get("add_ons", [])
    
    output = "Available Add-ons:\n"
    output += "==================\n"
    for addon in add_ons:
        output += f"  • {addon['name']} (key: '{addon['key']}'): ${addon['price_per_day']:.2f}/day\n"
    
    return output


# =============================================================================
# MCP Resources
# =============================================================================

@mcp.resource("locations://list")
async def locations_resource() -> str:
    """Provide list of rental locations as a resource."""
    result = await call_car_rental_service("GET", "/api/v1/locations")
    
    if "error" in result:
        return "Error loading locations"
    
    locations = result.get("locations", [])
    return "\n".join([f"{loc['city_code']}: {loc['name']} ({loc['type']})" for loc in locations])


@mcp.resource("categories://list")
async def categories_resource() -> str:
    """Provide list of vehicle categories as a resource."""
    result = await call_car_rental_service("GET", "/api/v1/categories")
    
    if "error" in result:
        return "Error loading categories"
    
    return "\n".join(result.get("categories", []))


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import sys
    
    port = int(os.getenv("PORT", "8012"))
    logger.info(f"Starting Car Rental MCP Server on port {port}")
    logger.info(f"Car Rental Service URL: {CAR_RENTAL_SERVICE_URL}")
    
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        logger.info("Running with stdio transport")
        mcp.run(transport="stdio")
    else:
        # Run with streamable HTTP transport (for network access)
        logger.info(f"Running with SSE transport on port {port}")
        mcp.settings.port = port
        mcp.settings.host = "0.0.0.0"
        mcp.settings.transport_security = False  # Disable host validation
        mcp.run(transport="sse")