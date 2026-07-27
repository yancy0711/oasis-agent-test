"""Session memory (no weight updates anywhere):
  1) object store -- large objects (GeoDataFrames, matrices) live here under
     handles and never enter the LLM context window;
  2) experience log -- recent successes/failures distilled into prompt hints
     (in-context learning; a small innovation point for the paper).
"""
from __future__ import annotations
from typing import Any
from agent.schemas import Provenance
_MISSING = object()



class Context:
    def __init__(self) -> None:
        """Create an empty object store, lineage log, and experience log."""
        self._objects: dict[str, Any] = {}
        self.lineage: list[Provenance] = []
        self.successes: list[dict] = []
        self.failures: list[dict] = []

    def put(self, handle: str, obj: Any, prov: Provenance | list[Provenance] | None = None) -> str:
        """Store an object by handle and record any attached provenance."""
        self._objects[handle] = obj
        if prov: self.add_provenance(prov)
        return handle

    def add_provenance(self, prov: Provenance | list[Provenance]) -> None:
        """Append one or more provenance records to the session lineage."""
        self.lineage.extend(prov if isinstance(prov, list) else [prov])

    def get(self, handle: str, default: Any = _MISSING) -> Any:
        """Return a stored object or an explicit default for a missing handle."""
        if handle in self._objects: return self._objects[handle]
        if default is not _MISSING: return default
        raise KeyError(f"Unknown context handle: {handle}")

    def has(self, handle: str) -> bool:
        """Return whether an object handle exists in the session store."""
        return handle in self._objects

    def hints(self) -> str:
        """Compress recent experience into a few hundred tokens of prompt hints."""
        lines = [f"succeeded: {s['tool']}({s['args']})" for s in self.successes[-5:]]
        lines += [f"failed: {f['tool']} -> {f['error']}" for f in self.failures[-5:]]
        return "\n".join(lines)
