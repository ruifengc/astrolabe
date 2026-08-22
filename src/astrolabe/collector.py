"""SpanCollector — in-memory span collection with sync + async context managers.

Core design:
  SpanCollector — one per traced request; buffers spans in memory, flushes to
                  a StorageBackend in batches (host controls flush timing).
  span()/aspan() — context managers that auto start/finish/timing/error-mark.
  Module-level span()/aspan() — ambient versions using the current-context
                  collector; transparent no-ops when tracing is not active
                  (host code runs unchanged without a collector installed).

Status mapping on flush: root span ok→done, error→failed (traces table);
span rows keep ok/error (trace_spans table).
"""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from .storage import StorageBackend

# Sentinel distinguishing "not passed" from an explicit None (new trace).
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SpanHandle:
    """Lightweight handle exposed to business code: span_id + set_attrs()."""

    __slots__ = ("_collector", "span_id")

    def __init__(self, collector: "SpanCollector", span_id: str):
        self._collector = collector
        self.span_id = span_id

    def set_attrs(self, **kwargs) -> None:
        """Append attributes to the in-flight span (JSON-serializable only)."""
        self._collector._patch_attrs(self.span_id, kwargs)


class _NoopHandle:
    """Handle returned when no collector is active (tracing disabled)."""

    __slots__ = ("span_id",)

    def __init__(self) -> None:
        self.span_id = ""

    def set_attrs(self, **kwargs) -> None:
        pass


class _SpanState:
    __slots__ = ("parent_id", "operation", "started_at", "started_at_iso",
                 "duration_ms", "status", "attrs")

    def __init__(self, *, parent_id, operation, started_at, started_at_iso,
                 attrs):
        self.parent_id = parent_id
        self.operation = operation
        self.started_at = started_at
        self.started_at_iso = started_at_iso
        self.duration_ms = 0
        self.status = "ok"
        self.attrs = attrs


