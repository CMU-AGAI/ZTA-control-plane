"""
Hotel MCP Server - ZTA Testbed Component
=========================================
MCP server that wraps the Hotel Booking Service, exposing hotel search
and booking capabilities as tools for LLM agents.
"""

import os
import httpx
import logging
from typing import Optional
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"hotel-mcp","message":"%(message)s"}'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

HOTEL_SERVICE_URL = os.getenv("HOTEL_SERVICE_URL", "http://localhost:8002")

# =============================================================================
# MCP Server Setup
# =============================================================================

mcp = FastMCP("Hotel Booking MCP Server")

http_client = httpx.AsyncClient(timeout=30.0)


# =============================================================================
# Helper Functions
# =============================================================================

async def call_hotel_service(
    method: str,
    endpoint: str,
    json_data: dict = None,
    params: dict = None
) -> dict:
    """Make HTTP request to the hotel service."""
    url = f"{HOTEL_SERVICE_URL}{endpoint}"
    
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
        return {"error": f"Failed to connect to hotel service: {str(e)}"}


# =============================================================================
# MCP Tools
# =============================================================================

@mcp.tool()
async def search_hotels(
    city_code: str,
    check_in_date: str,
    check_out_date: str,
    guests: int = 1,
    min_stars: int = 1
) -> str:
    """
    Search for available hotels in a city.
    
    Args:
        city_code: City code (3-letter airport IATA code, e.g., 'JFK' for New York, 'LAX' for Los Angeles)
        check_in_date: Check-in date in YYYY-MM-DD format
        check_out_date: Check-out date in YYYY-MM-DD format
        guests: Number of guests (1-10)
        min_stars: Minimum star rating (1-5)
    
    Returns:
        List of available hotels with room types and prices
    """
    logger.info(f"Searching hotels: {city_code}, {check_in_date} to {check_out_date}")
    
    result = await call_hotel_service(
        "POST",
        "/api/v1/hotels/search",
        json_data={
            "city_code": city_code.upper(),
            "check_in_date": check_in_date,
            "check_out_date": check_out_date,
            "guests": guests,
            "min_stars": min_stars
        }
    )
    
    if "error" in result:
        return f"Error searching hotels: {result['error']}"
    
    hotels = result.get("hotels", [])
    num_nights = result.get("num_nights", 0)
    
    if not hotels:
        return f"No hotels found in {city_code} for {check_in_date} to {check_out_date} with {min_stars}+ stars."
    
    output = f"Found {len(hotels)} hotel(s) in {city_code} for {num_nights} night(s):\n\n"
    
    for hotel in hotels:
        output += f"🏨 {hotel['name']} ({hotel['star_rating']}★)\n"
        output += f"   Chain: {hotel.get('chain') or 'Independent'}\n"
        output += f"   Address: {hotel['address']}\n"
        output += f"   Amenities: {', '.join(hotel.get('amenities', [])[:5])}\n"
        output += f"   Hotel ID: {hotel['hotel_id']}\n"
        output += f"   Room Options:\n"
        
        for room in hotel.get("room_types", []):
            output += f"      - {room['name']}: ${room['price_per_night']:.2f}/night (${room['total_price']:.2f} total)\n"
            output += f"        Max Occupancy: {room['max_occupancy']}, Available: {room['rooms_available']} rooms\n"
            output += f"        Room Type ID: {room['room_type_id']}\n"
        output += "\n"
    
    return output


@mcp.tool()
async def book_hotel(
    room_type_id: str,
    check_in_date: str,
    check_out_date: str,
    guest_name: str,
    guest_email: str,
    num_guests: int = 1,
    guest_phone: Optional[str] = None,
    special_requests: Optional[str] = None
) -> str:
    """
    Book a hotel room.
    
    Args:
        room_type_id: The room type ID from search results
        check_in_date: Check-in date in YYYY-MM-DD format
        check_out_date: Check-out date in YYYY-MM-DD format
        guest_name: Guest's full name
        guest_email: Guest's email address
        num_guests: Number of guests (default: 1)
        guest_phone: Guest's phone number (optional)
        special_requests: Any special requests (optional)
    
    Returns:
        Booking confirmation with confirmation number and details
    """
    logger.info(f"Booking hotel room {room_type_id} for {guest_name}")
    
    result = await call_hotel_service(
        "POST",
        "/api/v1/bookings",
        json_data={
            "room_type_id": room_type_id,
            "check_in_date": check_in_date,
            "check_out_date": check_out_date,
            "num_guests": num_guests,
            "guest_name": guest_name,
            "guest_email": guest_email,
            "guest_phone": guest_phone,
            "special_requests": special_requests
        }
    )
    
    if "error" in result:
        return f"Error booking hotel: {result['error']}"
    
    if not result.get("success"):
        return f"Booking failed: {result.get('error', 'Unknown error')}"
    
    booking = result["booking"]
    
    output = "✅ Hotel Booked Successfully!\n\n"
    output += f"Confirmation Number: {booking['confirmation_number']}\n"
    output += f"Booking ID: {booking['booking_id']}\n"
    output += f"Status: {booking['status']}\n\n"
    output += f"Hotel: {booking['hotel_name']}\n"
    output += f"Room Type: {booking['room_type']}\n"
    output += f"Check-in: {booking['check_in_date']}\n"
    output += f"Check-out: {booking['check_out_date']}\n"
    output += f"Nights: {booking['num_nights']}\n"
    output += f"Guests: {booking['num_guests']}\n\n"
    output += f"Guest: {booking['guest_name']}\n"
    output += f"Total Price: ${booking['total_price']:.2f} {booking['currency']}\n"
    
    return output


