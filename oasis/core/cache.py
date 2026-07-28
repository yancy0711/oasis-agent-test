"""Place-scoped disk cache helpers."""
from __future__ import annotations

import contextvars
import hashlib
import os
import pathlib
import re
import unicodedata
from contextlib import contextmanager
from typing import Iterator

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE_DIR = pathlib.Path(os.getenv("OASIS_CACHE_DIR", PROJECT_ROOT / "data" / "cache"))
CACHE_VERSION = "v1"
_CACHE_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar("oasis_cache_scope", default=None)


def place_slug(place_name: str) -> str:
    """Return a stable filesystem-safe identifier for one study area."""
    if place_name in {"_shared", "shared"}: return "_shared"
    text = unicodedata.normalize("NFKD", place_name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\bunited states(?: of america)?\b", "usa", text)
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug[:96] or f"place_{hashlib.sha256(place_name.encode()).hexdigest()[:12]}"


def current_cache_scope() -> str:
    """Return the active place identifier or reject an unscoped cache call."""
    scope = _CACHE_SCOPE.get()
    if scope is None: raise RuntimeError("Cache scope is not set; use scoped_cache(place_name)")
    return scope


@contextmanager
def scoped_cache(place_name: str) -> Iterator[str]:
    """Route cache reads and writes to one concrete place for a block."""
    slug = place_slug(place_name)
    if slug == "_shared": raise ValueError("A concrete place is required for cache writes")
    token = _CACHE_SCOPE.set(slug)
    try: yield slug
    finally: _CACHE_SCOPE.reset(token)


def cache_dir(namespace: str) -> pathlib.Path:
    """Return and create the active place/source/version cache directory."""
    path = CACHE_DIR / "places" / current_cache_scope() / namespace / CACHE_VERSION
    path.mkdir(parents=True, exist_ok=True)
    return path
