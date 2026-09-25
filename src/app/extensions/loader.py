"""Loading the domain registry.

No entry-points, no plugin discovery, no package scanning: the template is **copied**, not
installed. ``app/domain/__init__.py`` is the single coupling point — ten lines instead of a
plugin framework.
"""

from __future__ import annotations

from functools import lru_cache

from app.extensions.registry import EMPTY_REGISTRY, DomainRegistry


@lru_cache
def load_registry() -> DomainRegistry:
    """Return the domain's registry, or the empty one when there is no domain.

    ``ImportError`` means "no domain here" — the empty template is fully functional. Caveat:
    an ``ImportError`` raised *inside* a domain module also lands here
    and would silently yield an empty registry, so a service WITH a domain must assert in its own
    ``test_domain_registry.py`` that the registry is non-empty and contains what it expects.
    """
    try:
        from app.domain import REGISTRY
    except ImportError:
        return EMPTY_REGISTRY
    return REGISTRY
