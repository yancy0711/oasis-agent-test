"""HTTP calls that release their socket when a task is cancelled or expires."""
import asyncio
from time import monotonic

import httpx


def cancellable_request(method, url, *, check=lambda: None, timeout=(5, 45), **kwargs):
    """Sync entry point for worker threads; timeout is (connect, read) seconds.

    Socket timeouts alone do not bound slow streaming responses. Also enforce
    a wall deadline of connect + read, and observe the caller's cancellation.
    """
    check()
    connect, read = timeout

    async def request():
        deadline = monotonic() + connect + read
        limits = httpx.Timeout(read, connect=connect)
        async with httpx.AsyncClient(timeout=limits, follow_redirects=True) as client:
            task = asyncio.create_task(client.request(method, url, **kwargs))
            try:
                while True:
                    check()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise httpx.ReadTimeout(f"HTTP wall deadline reached after {connect + read:g}s")
                    done, _ = await asyncio.wait({task}, timeout=min(0.1, remaining))
                    if done:
                        check()
                        return task.result()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(request())
