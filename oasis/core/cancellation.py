"""Cooperative cancellation primitives shared by service and core stages."""
from __future__ import annotations

from threading import Event


class RunCancelled(RuntimeError):
    """Stop a run at a safe checkpoint without treating it as a failure."""


class CancellationToken:
    """Thread-safe cancellation flag owned by one application run."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        """Request cancellation; workers stop at their next checkpoint."""
        self._event.set()

    def cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._event.is_set()

    def is_cancelled(self) -> bool:
        """Compatibility alias suitable for a callback."""
        return self.cancelled()

    def check(self) -> None:
        """Raise the typed cancellation signal when cancellation was requested."""
        if self.cancelled():
            raise RunCancelled("Run cancelled by user")
