"""Tool registry: the LLM's only window into the system.

Registration == exposure. A function not registered here simply does not
exist for the agent, which makes the autonomy boundary explicit and auditable.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolSpec:
    name: str
    fn: Callable
    description: str          # tell the LLM when to use it, preconditions, failure meaning
    args_schema: dict[str, Any] = field(default_factory=dict)


class Registry:
    def __init__(self) -> None:
        """Create an empty named-tool registry."""
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, description: str, args_schema: dict | None = None):
        """Return a decorator that exposes a callable under a stable tool name."""
        def deco(fn: Callable):
            """Add the decorated callable to this registry."""
            self._tools[name] = ToolSpec(name, fn, description, args_schema or {})
            return fn
        return deco

    def get(self, name: str) -> ToolSpec:
        """Return one registered tool specification by name."""
        if name not in self._tools:
            raise KeyError(f"Unregistered tool: {name}. Available: {list(self._tools)}")
        return self._tools[name]

    def describe(self) -> list[dict]:
        """Tool list in the shape LLM tool-use APIs expect."""
        return [{"name": t.name, "description": t.description,
                 "input_schema": {"type": "object", "properties": t.args_schema,
                                  "additionalProperties": False}}
                for t in self._tools.values()]


registry = Registry()

# Import once so baseline and diagnostic workflow tools are always exposed.
from tools import workflow as _workflow  # noqa: E402,F401