@mcp.tool()
async def get_hotel_booking(booking_id: str) -> str:
    """
    Retrieve hotel booking details by booking ID.
    
    Args:
        booking_id: The booking ID (UUID)
    
    Returns:
        Booking details
    """
    logger.info(f"Retrieving hotel booking: {booking_id}")
    
    result = await call_hotel_service("GET", f"/api/v1/bookings/{booking_id}")
    
    if "error" in result:
        return f"Error retrieving booking: {result['error']}"
    
    if not result.get("success"):
        return f"Booking not found: {result.get('error', 'Unknown error')}"
    
    booking = result["booking"]
    
    output = f"Hotel Booking Details\n"
    output += f"=====================\n"
    output += f"Confirmation: {booking['confirmation_number']}\n"
    output += f"Status: {booking['status']}\n"
    output += f"Hotel: {booking['hotel_name']}\n"
    output += f"Room: {booking['room_type']}\n"
    output += f"Check-in: {booking['check_in_date']}\n"
    output += f"Check-out: {booking['check_out_date']}\n"
    output += f"Guest: {booking['guest_name']}\n"
    output += f"Total: ${booking['total_price']:.2f}\n"
    
    return output


@mcp.tool()
async def get_hotel_booking_by_confirmation(confirmation_number: str) -> str:
    """
    Retrieve hotel booking details by confirmation number.
    
    Args:
        confirmation_number: The 8-character confirmation number
    
    Returns:
        Booking details
    """
    logger.info(f"Retrieving hotel booking by confirmation: {confirmation_number}")
    
    result = await call_hotel_service("GET", f"/api/v1/bookings/confirmation/{confirmation_number.upper()}")
    
    if "error" in result:
        return f"Error retrieving booking: {result['error']}"
    
    if not result.get("success"):
        return f"Booking not found: {result.get('error', 'Unknown error')}"
    
    booking = result["booking"]
    
    output = f"Hotel Booking Details (Confirmation: {confirmation_number.upper()})\n"
    output += f"================================================\n"
    output += f"Status: {booking['status']}\n"
    output += f"Hotel: {booking['hotel_name']}\n"
    output += f"Room: {booking['room_type']}\n"
    output += f"Check-in: {booking['check_in_date']}\n"
    output += f"Check-out: {booking['check_out_date']}\n"
    output += f"Guest: {booking['guest_name']}\n"
    output += f"Total: ${booking['total_price']:.2f}\n"
    
    return output


@mcp.tool()
async def cancel_hotel_booking(booking_id: str) -> str:
    """
    Cancel a hotel booking.
    
    Args:
        booking_id: The booking ID to cancel
    
    Returns:
        Cancellation confirmation
    """
    logger.info(f"Cancelling hotel booking: {booking_id}")
    
    result = await call_hotel_service("DELETE", f"/api/v1/bookings/{booking_id}")
    
    if "error" in result:
        return f"Error cancelling booking: {result['error']}"
    
    if result.get("success"):
        return f"✅ Hotel booking {booking_id} has been cancelled.\nConfirmation: {result.get('confirmation_number', 'N/A')}"
    else:
        return f"Failed to cancel booking: {result.get('message', 'Unknown error')}"


@mcp.tool()
async def list_cities() -> str:
    """
    Get a list of supported cities for hotel search.
    
    Returns:
        List of city codes and names
    """
    logger.info("Listing cities")
    
    result = await call_hotel_service("GET", "/api/v1/cities")
    
    if "error" in result:
        return f"Error listing cities: {result['error']}"
    
    cities = result.get("cities", [])
    
    output = "Supported Cities:\n"
    output += "=================\n"
    for city in cities:
        output += f"  {city['code']}: {city['city']}\n"
        output += f"    Areas: {', '.join(city.get('areas', []))}\n"
    
    return output


# =============================================================================
# MCP Resources
# =============================================================================

@mcp.resource("cities://list")
async def cities_resource() -> str:
    """Provide list of supported cities as a resource."""
    result = await call_hotel_service("GET", "/api/v1/cities")
    
    if "error" in result:
        return "Error loading cities"
    
    cities = result.get("cities", [])
    return "\n".join([f"{c['code']}: {c['city']}" for c in cities])


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import sys
    
    port = int(os.getenv("PORT", "8011"))
    logger.info(f"Starting Hotel MCP Server on port {port}")
    logger.info(f"Hotel Service URL: {HOTEL_SERVICE_URL}")
    
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