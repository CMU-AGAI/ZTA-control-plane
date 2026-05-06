#!/usr/bin/env python3
"""
Car Rental MCP Server Test Client
=================================
Tests the Car Rental MCP server tools.

Prerequisites:
    1. Car Rental backend running on :8003
    2. Car Rental MCP server running on :8000

Usage:
    python test_car_rental_mcp.py [--interactive]
"""

import asyncio
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = "http://localhost:8000/mcp"


async def run_automated_tests(session: ClientSession):
    """Run automated test suite."""
    print("\n" + "=" * 60)
    print("🧪 Running Automated Tests for Car Rental MCP")
    print("=" * 60)
    
    tests_passed = 0
    tests_failed = 0
    
    # Test 1: List vehicle categories
    print("\n📍 Test 1: List Vehicle Categories")
    try:
        result = await session.call_tool("list_vehicle_categories", {})
        output = result.content[0].text if result.content else ""
        if "Economy" in output and "SUV" in output:
            print("   ✅ PASSED - Categories listed successfully")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Expected categories not found")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 2: List add-ons
    print("\n📍 Test 2: List Add-ons")
    try:
        result = await session.call_tool("list_add_ons", {})
        output = result.content[0].text if result.content else ""
        if "GPS" in output or "gps" in output:
            print("   ✅ PASSED - Add-ons listed successfully")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Expected add-ons not found")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 3: List locations
    print("\n📍 Test 3: List Locations in LAX")
    try:
        result = await session.call_tool("list_locations", {"city_code": "LAX"})
        output = result.content[0].text if result.content else ""
        if "LAX" in output or "Los Angeles" in output:
            print("   ✅ PASSED - Locations listed successfully")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Expected locations not found")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 4: Search vehicles
    print("\n🚗 Test 4: Search Vehicles in LAX")
    try:
        result = await session.call_tool("search_vehicles", {
            "pickup_location_code": "LAX",
            "pickup_date": "2026-01-20",
            "dropoff_date": "2026-01-25"
        })
        output = result.content[0].text if result.content else ""
        if "Vehicle ID:" in output or "No vehicles found" in output:
            print("   ✅ PASSED - Vehicle search completed")
            print(f"   📄 Preview: {output[:200]}...")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Unexpected response format")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 5: Search SUVs only
    print("\n🚗 Test 5: Search SUVs in Miami")
    try:
        result = await session.call_tool("search_vehicles", {
            "pickup_location_code": "MIA",
            "pickup_date": "2026-02-01",
            "dropoff_date": "2026-02-07",
            "category": "SUV"
        })
        output = result.content[0].text if result.content else ""
        if "SUV" in output or "No vehicles found" in output:
            print("   ✅ PASSED - SUV search completed")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Unexpected response")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 6: One-way rental search
    print("\n🚗 Test 6: One-way Rental (LAX → SFO)")
    try:
        result = await session.call_tool("search_vehicles", {
            "pickup_location_code": "LAX",
            "dropoff_location_code": "SFO",
            "pickup_date": "2026-01-20",
            "dropoff_date": "2026-01-22"
        })
        output = result.content[0].text if result.content else ""
        if "Vehicle ID:" in output or "No vehicles found" in output:
            print("   ✅ PASSED - One-way search completed")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Unexpected response")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 7: Get non-existent rental
    print("\n🔍 Test 7: Get Non-existent Rental")
    try:
        result = await session.call_tool("get_rental", {
            "rental_id": "00000000-0000-0000-0000-000000000000"
        })
        output = result.content[0].text if result.content else ""
        if "not found" in output.lower() or "error" in output.lower():
            print("   ✅ PASSED - Correctly handled missing rental")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Should have returned error")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Summary
    print("\n" + "=" * 60)
    print(f"📊 Test Summary: {tests_passed} passed, {tests_failed} failed")
    print("=" * 60)
    
    return tests_failed == 0


async def run_interactive(session: ClientSession):
    """Run interactive mode."""
    print("\n" + "=" * 60)
    print("🎮 Interactive Mode - Car Rental MCP")
    print("=" * 60)
    print("\nAvailable commands:")
    print("  tools              - List all tools")
    print("  search             - Search vehicles (guided)")
    print("  categories         - List vehicle categories")
    print("  addons             - List available add-ons")
    print("  <tool_name> {...}  - Call tool with JSON args")
    print("  quit               - Exit")
    print()
    
    while True:
        try:
            user_input = input("car>>> ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() == 'quit':
                break
            
            if user_input.lower() == 'tools':
                tools = await session.list_tools()
                for tool in tools.tools:
                    print(f"  • {tool.name}")
                continue
            
            if user_input.lower() == 'categories':
                result = await session.call_tool("list_vehicle_categories", {})
                print(result.content[0].text if result.content else "No results")
                continue
            
            if user_input.lower() == 'addons':
                result = await session.call_tool("list_add_ons", {})
                print(result.content[0].text if result.content else "No results")
                continue
            
            if user_input.lower() == 'search':
                # Guided search
                pickup = input("  Pickup city (e.g., LAX): ").strip().upper() or "LAX"
                dropoff = input("  Dropoff city (empty = same): ").strip().upper() or None
                pickup_date = input("  Pickup date (YYYY-MM-DD): ").strip() or "2026-01-20"
                dropoff_date = input("  Dropoff date (YYYY-MM-DD): ").strip() or "2026-01-25"
                category = input("  Category (empty = all): ").strip() or None
                
                args = {
                    "pickup_location_code": pickup,
                    "pickup_date": pickup_date,
                    "dropoff_date": dropoff_date
                }
                if dropoff:
                    args["dropoff_location_code"] = dropoff
                if category:
                    args["category"] = category
                
                result = await session.call_tool("search_vehicles", args)
                print("\n" + result.content[0].text if result.content else "No results")
                continue
            
            # Parse tool call
            import json
            parts = user_input.split(" ", 1)
            tool_name = parts[0]
            args = json.loads(parts[1]) if len(parts) > 1 else {}
            
            result = await session.call_tool(tool_name, args)
            print("\n" + (result.content[0].text if result.content else "No output") + "\n")
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}\n")


async def main():
    interactive = "--interactive" in sys.argv or "-i" in sys.argv
    
    print(f"\n🔌 Connecting to Car Rental MCP Server at {SERVER_URL}...")
    
    try:
        async with streamablehttp_client(SERVER_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("✅ Connected to Car Rental MCP Server")
                
                # List tools
                tools = await session.list_tools()
                print(f"📦 Found {len(tools.tools)} tools")
                
                if interactive:
                    await run_interactive(session)
                    return True
                else:
                    return await run_automated_tests(session)
                    
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("\nMake sure:")
        print("  1. Car Rental backend is running on :8003")
        print("  2. Car Rental MCP server is running on :8000")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)