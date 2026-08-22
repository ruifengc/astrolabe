"""SpanCollector + ambient span/aspan tests (sync and async)."""

from __future__ import annotations

import asyncio

import pytest

from astrolabe import SpanCollector, aspan, get_collector, reset_collector, set_collector, span
from astrolabe.backends.sqlite import SQLiteBackend


@pytest.fixture
def backend(tmp_path):
    b = SQLiteBackend(str(tmp_path / "test.db"))
    yield b
    b.close()


def test_sync_span_lifecycle_and_flush(backend):
    c = SpanCollector(backend, host="app")
    with c.span("ask", question="q1") as root:
        assert root.span_id == c.root_span_id
        with c.span("hop", parent_id=root.span_id, hop_number=1):
            pass
        root.set_attrs(hop_count=1)

    assert c.flush() == 2
    trace = backend.get_trace(c.global_trace_id)
    assert trace["status"] == "done"
    assert trace["host"] == "app"
    assert trace["metadata"]["hop_count"] == 1  # host field lives in metadata
    assert trace["metadata"]["question"] == "q1"
    assert trace["duration_ms"] >= 0

    spans = backend.get_trace_spans(c.global_trace_id)
    assert [s["operation"] for s in spans] == ["ask", "hop"]
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]


def test_sync_span_error_marking(backend):
    c = SpanCollector(backend)
    with pytest.raises(RuntimeError, match="boom"):
        with c.span("ask", question="q"):
            raise RuntimeError("boom")

    assert c.flush() == 1
    trace = backend.get_trace(c.global_trace_id)
    assert trace["status"] == "failed"
    assert "boom" in trace["error"]
    span_row = backend.get_trace_spans(c.global_trace_id)[0]
    assert span_row["status"] == "error"


def test_async_span_lifecycle():
    async def scenario(backend):
        c = SpanCollector(backend, host="app")
        async with c.aspan("ask", question="q1") as root:
            async with c.aspan("tool.search", parent_id=root.span_id,
                               tool_args={"q": "x"}) as tool:
                tool.set_attrs(result_count=5)
            root.set_attrs(hop_count=1)
        return c

    def run(backend):
        c = asyncio.run(scenario(backend))
        assert c.flush() == 2
        trace = backend.get_trace(c.global_trace_id)
        assert trace["status"] == "done"
        spans = backend.get_trace_spans(c.global_trace_id)
        assert spans[1]["metadata"]["result_count"] == 5
        assert spans[1]["duration_ms"] >= 0

    b = SQLiteBackend(":memory:")
    try:
        run(b)
    finally:
        b.close()


def test_async_span_error_marking():
    async def scenario(backend):
        c = SpanCollector(backend)
        try:
            async with c.aspan("ask"):
                raise ValueError("bad input")
        except ValueError:
            pass
        return c

    def run(backend):
        c = asyncio.run(scenario(backend))
        assert c.flush() == 1
        assert backend.get_trace(c.global_trace_id)["status"] == "failed"

    b = SQLiteBackend(":memory:")
    try:
        run(b)
    finally:
        b.close()


def test_ambient_span_noop_without_collector():
    """Without an installed collector, ambient spans are transparent no-ops."""
    with span("hop", hop_number=1) as h:
        assert h.span_id == ""
        h.set_attrs(foo=1)

    async def scenario():
        async with aspan("tool.search"):
            pass

    asyncio.run(scenario())


def test_ambient_span_uses_context_collector(tmp_path):
    b = SQLiteBackend(str(tmp_path / "t.db"))
    try:
        c = SpanCollector(b, host="amb")
        token = set_collector(c)

        # ambient spans inside the collector's context attach to it
        with span("ask") as root:
            with span("hop", parent_id=root.span_id):
                pass
        c.flush()
        reset_collector(token)

        assert b.count_traces() == 1
        assert b.get_trace(c.global_trace_id)["host"] == "amb"
        assert [s["operation"] for s in b.get_trace_spans(c.global_trace_id)] == ["ask", "hop"]
    finally:
        b.close()


def test_get_collector_returns_none_by_default():
    assert get_collector() is None
