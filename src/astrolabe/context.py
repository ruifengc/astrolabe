"""Context propagation for the active SpanCollector.

contextvars are safe across asyncio tasks and threads, so both sync and async
hosts can use the module-level span()/aspan() helpers without threading the
collector through function signatures.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import SpanCollector

_current_collector: ContextVar["SpanCollector | None"] = ContextVar(
    "astrolabe_collector", default=None
)


def get_collector() -> "SpanCollector | None":
    """Return the ambient collector for the current task/thread, or None."""
    collector = _current_collector.get()
    return collector if collector is not None and collector.lifecycle == "active" else None


def get_trace_context() -> dict | None:
    """Propagation context for downstream calls: {"trace_id", "host"}.

    The first system generates the trace_id; a downstream host passes it
    into its own ``trace()``/``atrace()`` entry point so the whole chain
    shares one id. Returns None when tracing is inactive (ambient no-op)."""
    collector = get_collector()
    if collector is None:
        return None
    return {"trace_id": collector.global_trace_id, "host": collector.host}


def set_collector(collector: "SpanCollector") -> Token:
    """Install a collector for the current context. Returns the reset token."""
    return _current_collector.set(collector)


def reset_collector(token: Token) -> None:
    """Restore the previous collector using the token from set_collector."""
    _current_collector.reset(token)
