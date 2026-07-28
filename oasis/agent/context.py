"""Session object memory, provenance, and auditable agent event history."""
from __future__ import annotations
from typing import Any, Callable
from oasis.contracts.domain import AgentEvent, Provenance
_MISSING = object()


class Context:
    """Store run objects, provenance, events, and lightweight experience."""

    def __init__(self) -> None:
        """Create an empty object store, lineage, experience, and event log."""
        self._objects: dict[str, Any] = {}
        self.lineage: list[Provenance] = []
        self.successes: list[dict] = []
        self.failures: list[dict] = []
        self.history: list[AgentEvent] = []
        self.event_callback: Callable[[AgentEvent], None] | None = None
        self.progress_callback: Callable[[str, int, int], None] | None = None

    def put(self, handle: str, obj: Any, prov: Provenance | list[Provenance] | None = None) -> str:
        """Store an object by handle and record any attached provenance."""
        self._objects[handle] = obj
        if prov: self.add_provenance(prov)
        return handle

    def add_provenance(self, prov: Provenance | list[Provenance]) -> None:
        """Append one or more provenance records to the session lineage."""
        self.lineage.extend(prov if isinstance(prov, list) else [prov])

    def record(self, stage: str, message: str, tool: str | None = None, **data: Any) -> AgentEvent:
        """Append one lightweight, user-visible event to the audit history."""
        event = AgentEvent(stage=stage, message=message, tool=tool, data=data)
        self.history.append(event)
        if self.event_callback: self.event_callback(event)
        return event

    def progress(self, label: str, done: int, total: int) -> None:
        """Forward transient deterministic progress without expanding history."""
        if self.progress_callback: self.progress_callback(label, done, total)

    def get(self, handle: str, default: Any = _MISSING) -> Any:
        """Return a stored object or an explicit default for a missing handle."""
        if handle in self._objects: return self._objects[handle]
        if default is not _MISSING: return default
        raise KeyError(f"Unknown context handle: {handle}")

    def has(self, handle: str) -> bool:
        """Return whether an object handle exists in the session store."""
        return handle in self._objects

    def handles(self) -> list[str]:
        """Return sorted lightweight object names without exposing stored values."""
        return sorted(self._objects)

    def hints(self) -> str:
        """Compress recent experience into a small planner prompt."""
        lines = [f"succeeded: {s['tool']}({s['args']})" for s in self.successes[-5:]]
        lines += [f"failed: {f['tool']} -> {f['error']}" for f in self.failures[-5:]]
        return "\n".join(lines)
