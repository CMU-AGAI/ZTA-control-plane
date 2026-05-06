#!/usr/bin/env python3
"""
Hotel MCP Server Test Client
============================
Tests the Hotel MCP server tools.

Prerequisites:
    1. Hotel backend running on :8002
    2. Hotel MCP server running on :8000

Usage:
    python test_hotel_mcp.py [--interactive]
"""

import asyncio
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = "http://localhost:8000/mcp"


async def run_automated_tests(session: ClientSession):
    """Run automated test suite."""
    print("\n" + "=" * 60)
    print("🧪 Running Automated Tests for Hotel MCP")
    print("=" * 60)
    
    tests_passed = 0
    tests_failed = 0
    
    # Test 1: List cities
    print("\n📍 Test 1: List Cities")
    try:
        result = await session.call_tool("list_cities", {})
        output = result.content[0].text if result.content else ""
        if "JFK" in output and "New York" in output:
            print("   ✅ PASSED - Cities listed successfully")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Expected cities not found")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 2: Search hotels
    print("\n🏨 Test 2: Search Hotels in NYC")
    try:
        result = await session.call_tool("search_hotels", {
            "city_code": "JFK",
            "check_in_date": "2026-01-20",
            "check_out_date": "2026-01-23",
            "guests": 2,
            "min_stars": 3
        })
        output = result.content[0].text if result.content else ""
        if "Hotel ID:" in output or "Room Type ID:" in output or "No hotels found" in output:
            print("   ✅ PASSED - Hotel search completed")
            print(f"   📄 Preview: {output[:200]}...")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Unexpected response format")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 3: Search hotels in LA with high star rating
    print("\n🏨 Test 3: Search 4+ Star Hotels in LA")
    try:
        result = await session.call_tool("search_hotels", {
            "city_code": "LAX",
            "check_in_date": "2026-02-01",
            "check_out_date": "2026-02-05",
            "guests": 1,
            "min_stars": 4
        })
        output = result.content[0].text if result.content else ""
        if "★" in output or "No hotels found" in output:
            print("   ✅ PASSED - LA hotel search completed")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Unexpected response")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 4: Get non-existent booking
    print("\n🔍 Test 4: Get Non-existent Booking")
    try:
        result = await session.call_tool("get_hotel_booking", {
            "booking_id": "00000000-0000-0000-0000-000000000000"
        })
        output = result.content[0].text if result.content else ""
        if "not found" in output.lower() or "error" in output.lower():
            print("   ✅ PASSED - Correctly handled missing booking")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Should have returned error")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 5: Invalid city code
    print("\n🔍 Test 5: Search with Invalid City Code")
    try:
        result = await session.call_tool("search_hotels", {
            "city_code": "XXX",
            "check_in_date": "2026-01-20",
            "check_out_date": "2026-01-23",
            "guests": 1,
            "min_stars": 1
        })
        output = result.content[0].text if result.content else ""
        if "error" in output.lower() or "unknown" in output.lower():
            print("   ✅ PASSED - Correctly rejected invalid city")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Should have returned error for invalid city")
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
    print("🎮 Interactive Mode - Hotel MCP")
    print("=" * 60)
    print("\nAvailable commands:")
    print("  tools              - List all tools")
    print("  search             - Search hotels (guided)")
    print("  <tool_name> {...}  - Call tool with JSON args")
    print("  quit               - Exit")
    print()
    
    while True:
        try:
            user_input = input("hotel>>> ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() == 'quit':
                break
            
            if user_input.lower() == 'tools':
                tools = await session.list_tools()
                for tool in tools.tools:
                    print(f"  • {tool.name}")
                continue
            
            if user_input.lower() == 'search':
                # Guided search
                city = input("  City code (e.g., JFK, LAX): ").strip().upper() or "JFK"
                checkin = input("  Check-in (YYYY-MM-DD): ").strip() or "2026-01-20"
                checkout = input("  Check-out (YYYY-MM-DD): ").strip() or "2026-01-23"
                guests = input("  Guests (1-10): ").strip() or "2"
                stars = input("  Min stars (1-5): ").strip() or "3"
                
                result = await session.call_tool("search_hotels", {
                    "city_code": city,
                    "check_in_date": checkin,
                    "check_out_date": checkout,
                    "guests": int(guests),
                    "min_stars": int(stars)
                })
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
    
    print(f"\n🔌 Connecting to Hotel MCP Server at {SERVER_URL}...")
    
    try:
        async with streamablehttp_client(SERVER_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("✅ Connected to Hotel MCP Server")
                
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
        print("  1. Hotel backend is running on :8002")
        print("  2. Hotel MCP server is running on :8000")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)