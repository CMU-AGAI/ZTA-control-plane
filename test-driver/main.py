import argparse
import asyncio
import os
import sys
from datetime import datetime
from load_test import run_load_test
from security_test import run_prompt_injection, run_intent_hijack, run_transit_trust_tests

# Default Configuration
DEFAULT_TARGET_URL = os.environ.get("TARGET_URL", "http://localhost:8080/chat")
DEFAULT_USER_ID = "11111111-1111-1111-1111-111111111111"

# Sidecar URLs for transit trust tests
SIDECAR_URLS = {
    "airline-agent-sidecar": os.environ.get("AIRLINE_SIDECAR_URL", "http://localhost:19091"),
    "hotel-agent-sidecar": os.environ.get("HOTEL_SIDECAR_URL", "http://localhost:19092"),
    "car-rental-agent-sidecar": os.environ.get("CARRENTAL_SIDECAR_URL", "http://localhost:19093"),
}
DEFAULT_SIDECAR_URL = SIDECAR_URLS["airline-agent-sidecar"]

async def main():
    parser = argparse.ArgumentParser(description="ZTA Test Bed Driver")
    parser.add_argument("--mode", choices=["load", "security", "prompt", "intent", "transit", "all"], default="all", help="Test mode")
    parser.add_argument("--target", default=DEFAULT_TARGET_URL, help="Target URL (Travel Planner)")
    parser.add_argument("--sidecar-target", default=DEFAULT_SIDECAR_URL, help="Default sidecar URL for transit trust")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrency for load test")
    parser.add_argument("--requests", type=int, default=100, help="Total requests for load test")
    
    args = parser.parse_args()
    
    print(f"\n{'='*72}")
    print(f"  ZTA MULTI-AGENT SYSTEM — TEST DRIVER")
    print(f"  Mode: {args.mode} | Target: {args.target}")
    print(f"  Time: {datetime.now().isoformat()}")
    print(f"{'='*72}\n")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    prompts_dir = os.path.join(base_dir, "prompts")

    if args.mode in ["load", "all"]:
        await run_load_test(args.target, args.concurrency, args.requests, DEFAULT_USER_ID)
    
    if args.mode in ["prompt", "security", "all"]:
        print("running prompt injection tests")
        await run_prompt_injection(args.target, os.path.join(prompts_dir, "prompt_injection.json"))
    
    if args.mode in ["intent", "security", "all"]:
        print("running intent hijack tests")
        await run_intent_hijack(args.target, os.path.join(prompts_dir, "intent_tests.json"))
    
    if args.mode in ["transit", "security", "all"]:
        print("running transit trust tests")
        await run_transit_trust_tests(
            args.sidecar_target,
            os.path.join(prompts_dir, "transit_trust.json"),
            sidecar_urls=SIDECAR_URLS
        )

    print(f"\n{'='*72}")
    print(f"  TEST DRIVER COMPLETE")
    print(f"{'='*72}\n")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTest interrupted.")
        sys.exit(1)
