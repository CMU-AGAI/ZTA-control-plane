"""
Airline MCP Server - ZTA Testbed Component
============================================
MCP server that wraps the Airline Reservation Service, exposing flight search
and booking capabilities as tools for LLM agents.

Uses FastMCP with SSE transport for agent connectivity.
"""

import os
import httpx
import logging
from typing import Optional
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP, Context

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"airline-mcp","message":"%(message)s"}'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

AIRLINE_SERVICE_URL = os.getenv("AIRLINE_SERVICE_URL", "http://localhost:8001")

# =============================================================================
# MCP Server Setup
# =============================================================================

mcp = FastMCP("Airline Reservation MCP Server")

# HTTP client for calling the airline service
http_client = httpx.AsyncClient(timeout=30.0)


# =============================================================================
# Helper Functions
# =============================================================================

async def call_airline_service(
    method: str,
    endpoint: str,
    json_data: dict = None,
    params: dict = None,
    headers: dict = None
) -> dict:
    """Make HTTP request to the airline service."""
    url = f"{AIRLINE_SERVICE_URL}{endpoint}"
    
    request_headers = {
        "Content-Type": "application/json",
        "X-Request-ID": f"mcp-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
    }
    if headers:
        request_headers.update(headers)
    
    try:
        if method.upper() == "GET":
            response = await http_client.get(url, params=params, headers=request_headers)
        elif method.upper() == "POST":
            response = await http_client.post(url, json=json_data, headers=request_headers)
        elif method.upper() == "DELETE":
            response = await http_client.delete(url, headers=request_headers)
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
        return {"error": f"Failed to connect to airline service: {str(e)}"}


# =============================================================================
# MCP Tools
# =============================================================================

@mcp.tool()
async def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    passengers: int = 1,
    cabin_class: str = "economy"
) -> str:
    """
    Search for available flights between two airports.
    
    Args:
        origin: Origin airport code (3-letter IATA code, e.g., 'JFK', 'LAX')
        destination: Destination airport code (3-letter IATA code)
        departure_date: Departure date in YYYY-MM-DD format
        passengers: Number of passengers (1-9)
        cabin_class: Cabin class - 'economy', 'business', or 'first'
    
    Returns:
        JSON string with available flights including flight numbers, times, prices, and availability
    """
    logger.info(f"Searching flights: {origin} -> {destination} on {departure_date}")
    
    result = await call_airline_service(
        "POST",
        "/api/v1/flights/search",
        json_data={
            "origin": origin.upper(),
            "destination": destination.upper(),
            "departure_date": departure_date,
            "passengers": passengers,
            "cabin_class": cabin_class.lower()
        }
    )
    
    if "error" in result:
        return f"Error searching flights: {result['error']}"
    
    flights = result.get("flights", [])
    if not flights:
        return f"No flights found from {origin} to {destination} on {departure_date} in {cabin_class} class."
    
    output = f"Found {len(flights)} flight(s) from {origin} to {destination} on {departure_date}:\n\n"
    
    for i, flight in enumerate(flights, 1):
        output += f"{i}. {flight['airline']} {flight['flight_number']}\n"
        output += f"   Departure: {flight['departure_time']}\n"
        output += f"   Arrival: {flight['arrival_time']}\n"
        output += f"   Duration: {flight['duration_minutes']} minutes\n"
        output += f"   Price: ${flight['price']:.2f} per person ({cabin_class})\n"
        output += f"   Seats Available: {flight['seats_available']}\n"
        output += f"   Aircraft: {flight['aircraft']}\n"
        output += f"   Flight ID: {flight['flight_id']}\n\n"
    
    return output


@mcp.tool()
async def book_flight(
    flight_id: str,
    passenger_first_name: str,
    passenger_last_name: str,
    passenger_email: str,
    passenger_phone: Optional[str] = None
) -> str:
    """
    Book a flight for a passenger.
    
    Args:
        flight_id: The flight ID from search results
        passenger_first_name: Passenger's first name
        passenger_last_name: Passenger's last name
        passenger_email: Passenger's email address
        passenger_phone: Passenger's phone number (optional)
    
    Returns:
        Booking confirmation with PNR code and details
    """
    logger.info(f"Booking flight {flight_id} for {passenger_first_name} {passenger_last_name}")
    
    passengers = [{
        "first_name": passenger_first_name,
        "last_name": passenger_last_name,
        "email": passenger_email,
    }]
    if passenger_phone:
        passengers[0]["phone"] = passenger_phone
    
    result = await call_airline_service(
        "POST",
        "/api/v1/bookings",
        json_data={
            "flight_id": flight_id,
            "passengers": passengers,
            "contact_email": passenger_email,
            "contact_phone": passenger_phone
        }
    )
    
    if "error" in result:
        return f"Error booking flight: {result['error']}"
    
    if not result.get("success"):
        return f"Booking failed: {result.get('error', 'Unknown error')}"
    
    booking = result["booking"]
    flight = booking.get("flight_details", {})
    
    output = "Flight Booked Successfully!\n\n"
    output += f"Confirmation Number (PNR): {booking['pnr']}\n"
    output += f"Booking ID: {booking['booking_id']}\n"
    output += f"Status: {booking['status']}\n\n"
    output += f"Flight: {flight.get('airline', 'N/A')} {flight.get('flight_number', 'N/A')}\n"
    output += f"Route: {flight.get('origin', 'N/A')} -> {flight.get('destination', 'N/A')}\n"
    output += f"Departure: {flight.get('departure_time', 'N/A')}\n"
    output += f"Arrival: {flight.get('arrival_time', 'N/A')}\n\n"
    output += f"Passenger: {passenger_first_name} {passenger_last_name}\n"
    output += f"Total Price: ${booking['total_price']:.2f} {booking['currency']}\n"
    output += f"Booked At: {booking['created_at']}\n"
    
    return output


