"""Storage backend abstraction.

A backend persists and queries spans. Astrolabe ships one implementation
(SQLiteBackend); custom backends plug in via this interface without touching
the collector or the facade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


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

    @abstractmethod
    def commit(self) -> None:
        """Flush any pending writes (no-op for autocommit backends)."""

    # ── write ─────────────────────────────────────────────────
    @abstractmethod
    def upsert_trace(
        self,
        trace_id: str,
        *,
        host: str,
        root_span_id: str,
        status: str,
        duration_ms: int | None,
        error: str,
        metadata: dict,
        created_at: str,
        updated_at: str,
    ) -> None:
        """Insert or replace the root-span row (trace materialization)."""

    @abstractmethod
    def insert_span(
        self,
        span_id: str,
        trace_id: str,
        parent_span_id: str | None,
        operation: str,
        duration_ms: int,
        status: str,
        metadata: dict,
        created_at: str,
    ) -> None:
        """Append one span row."""

    # ── query ─────────────────────────────────────────────────
    @abstractmethod
    def get_trace(self, trace_id: str, *, host: str = "") -> dict | None:
        """Fetch a trace row by id, metadata JSON-decoded. When several
        hosts share one trace_id, pass host to disambiguate."""

    @abstractmethod
    def list_traces(
        self,
        limit: int = 20,
        offset: int = 0,
        *,
        status: str = "",
        host: str = "",
        filters: dict | None = None,
    ) -> list[dict]:
        """List traces newest-first. ``filters``: {jsonpath__op: value} on metadata."""

    @abstractmethod
    def count_traces(self, *, status: str = "", host: str = "") -> int:
        """Count traces matching status/host."""

    @abstractmethod
    def get_trace_spans(self, trace_id: str, *, host: str = "") -> list[dict]:
        """All spans of a trace, chronological. With host, only that host's
        subtree (rooted at its root_span_id) is returned."""

    @abstractmethod
    def build_span_tree(self, trace_id: str, *, host: str = "") -> dict | None:
        """Return {trace, tree} — tree is the nested span tree (metadata decoded)."""

    @abstractmethod
    def stats(self) -> dict:
        """Aggregated latency / hop / per-operation metrics."""
