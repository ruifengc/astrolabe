"""Public collection and query facade; storage implementations stay behind it."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager

from .backends.sqlite import SQLiteBackend
from .collector import SpanCollector
from .context import reset_collector, set_collector
from .metadata import CaptureLimits
from .storage import StorageBackend

logger = logging.getLogger(__name__)
_DEFAULT_DB = "~/.astrolabe/astrolabe.db"


class Astrolabe:
    """One instance per application. SQLite serializes complete write batches."""

    def __init__(self, db_path: str = _DEFAULT_DB, *, backend: StorageBackend | None = None,
                 readonly: bool = False, limits: CaptureLimits | None = None):
        self._backend = backend if backend is not None else SQLiteBackend(db_path, readonly=readonly)
        self._limits = limits or CaptureLimits()

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    def create_collector(self, host: str = "", trace_id: str | None = None) -> SpanCollector:
        return SpanCollector(self._backend, host=host, trace_id=trace_id, limits=self._limits)

    @contextmanager
    def trace(self, operation: str = "trace", *, host: str = "",
              trace_id: str | None = None, **attrs):
        """Create a request, capture its root, and seal/flush on every exit path.

        Upstream trace_id joins the global chain; omission generates a new id.
        Storage failures are logged and never replace host exceptions.
        """
        collector = self.create_collector(host=host, trace_id=trace_id)
        token = set_collector(collector)
        try:
            with collector.span(operation, **attrs):
                yield collector
        finally:
            try:
                collector.close()
            except Exception:
                logger.exception("astrolabe: flush failed for %s", collector.global_trace_id)
            finally:
                reset_collector(token)

    @asynccontextmanager
    async def atrace(self, operation: str = "trace", *, host: str = "",
                     trace_id: str | None = None, **attrs):
        """Async entry using the same lifecycle as trace(). Flush is synchronous."""
        with self.trace(operation, host=host, trace_id=trace_id, **attrs) as collector:
            yield collector

    def get_trace(self, trace_id: str, *, host: str | None = None) -> dict | None:
        return self._backend.get_trace(trace_id, host=host)

    def get_traces(self, trace_id: str) -> list[dict]:
        return self._backend.get_traces(trace_id)

    def list_traces(self, limit: int = 20, offset: int = 0, *, status: str = "",
                    host: str | None = None, filters: dict | None = None) -> list[dict]:
        return self._backend.list_traces(limit, offset, status=status, host=host, filters=filters)

    def count_traces(self, *, status: str = "", host: str | None = None,
                     filters: dict | None = None) -> int:
        return self._backend.count_traces(status=status, host=host, filters=filters)

    def get_trace_spans(self, trace_id: str, *, host: str | None = None) -> list[dict]:
        return self._backend.get_trace_spans(trace_id, host=host)

    def build_span_tree(self, trace_id: str, *, host: str | None = None) -> dict | None:
        return self._backend.build_span_tree(trace_id, host=host)

    def build_span_trees(self, trace_id: str) -> list[dict]:
        return self._backend.build_span_trees(trace_id)

    def operation_stats(self, *, host: str | None = None, filters: dict | None = None,
                        operation: str | None = None) -> list[dict]:
        return self._backend.operation_stats(host=host, filters=filters, operation=operation)

    def stats(self, *, host: str | None = None, filters: dict | None = None) -> dict:
        return self._backend.stats(host=host, filters=filters)

    def close(self) -> None:
        self._backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
