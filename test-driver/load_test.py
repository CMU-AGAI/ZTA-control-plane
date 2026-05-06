import asyncio
import time
import uuid
import httpx
from colorama import Fore, Style

async def send_request(client, url, message, user_id):
    payload = {
        "message": message,
        "user_id": user_id,
        "conversation_id": str(uuid.uuid4()),
        "trip_id": None
    }
    start = time.perf_counter()
    try:
        response = await client.post(url, json=payload)
        latency_ms = (time.perf_counter() - start) * 1000
        return response.status_code, latency_ms
    except Exception as e:
        print(f"{Fore.RED}Request failed: {e}{Style.RESET_ALL}")
        return 0, 0

async def run_load_test(target_url, concurrency, total_requests, user_id):
    print(f"{Fore.CYAN}Starting Load Test against {target_url}{Style.RESET_ALL}")
    print(f"Concurrency: {concurrency}, Total Requests: {total_requests}")

    messages = [
        "Find flights from JFK to LAX",
        "Find hotels in Miami",
        "Search for rental cars in LAX",
        "Show me my trips"
    ]
    results = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        sem = asyncio.Semaphore(concurrency)

        async def bounded_request(i):
            async with sem:
                message = messages[i % len(messages)]
                return await send_request(client, target_url, message, user_id)

        tasks = [asyncio.create_task(bounded_request(i)) for i in range(total_requests)]
        for task in asyncio.as_completed(tasks):
             result = await task
             results.append(result)
             if len(results) % 10 == 0:
                 print(f".", end="", flush=True)

    print("\n")
    statuses = [r[0] for r in results]
    latencies = [r[1] for r in results if r[1] > 0]
    
    success = sum(1 for s in statuses if 200 <= s < 300)
    error = total_requests - success
    
    if latencies:
        p95 = sorted(latencies)[int(0.95 * len(latencies)) - 1]
        avg = sum(latencies) / len(latencies)
    else:
        p95 = 0
        avg = 0

    print(f"{Fore.GREEN}Load Test Completed{Style.RESET_ALL}")
    print(f"Success: {success} ({success/total_requests*100:.1f}%)")
    print(f"Error: {error} ({error/total_requests*100:.1f}%)")
    print(f"Avg Latency: {avg:.2f}ms")
    print(f"P95 Latency: {p95:.2f}ms")
