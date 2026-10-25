"""Retry with exponential backoff. Mounted as a decorator so business
modules stay free of boilerplate."""
from __future__ import annotations
import functools, logging, random, time
from typing import Callable

log = logging.getLogger(__name__)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class TransientError(Exception):
    """Retryable failure with an optional provider-mandated wait."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


def with_retry(max_retries: int = 4, base: float = 1.5) -> Callable:
    """Retry decorated operations that raise TransientError."""
    def deco(fn: Callable) -> Callable:
        """Wrap one callable with the configured retry policy."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            """Run the wrapped callable until success or retry exhaustion."""
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except TransientError as e:
                    if attempt == max_retries:
                        raise
                    delay = max(
                        base ** attempt + random.random(),
                        e.retry_after_s or 0.0,
                    )
                    log.warning("retry %s (%d/%d): %s -- sleeping %.1fs",
                                fn.__name__, attempt + 1, max_retries, e, delay)
                    check = kwargs.get("cancel_check") or kwargs.get("check")
                    if check:
                        until = time.monotonic() + delay
                        while time.monotonic() < until:
                            check()
                            time.sleep(min(0.1, max(0.0, until - time.monotonic())))
                    else:
                        time.sleep(delay)
        return wrapper
    return deco
