#!/usr/bin/env python3
"""
Airline MCP Server Test Client
==============================
Tests the Airline MCP server tools.

Prerequisites:
    1. Airline backend running on :8001
    2. Airline MCP server running on :8010

Usage:
    python test_airline_mcp.py [--interactive]
"""

import asyncio
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = "http://localhost:8000/mcp"


async def run_automated_tests(session: ClientSession):
    """Run automated test suite."""
    print("\n" + "=" * 60)
    print("🧪 Running Automated Tests for Airline MCP")
    print("=" * 60)
    
    tests_passed = 0
    tests_failed = 0
    
    # Test 1: List airports
    print("\n📍 Test 1: List Airports")
    try:
        result = await session.call_tool("list_airports", {})
        output = result.content[0].text if result.content else ""
        if "JFK" in output and "LAX" in output:
            print("   ✅ PASSED - Airports listed successfully")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Expected airports not found")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 2: Search flights
    print("\n✈️  Test 2: Search Flights (JFK → LAX)")
    try:
        result = await session.call_tool("search_flights", {
            "origin": "JFK",
            "destination": "LAX",
            "departure_date": "2026-01-20",
            "passengers": 1,
            "cabin_class": "economy"
        })
        output = result.content[0].text if result.content else ""
        if "Flight ID:" in output or "No flights found" in output:
            print("   ✅ PASSED - Flight search completed")
            print(f"   📄 Preview: {output[:200]}...")
            tests_passed += 1
        else:
            print("   ❌ FAILED - Unexpected response format")
            tests_failed += 1
    except Exception as e:
        print(f"   ❌ FAILED - {e}")
        tests_failed += 1
    
    # Test 3: Search with different cabin class
    print("\n✈️  Test 3: Search Business Class Flights")
    try:
        result = await session.call_tool("search_flights", {
            "origin": "SFO",
            "destination": "ORD",
            "departure_date": "2026-01-25",
            "passengers": 2,
            "cabin_class": "business"
        })
        output = result.content[0].text if result.content else ""
        if "business" in output.lower() or "No flights found" in output:
            print("   ✅ PASSED - Business class search completed")
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
        result = await session.call_tool("get_booking", {
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
    
    # Summary
    print("\n" + "=" * 60)
    print(f"📊 Test Summary: {tests_passed} passed, {tests_failed} failed")
    print("=" * 60)
    
    return tests_failed == 0


async def run_interactive(session: ClientSession):
    """Run interactive mode."""
    print("\n" + "=" * 60)
    print("🎮 Interactive Mode - Airline MCP")
    print("=" * 60)
    print("\nAvailable commands:")
    print("  tools              - List all tools")
    print("  search             - Search flights (guided)")
    print("  <tool_name> {...}  - Call tool with JSON args")
    print("  quit               - Exit")
    print()
    
    while True:
        try:
            user_input = input("airline>>> ").strip()
            
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
                origin = input("  Origin (e.g., JFK): ").strip().upper() or "JFK"
                dest = input("  Destination (e.g., LAX): ").strip().upper() or "LAX"
                date = input("  Date (YYYY-MM-DD): ").strip() or "2026-01-20"
                cabin = input("  Cabin (economy/business/first): ").strip() or "economy"
                
                result = await session.call_tool("search_flights", {
                    "origin": origin,
                    "destination": dest,
                    "departure_date": date,
                    "passengers": 1,
                    "cabin_class": cabin
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
    
    print(f"\n🔌 Connecting to Airline MCP Server at {SERVER_URL}...")
    
    try:
        async with streamablehttp_client(SERVER_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("✅ Connected to Airline MCP Server")
                
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
        print("  1. Airline backend is running on :8001")
        print("  2. Airline MCP server is running on :8000")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)