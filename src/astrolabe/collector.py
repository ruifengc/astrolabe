"""Per-request capture with isolated task context and atomic batch persistence."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from .storage import StorageBackend
from .metadata import CaptureLimits, freeze, snapshot

_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SpanHandle:
    """A span's id and attributes; updates after it finishes are no-ops."""

    __slots__ = ("_collector", "span_id")

    def __init__(self, collector: SpanCollector, span_id: str):
        self._collector = collector
        self.span_id = span_id

    def set_attrs(self, **kwargs) -> None:
        self._collector._patch_attrs(self.span_id, kwargs)


class _NoopHandle:
    span_id = ""

    def set_attrs(self, **kwargs) -> None:
        pass


_noop_handle = _NoopHandle()


@dataclass
class _SpanState:
    parent_id: str | None
    trace_id: str
    operation: str
    started_at: float
    started_at_iso: str
    attrs: dict
    diagnostics: dict = field(default_factory=dict)
    metadata: dict | None = None
    duration_ms: int = 0
    status: str = "ok"
    finished: bool = False
    error: str = ""


class SpanCollector:
    """One collector per request; sibling asyncio tasks have independent stacks.

    trace_id membership is determined at creation, and metadata is copied at
    capture. close() seals any unfinished spans before persisting. Tasks which
    outlive the request subsequently get no-op handles, never mutate its trace.
    """

    def __init__(self, backend: StorageBackend, *, host: str = "",
                 trace_id: str | None = None, limits: CaptureLimits | None = None):
        self._backend = backend
        self.host = host
        self.global_trace_id = trace_id or uuid4().hex[:16]
        self.root_span_id: str | None = None
        self._limits = limits or CaptureLimits()
        self._spans: dict[str, _SpanState] = {}
        self._pending: list[str] = []
        self._stack: ContextVar[tuple[str, ...]] = ContextVar(
            "astrolabe_collector_stack", default=())
        self._lifecycle = "active"
        self._dropped_spans = 0

    @property
    def lifecycle(self) -> str:
        return self._lifecycle

    @contextmanager
    def span(self, operation: str, *, parent_id=_UNSET, **attrs):
        if self._lifecycle != "active":
            yield _noop_handle
            return
        if self._limits.max_spans and len(self._spans) >= self._limits.max_spans:
            self._dropped_spans += 1
            yield _noop_handle
            return
        handle = self._start(operation, parent_id, attrs)
        token = self._stack.set((*self._stack.get(), handle.span_id))
        try:
            yield handle
        except BaseException as exc:
            # Cancellation and process-exit exceptions must finish the span,
            # then propagate unchanged to the host.
            try:
                message = str(exc) or type(exc).__name__
            except Exception:
                message = type(exc).__name__
            self._finish(handle.span_id, status="error", error=message,
                         error_type=type(exc).__name__)
            raise
        else:
            self._finish(handle.span_id)
        finally:
            self._stack.reset(token)

    @asynccontextmanager
    async def aspan(self, operation: str, *, parent_id=_UNSET, **attrs):
        with self.span(operation, parent_id=parent_id, **attrs) as handle:
            yield handle

    def flush(self) -> int:
        """Persist finished spans; failure retains a retryable batch."""
        if not self._pending:
            return 0
        pending = tuple(self._pending)
        traces, spans = [], []
        for sid in pending:
            state = self._spans[sid]
            if state.metadata is None:
                state.metadata = freeze(state.attrs, state.diagnostics, self._limits)
            metadata = state.metadata
            if state.parent_id is None:
                traces.append(dict(
                    trace_id=state.trace_id, host=self.host, root_span_id=sid,
                    status="done" if state.status == "ok" else "failed",
                    duration_ms=state.duration_ms, error=state.error,
                    metadata=metadata, created_at=state.started_at_iso,
                    updated_at=_now(),
                ))
            spans.append(dict(
                span_id=sid, trace_id=state.trace_id, parent_span_id=state.parent_id,
                operation=state.operation, duration_ms=state.duration_ms,
                status=state.status, metadata=metadata, created_at=state.started_at_iso,
            ))
        self._backend.write_batch(traces, spans)
        del self._pending[:len(pending)]
        return len(pending)

    def close(self) -> int:
        """Seal request capture and flush. Storage errors propagate to the facade."""
        if self._lifecycle == "closed":
            return self.flush()
        self._lifecycle = "closing"
        try:
            unfinished = [sid for sid, state in self._spans.items() if not state.finished]
            for sid in unfinished:
                self._finish(sid, status="error", error="trace closed before span finished",
                             error_type="IncompleteSpan")
            if self.root_span_id and (unfinished or self._dropped_spans):
                root = self._spans[self.root_span_id]
                if unfinished:
                    root.diagnostics["unfinished_spans"] = len(unfinished)
                if self._dropped_spans:
                    root.diagnostics["dropped_spans"] = self._dropped_spans
                root.metadata = None
            return self.flush()
        finally:
            self._lifecycle = "closed"

    def _start(self, operation: str, parent_id, attrs: dict) -> SpanHandle:
        if parent_id is _UNSET:
            stack = self._stack.get()
            parent_id = stack[-1] if stack else self.root_span_id
        elif parent_id is not None and parent_id not in self._spans:
            attrs["_dangling_parent"] = parent_id
            parent_id = None
        sid = uuid4().hex[:16]
        if parent_id is None and self.root_span_id is None:
            self.root_span_id = sid
        trace_id = (self._spans[parent_id].trace_id if parent_id is not None else
                    self.global_trace_id if sid == self.root_span_id else sid)
        diagnostics = {}
        self._spans[sid] = _SpanState(
            parent_id=parent_id, trace_id=trace_id, operation=operation,
            started_at=time.monotonic(), started_at_iso=_now(),
            attrs=snapshot(attrs, diagnostics), diagnostics=diagnostics,
        )
        return SpanHandle(self, sid)

    def _finish(self, span_id: str, *, status: str = "ok", **extra) -> None:
        state = self._spans[span_id]
        if state.finished:
            return
        state.duration_ms = int((time.monotonic() - state.started_at) * 1000)
        state.status = status
        state.error = extra.get("error", "")
        if self._limits.max_string_length:
            state.error = state.error[:self._limits.max_string_length]
        state.attrs.update(snapshot(extra, state.diagnostics))
        state.finished = True
        self._pending.append(span_id)

    def _patch_attrs(self, span_id: str, attrs: dict) -> None:
        state = self._spans.get(span_id)
        if self._lifecycle == "active" and state and not state.finished:
            state.attrs.update(snapshot(attrs, state.diagnostics))

    def set_root_attrs(self, **kwargs) -> None:
        """Set root attributes while the root is active; otherwise a no-op."""
        if self.root_span_id:
            self._patch_attrs(self.root_span_id, kwargs)


@contextmanager
def span(operation: str, *, parent_id=_UNSET, **attrs):
    """Ambient synchronous span, a no-op when no request is active."""
    from .context import get_collector
    collector = get_collector()
    if collector is None:
        yield _noop_handle
    else:
        with collector.span(operation, parent_id=parent_id, **attrs) as handle:
            yield handle


@asynccontextmanager
async def aspan(operation: str, *, parent_id=_UNSET, **attrs):
    """Ambient asynchronous span; shares the synchronous lifecycle."""
    with span(operation, parent_id=parent_id, **attrs) as handle:
        yield handle
