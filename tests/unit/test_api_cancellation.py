"""A dismissed route request must stop its provider work, not only hide the UI."""
import asyncio
import threading
import time

import pytest

from api.cancellation import call_while_connected
from oasis.core.cancellation import RunCancelled


def test_disconnected_browser_stops_worker_and_waits_for_cleanup():
    stopped = threading.Event()

    class Browser:
        async def is_disconnected(self):
            return True

    def provider(*, check):
        try:
            while True:
                check()
                time.sleep(0.01)
        finally:
            stopped.set()

    started = time.monotonic()
    with pytest.raises(RunCancelled):
        asyncio.run(call_while_connected(Browser(), provider))
    assert stopped.is_set()
    assert time.monotonic() - started < 1


def test_connected_browser_receives_original_provider_error():
    class Browser:
        async def is_disconnected(self):
            return False

    def provider(*, check):
        check()
        raise RuntimeError("ORS unavailable")

    with pytest.raises(RuntimeError, match="ORS unavailable"):
        asyncio.run(call_while_connected(Browser(), provider))