@mcp.tool()
async def get_booking(booking_id: str) -> str:
    """
    Retrieve booking details by booking ID.
    
    Args:
        booking_id: The booking ID (UUID)
    
    Returns:
        Booking details including flight information and passenger details
    """
    logger.info(f"Retrieving booking: {booking_id}")
    
    result = await call_airline_service("GET", f"/api/v1/bookings/{booking_id}")
    
    if "error" in result:
        return f"Error retrieving booking: {result['error']}"
    
    if not result.get("success"):
        return f"Booking not found: {result.get('error', 'Unknown error')}"
    
    booking = result["booking"]
    flight = booking.get("flight_details", {})
    
    output = f"Booking Details\n"
    output += f"===============\n"
    output += f"PNR: {booking['pnr']}\n"
    output += f"Booking ID: {booking['booking_id']}\n"
    output += f"Status: {booking['status']}\n\n"
    output += f"Flight: {flight.get('airline', 'N/A')} {flight.get('flight_number', 'N/A')}\n"
    output += f"Route: {flight.get('origin', 'N/A')} -> {flight.get('destination', 'N/A')}\n"
    output += f"Departure: {flight.get('departure_time', 'N/A')}\n\n"
    output += f"Passengers: {len(booking.get('passengers', []))}\n"
    output += f"Total Price: ${booking['total_price']:.2f} {booking['currency']}\n"
    
    return output


@mcp.tool()
async def get_booking_by_pnr(pnr: str) -> str:
    """
    Retrieve booking details by PNR (confirmation code).
    
    Args:
        pnr: The 6-character PNR confirmation code
    
    Returns:
        Booking details including flight information and passenger details
    """
    logger.info(f"Retrieving booking by PNR: {pnr}")
    
    result = await call_airline_service("GET", f"/api/v1/bookings/pnr/{pnr.upper()}")
    
    if "error" in result:
        return f"Error retrieving booking: {result['error']}"
    
    if not result.get("success"):
        return f"Booking not found: {result.get('error', 'Unknown error')}"
    
    booking = result["booking"]
    flight = booking.get("flight_details", {})
    
    output = f"Booking Details (PNR: {pnr.upper()})\n"
    output += f"================================\n"
    output += f"Booking ID: {booking['booking_id']}\n"
    output += f"Status: {booking['status']}\n\n"
    output += f"Flight: {flight.get('airline', 'N/A')} {flight.get('flight_number', 'N/A')}\n"
    output += f"Route: {flight.get('origin', 'N/A')} -> {flight.get('destination', 'N/A')}\n"
    output += f"Departure: {flight.get('departure_time', 'N/A')}\n\n"
    output += f"Passengers: {len(booking.get('passengers', []))}\n"
    output += f"Total Price: ${booking['total_price']:.2f} {booking['currency']}\n"
    
    return output


@mcp.tool()
async def cancel_booking(booking_id: str) -> str:
    """
    Cancel an existing flight booking.
    
    Args:
        booking_id: The booking ID to cancel
    
    Returns:
        Cancellation confirmation
    """
    logger.info(f"Cancelling booking: {booking_id}")
    
    result = await call_airline_service("DELETE", f"/api/v1/bookings/{booking_id}")
    
    if "error" in result:
        return f"Error cancelling booking: {result['error']}"
    
    if result.get("success"):
        return f"Booking {booking_id} has been cancelled successfully.\nPNR: {result.get('pnr', 'N/A')}"
    else:
        return f"Failed to cancel booking: {result.get('message', 'Unknown error')}"


@mcp.tool()
async def list_airports() -> str:
    """
    Get a list of all supported airports.
    
    Returns:
        List of airport codes and names
    """
    logger.info("Listing airports")
    
    result = await call_airline_service("GET", "/api/v1/airports")
    
    if "error" in result:
        return f"Error listing airports: {result['error']}"
    
    airports = result.get("airports", [])
    
    output = "Supported Airports:\n"
    output += "==================\n"
    for airport in airports:
        output += f"  {airport['code']}: {airport['name']}\n"
    
    return output


# =============================================================================
# MCP Resources
# =============================================================================

@mcp.resource("airports://list")
async def airports_resource() -> str:
    """Provide list of supported airports as a resource."""
    result = await call_airline_service("GET", "/api/v1/airports")
    if "error" in result:
        return "Error loading airports"
    airports = result.get("airports", [])
    return "\n".join([f"{a['code']}: {a['name']}" for a in airports])


@mcp.resource("airlines://list")
async def airlines_resource() -> str:
    """Provide list of airlines as a resource."""
    result = await call_airline_service("GET", "/api/v1/airlines")
    if "error" in result:
        return "Error loading airlines"
    airlines = result.get("airlines", [])
    return "\n".join([f"{a['code']}: {a['name']}" for a in airlines])


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import sys
    
    port = int(os.getenv("PORT", "8010"))
    logger.info(f"Starting Airline MCP Server on port {port}")
    logger.info(f"Airline Service URL: {AIRLINE_SERVICE_URL}")
    
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        logger.info("Running with stdio transport")
        mcp.run(transport="stdio")
    else:
        # SSE transport for agent connectivity via MCP client SDK
        logger.info(f"Running with SSE transport on port {port}")
        mcp.settings.port = port
        mcp.settings.host = "0.0.0.0"
        mcp.settings.transport_security = False  # Allow Docker hostnames (airline-mcp, etc.)
        mcp.run(transport="sse")