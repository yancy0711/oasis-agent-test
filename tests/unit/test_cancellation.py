"""Offline checks for cooperative run cancellation."""
import pytest

from oasis.agent.context import Context
from oasis.core.cancellation import CancellationToken, RunCancelled


def test_context_progress_forwards_stats_until_cancelled():
    """Progress remains backward compatible while accepting structured stats."""
    token = CancellationToken()
    events = []
    context = Context()
    context.cancel_callback = token.is_cancelled
    context.progress_callback = (
        lambda label, done, total, **stats:
        events.append((label, done, total, stats))
    )

    context.progress("ORS requests", 1, 2, cache_hits=7)
    token.cancel()

    with pytest.raises(RunCancelled):
        context.progress("ORS requests", 2, 2)
    assert events == [("ORS requests", 1, 2, {"cache_hits": 7})]


def test_cancellation_token_is_idempotent():
    """Repeated cancellation requests keep the same typed terminal signal."""
    token = CancellationToken()
    token.cancel()
    token.cancel()

    assert token.cancelled() and token.is_cancelled()
    with pytest.raises(RunCancelled, match="cancelled"):
        token.check()
