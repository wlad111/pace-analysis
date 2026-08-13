"""HTTP API package for pace-analysis.

The application lives in `karting.api.app` (module attribute `app`, factory
`create_app`).  `create_app` is re-exported lazily here so that importing
`karting.api` alone does not pull FastAPI, the storage layer and scipy into a
process that only needs the domain model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from karting.api.app import create_app as create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    """Resolve `create_app` on first access (PEP 562)."""
    if name == "create_app":
        from karting.api.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
