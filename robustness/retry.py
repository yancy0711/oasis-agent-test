"""Retry with exponential backoff. Mounted as a decorator so business
modules stay free of boilerplate."""
from __future__ import annotations
import functools, logging, random, time
from typing import Callable

log = logging.getLogger(__name__)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class TransientError(Exception):
    """Retryable failure. Tools map HTTP 429/5xx and timeouts to this."""


def with_retry(max_retries: int = 4, base: float = 1.5) -> Callable:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except TransientError as e:
                    if attempt == max_retries:
                        raise
                    delay = base ** attempt + random.random()
                    log.warning("retry %s (%d/%d): %s -- sleeping %.1fs",
                                fn.__name__, attempt + 1, max_retries, e, delay)
                    time.sleep(delay)
        return wrapper
    return deco
