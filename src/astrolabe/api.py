"""Astrolabe — public facade over storage + collection.

Usage::

    from astrolabe import Astrolabe, aspan

    a = Astrolabe(db_path="~/.astrolabe/astrolabe.db")

    # Async host: entry point + ambient spans
    async def ask(question):
        async with a.atrace("ask", host="my-agent", question=question):
            async with aspan("llm.chat", model="deepseek-v4"):
                ...
            # auto-flushed on exit of atrace()

    # Sync host
    def run():
        with a.trace("job", host="cron", name="nightly"):
            with span("step1"):
                ...
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager

from .backends.sqlite import SQLiteBackend
from .collector import SpanCollector
from .context import reset_collector, set_collector
from .storage import StorageBackend

logger = logging.getLogger(__name__)

_DEFAULT_DB = "~/.astrolabe/astrolabe.db"


class Astrolabe:
    """Facade. One instance per application; thread-safe enough for shared use
    (SQLite backend uses a single connection with check_same_thread=False)."""

    def __init__(self, db_path: str = _DEFAULT_DB,
                 *, backend: StorageBackend | None = None):
        self._backend = backend or SQLiteBackend(db_path)

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    def create_collector(self, host: str = "",
                         trace_id: str | None = None) -> SpanCollector:
        return SpanCollector(self._backend, host=host, trace_id=trace_id)

    # ── entry-point convenience ───────────────────────────────

    @contextmanager
    def trace(self, operation: str = "trace", *, host: str = "",
              trace_id: str | None = None, **attrs):
        """Sync entry point: create collector, install it in the current
        context, open the root span, flush everything on exit.

        ``trace_id``: upstream global trace id when this system is not the
        first hop of a cross-system chain (see get_trace_context); omitted
        → this system generates it."""
        collector = self.create_collector(host=host, trace_id=trace_id)
        token = set_collector(collector)
        try:
            with collector.span(operation, **attrs):
                yield collector
        finally:
            try:
                collector.flush()
            except Exception:
                # Observability must never break the host: log and continue.
                # The finally below guarantees the context is always reset.
                logger.exception("astrolabe: flush failed for %s",
                                 collector.global_trace_id)
            finally:
                reset_collector(token)

    @asynccontextmanager
    async def atrace(self, operation: str = "trace", *, host: str = "",
                     trace_id: str | None = None, **attrs):
        """Async entry point: create collector, install it, open the root
        span, flush everything on exit. ``trace_id`` semantics as in
        ``trace()``."""
        collector = self.create_collector(host=host, trace_id=trace_id)
        token = set_collector(collector)
        try:
            async with collector.aspan(operation, **attrs):
                yield collector
        finally:
            try:
                collector.flush()
            except Exception:
                logger.exception("astrolabe: flush failed for %s",
                                 collector.global_trace_id)
            finally:
                reset_collector(token)

    # ── query delegation ──────────────────────────────────────

    def get_trace(self, trace_id: str, *, host: str = "") -> dict | None:
        return self._backend.get_trace(trace_id, host=host)

    def list_traces(self, limit: int = 20, offset: int = 0, *,
                    status: str = "", host: str = "",
                    filters: dict | None = None) -> list[dict]:
        return self._backend.list_traces(
            limit, offset, status=status, host=host, filters=filters)

    def count_traces(self, *, status: str = "", host: str = "") -> int:
        return self._backend.count_traces(status=status, host=host)

    def get_trace_spans(self, trace_id: str, *, host: str = "") -> list[dict]:
        return self._backend.get_trace_spans(trace_id, host=host)

    def build_span_tree(self, trace_id: str, *, host: str = "") -> dict | None:
        return self._backend.build_span_tree(trace_id, host=host)

    def stats(self) -> dict:
        return self._backend.stats()

    def close(self) -> None:
        self._backend.close()