class SpanCollector:
    """In-memory span buffer flushed to a StorageBackend.

    One instance per traced request. Business code interacts only through
    collector.span() / collector.aspan() context managers — never calling
    start/finish/flush directly (flush is the host/entry-point's job).

    Parent resolution: spans track a per-task stack of in-flight span ids
    (contextvar). A new span automatically attaches to the top of the stack —
    so nested ``aspan()`` blocks build the tree without passing any ids
    around. Explicit ``parent_id`` overrides the stack; explicit ``None``
    starts an independent trace.
    """

    def __init__(self, backend: StorageBackend, *, host: str = "",
                 trace_id: str | None = None):
        self._backend = backend
        self.host = host
        # Global trace id shared across systems (first hop generates it,
        # downstream hosts pass the upstream value in). Decoupled from
        # root_span_id: root_span_id is this system's local root span id.
        self.global_trace_id = trace_id or uuid4().hex[:16]
        self.root_span_id: str | None = None
        self._spans: dict[str, _SpanState] = {}
        self._pending: list[str] = []

    # ── context managers ──────────────────────────────────────

    @contextmanager
    def span(self, operation: str, *, parent_id=_UNSET, **attrs):
        """Sync context manager: auto start/finish/timing, marks errors."""
        handle = self._start(operation, parent_id, attrs)
        _push(handle.span_id)
        try:
            yield handle
        except Exception:
            self._finish(handle.span_id, status="error", error=self._exc_str())
            raise
        else:
            self._finish(handle.span_id, status="ok")
        finally:
            _pop(handle.span_id)

    @asynccontextmanager
    async def aspan(self, operation: str, *, parent_id=_UNSET, **attrs):
        """Async context manager: auto start/finish/timing, marks errors."""
        handle = self._start(operation, parent_id, attrs)
        _push(handle.span_id)
        try:
            yield handle
        except Exception:
            self._finish(handle.span_id, status="error", error=self._exc_str())
            raise
        else:
            self._finish(handle.span_id, status="ok")
        finally:
            _pop(handle.span_id)

    # ── flush ─────────────────────────────────────────────────

    def flush(self) -> int:
        """Persist all finished spans to the backend. Returns count written."""
        written = 0
        for sid in self._pending:
            state = self._spans[sid]
            trace_id = self._resolve_trace_id(sid)

            # Root span → materialize the traces row (fast listing).
            if state.parent_id is None:
                trace_status = {"ok": "done", "error": "failed"}.get(
                    state.status, state.status)
                self._backend.upsert_trace(
                    trace_id,
                    host=self.host,
                    root_span_id=sid,
                    status=trace_status,
                    duration_ms=state.duration_ms,
                    error=state.attrs.get("error", ""),
                    metadata=state.attrs,
                    created_at=state.started_at_iso,
                    updated_at=_now(),
                )

            self._backend.insert_span(
                sid, trace_id, state.parent_id, state.operation,
                state.duration_ms, state.status, state.attrs,
                state.started_at_iso,
            )
            written += 1

        self._backend.commit()
        self._pending.clear()
        return written

    # ── internal ──────────────────────────────────────────────

    def _start(self, operation: str, parent_id, attrs: dict) -> SpanHandle:
        if parent_id is _UNSET:
            if self.root_span_id is None:
                # First span of a collector is always its own root — do not
                # attach to an outer system's stack (nested trace entry).
                parent_id = None
            else:
                parent_id = self._stack_top_or_root()
        elif parent_id is not None and parent_id not in self._spans:
            # Dangling explicit parent (id from another collector / stale):
            # degrade to an independent root instead of breaking the flush
            # with a KeyError; record the original id for diagnosis.
            attrs["_dangling_parent"] = parent_id
            parent_id = None
        sid = uuid4().hex[:16]
        if parent_id is None and self.root_span_id is None:
            self.root_span_id = sid
        self._spans[sid] = _SpanState(
            parent_id=parent_id,
            operation=operation,
            started_at=time.monotonic(),
            started_at_iso=_now(),
            attrs=dict(attrs),
        )
        return SpanHandle(self, sid)

    def _stack_top_or_root(self) -> str | None:
        """Top of the per-task span stack, or this trace's root when the
        stack is empty (a bare call at entry level attaches to the root)."""
        st = _get_stack()
        if st:
            return st[-1]
        return self.root_span_id

    def _finish(self, span_id: str, *, status: str = "ok", **extra) -> None:
        state = self._spans[span_id]
        state.duration_ms = int((time.monotonic() - state.started_at) * 1000)
        state.status = status
        state.attrs.update(extra)
        self._pending.append(span_id)

    def _patch_attrs(self, span_id: str, attrs: dict) -> None:
        if span_id in self._spans:
            self._spans[span_id].attrs.update(attrs)

    def set_root_attrs(self, **kwargs) -> None:
        """Append attributes to the root span. Entry-point bookkeeping for
        values only known at the end (final answer, citations, hop_count...).
        No-op when the root has already finished."""
        if self.root_span_id:
            self._patch_attrs(self.root_span_id, kwargs)

    def _resolve_trace_id(self, span_id: str) -> str:
        """Trace id a span belongs to.

        Walks the parent chain to the local root: the collector's primary
        root (root_span_id) uses the shared global trace_id (possibly passed
        in from upstream); an independent root opened via explicit
        parent_id=None gets its own id (= its span_id). Defensive: missing
        chain links or cycles degrade to the global id instead of raising."""
        sid = span_id
        seen: set[str] = set()
        while sid not in seen:
            seen.add(sid)
            state = self._spans.get(sid)
            if state is None:
                return self.global_trace_id
            if state.parent_id is None:
                return self.global_trace_id if sid == self.root_span_id else sid
            sid = state.parent_id
        return self.global_trace_id  # cycle → fall back to global id

    @staticmethod
    def _exc_str() -> str:
        exc = sys.exc_info()[1]
        return str(exc) or exc.__class__.__name__


# ── per-task span stack (copy-on-write: tasks never share list objects) ──

_span_stack: ContextVar[list[str] | None] = ContextVar(
    "astrolabe_span_stack", default=None)


def _get_stack() -> list[str]:
    st = _span_stack.get()
    if st is None:
        st = []
        _span_stack.set(st)
    return st


def _push(span_id: str) -> None:
    _span_stack.set([*_get_stack(), span_id])


def _pop(span_id: str) -> None:
    st = _get_stack()
    if st and st[-1] == span_id:  # LIFO guarantee; defensive on mismatch
        _span_stack.set(st[:-1])


# ── ambient module-level helpers (no-op when tracing is off) ──

_noop_handle = _NoopHandle()


def _ambient() -> "SpanCollector | None":
    from .context import get_collector  # deferred: avoid circular import
    return get_collector()


@contextmanager
def span(operation: str, *, parent_id=_UNSET, **attrs):
    """Ambient sync span — uses the current-context collector; transparent
    no-op if none is active. Parent is auto-resolved from the span stack."""
    collector = _ambient()
    if collector is None:
        yield _noop_handle
        return
    with collector.span(operation, parent_id=parent_id, **attrs) as h:
        yield h


@asynccontextmanager
async def aspan(operation: str, *, parent_id=_UNSET, **attrs):
    """Ambient async span — uses the current-context collector; transparent
    no-op if none is active. Parent is auto-resolved from the span stack."""
    collector = _ambient()
    if collector is None:
        yield _noop_handle
        return
    async with collector.aspan(operation, parent_id=parent_id, **attrs) as h:
        yield h
