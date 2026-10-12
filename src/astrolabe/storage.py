"""Storage backend abstraction.

A backend persists and queries spans. Astrolabe ships one implementation
(SQLiteBackend); custom backends plug in via this interface without touching
the collector or the facade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .tree import build_tree


class StorageBackend(ABC):
    """Persist and query trace spans.

    Contract:
      - write methods accept plain dicts / scalars; JSON (de)serialization of
        metadata is the backend's concern.
      - query methods return plain dicts (never sqlite3.Row or ORM objects),
        with ``metadata`` decoded to a dict.
    """

    # ── lifecycle ─────────────────────────────────────────────
    @abstractmethod
    def close(self) -> None:
        """Release underlying resources."""

    # ── write ─────────────────────────────────────────────────
    @abstractmethod
    def write_batch(self, traces: list[dict], spans: list[dict]) -> None:
        """Atomically persist a batch, rolling back on failure.

        Retrying the same span ids must be safe. Concurrent batches must not
        share a transaction. A different root_span_id must never replace an
        existing (trace_id, host). Serialization remains the backend's concern.
        """

    # ── query ─────────────────────────────────────────────────
    @abstractmethod
    def get_trace(self, trace_id: str, *, host: str | None = None) -> dict | None:
        """Fetch a trace row by id, metadata JSON-decoded. When several
        hosts share one trace_id, pass host to disambiguate."""

    @abstractmethod
    def get_traces(self, trace_id: str) -> list[dict]:
        """All system summaries for a global trace id."""

    @abstractmethod
    def list_traces(
        self,
        limit: int = 20,
        offset: int = 0,
        *,
        status: str = "",
        host: str | None = None,
        filters: dict | None = None,
    ) -> list[dict]:
        """List traces newest-first. ``filters``: {jsonpath__op: value} on metadata."""

    @abstractmethod
    def count_traces(self, *, status: str = "", host: str | None = None,
                     filters: dict | None = None) -> int:
        """Count traces using the same filters as list_traces."""

    @abstractmethod
    def get_trace_spans(self, trace_id: str, *, host: str | None = None) -> list[dict]:
        """All spans of a trace, chronological. With host, only that host's
        subtree (rooted at its root_span_id) is returned."""

    def build_span_tree(self, trace_id: str, *, host: str | None = None) -> dict | None:
        """One system's tree. Ambiguous multi-host ids raise ValueError."""
        trace = self.get_trace(trace_id, host=host)
        if trace is None:
            return None
        tree = build_tree(self.get_trace_spans(trace_id, host=trace["host"]),
                          trace["root_span_id"])
        return {"trace": trace, "tree": tree}

    def build_span_trees(self, trace_id: str) -> list[dict]:
        """Explicit multi-system view: one summary and tree per host."""
        spans = self.get_trace_spans(trace_id)
        return [{"trace": trace, "tree": build_tree(spans, trace["root_span_id"])}
                for trace in self.get_traces(trace_id)]

    @abstractmethod
    def operation_stats(self, *, host: str | None = None, filters: dict | None = None,
                        operation: str | None = None) -> list[dict]:
        """Generic operation call counts and durations; optional exact operation filter."""

    @abstractmethod
    def stats(self, *, host: str | None = None, filters: dict | None = None) -> dict:
        """Aggregated latency and generic operation metrics.

        by_hops/tool_stats remain compatibility views of host conventions.
        """
