"""Stop synchronous provider work when its browser request disconnects."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from oasis.core.cancellation import CancellationToken, RunCancelled


async def call_while_connected(connection: Request, fn, *args):
    token = CancellationToken()
    worker = asyncio.create_task(run_in_threadpool(fn, *args, check=token.check))
    try:
        while not worker.done():
            await asyncio.wait({worker}, timeout=0.1)
            if not worker.done() and await connection.is_disconnected():
                token.cancel()
        return await worker
    finally:
        token.cancel()
        if not worker.done():
            # Provider transports observe this token and close their active socket.
            with suppress(RunCancelled, asyncio.CancelledError):
                await asyncio.shield(worker)
