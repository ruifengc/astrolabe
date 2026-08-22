"""Span-stack tests: automatic parent resolution for nested spans."""

from __future__ import annotations

import asyncio

import pytest

from astrolabe import Astrolabe, aspan, span
from astrolabe.backends.sqlite import SQLiteBackend


@pytest.fixture
def a(tmp_path):
    a = Astrolabe(str(tmp_path / "t.db"))
    yield a
    a.close()


def _ops_by_trace(a, trace_id):
    return {s["operation"]: s["parent_span_id"] for s in a.get_trace_spans(trace_id)}


def test_nested_sync_spans_auto_parent(a):
    """Nested span() blocks attach to the enclosing span automatically."""
    with a.trace("ask", host="app") as c:
        with span("hop", hop_number=1):
            with span("tool.search"):
                pass
            with span("tool.get_article"):
                pass
        with span("hop", hop_number=2):
            pass

    spans = a.get_trace_spans(c.global_trace_id)
    parents = {s["operation"]: s["parent_span_id"] for s in spans}
    ask_id = next(s["span_id"] for s in spans if s["operation"] == "ask")
    hop_ids = [s["span_id"] for s in spans if s["operation"] == "hop"]
    assert parents["ask"] is None
    assert parents["hop"] == ask_id          # both hops attach to ask
    assert parents["tool.search"] == hop_ids[0]      # under hop1
    assert parents["tool.get_article"] == hop_ids[0]  # under hop1
    assert spans[0]["span_id"] == c.root_span_id


def test_async_nested_auto_parent(a):
    async def scenario():
        async with a.atrace("ask", host="app") as c:
            async with aspan("hop", hop_number=1) as hop:
                async with aspan("tool.search"):
                    pass
            return c

    c = asyncio.run(scenario())
    spans = a.get_trace_spans(c.global_trace_id)
    by_op = {s["operation"]: s for s in spans}
    assert by_op["tool.search"]["parent_span_id"] == by_op["hop"]["span_id"]
    assert by_op["hop"]["parent_span_id"] == by_op["ask"]["span_id"]
    assert by_op["ask"]["parent_span_id"] is None


def test_bare_span_at_entry_attaches_to_root(a):
    """A span opened with an empty stack attaches to the trace root."""
    with a.trace("ask", host="app"):
        with span("hop", hop_number=1):
            pass
        with span("tool.search"):  # stack empty again → root
            pass

    trace = a.list_traces()[0]
    spans = a.get_trace_spans(trace["trace_id"])
    by_op = {s["operation"]: s for s in spans}
    assert by_op["hop"]["parent_span_id"] == by_op["ask"]["span_id"]
    assert by_op["tool.search"]["parent_span_id"] == by_op["ask"]["span_id"]


def test_explicit_parent_overrides_stack(a):
    with a.trace("ask", host="app") as c:
        with span("hop") as hop:
            # explicit parent_id beats the stack (attaches to root instead)
            with span("orphan", parent_id=c.root_span_id):
                pass
            assert hop.span_id != c.root_span_id

    trace = a.list_traces()[0]
    by_op = {s["operation"]: s for s in a.get_trace_spans(trace["trace_id"])}
    assert by_op["orphan"]["parent_span_id"] == c.root_span_id


def test_explicit_none_starts_new_trace(a):
    """parent_id=None inside an active trace starts an independent trace."""
    with a.trace("ask", host="app"):
        with span("hop"):
            pass
        with span("subtask", parent_id=None):  # new independent trace
            pass

    assert a.count_traces() == 2
    # two independent roots exist: ask's root and subtask's root
    root_count = a.backend.conn.execute(
        "SELECT COUNT(*) c FROM trace_spans WHERE parent_span_id IS NULL"
    ).fetchone()["c"]
    assert root_count == 2
    # subtask is its own trace, not a child of ask
    traces = a.list_traces()
    spans_by_trace = {t["trace_id"]: a.get_trace_spans(t["trace_id"]) for t in traces}
    subtask_trace = next(t["trace_id"] for t in traces
                         if any(s["operation"] == "subtask" for s in spans_by_trace[t["trace_id"]]))
    assert [s["operation"] for s in spans_by_trace[subtask_trace]] == ["subtask"]


def test_stack_isolation_across_tasks(a):
    """Concurrent asyncio tasks keep independent stacks (copy-on-write)."""

    async def worker(name):
        async with aspan("worker", worker=name):
            await asyncio.sleep(0.01)
            async with aspan("tool", worker=name):
                await asyncio.sleep(0.01)
            async with aspan("tool", worker=name):
                await asyncio.sleep(0.01)

    async def scenario():
        async with a.atrace("ask", host="app") as c:
            await asyncio.gather(worker("w1"), worker("w2"))
            return c

    c = asyncio.run(scenario())
    spans = a.get_trace_spans(c.global_trace_id)
    workers = [s for s in spans if s["operation"] == "worker"]
    assert len(workers) == 2

    tools = [s for s in spans if s["operation"] == "tool"]
    assert len(tools) == 4
    # every tool attaches to its own worker, never to the sibling's
    for t in tools:
        worker_id = t["parent_span_id"]
        assert worker_id in {w["span_id"] for w in workers}
    # each worker has exactly 2 tools under it
    from collections import Counter
    counts = Counter(t["parent_span_id"] for t in tools)
    assert set(counts.values()) == {2}
    # both workers attach to ask (root)
    assert all(w["parent_span_id"] == c.root_span_id for w in workers)


def test_dangling_explicit_parent_degrades_to_root(a):
    """An explicit parent_id that doesn't exist (stale / another collector)
    degrades to an independent root instead of breaking the flush."""
    with a.trace("ask", host="app") as c:
        with span("orphan", parent_id="nonexistent-span-id"):
            pass

    # orphan becomes an independent trace (same semantics as parent_id=None)
    assert a.count_traces() == 2
    orphan_trace = next(t["trace_id"] for t in a.list_traces()
                        if t["trace_id"] != c.global_trace_id)
    spans = a.get_trace_spans(orphan_trace)
    orphan = spans[0]
    assert [s["operation"] for s in spans] == ["orphan"]
    assert orphan["parent_span_id"] is None          # independent root
    assert orphan["metadata"]["_dangling_parent"] == "nonexistent-span-id"
    assert orphan["trace_id"] == orphan["span_id"]   # own trace
    # root span of the collector is still the first span (ask), not the orphan
    assert c.root_span_id is not None


def test_stack_cleanup_after_exception(a):
    """Stack unwinds correctly even when a span body raises."""
    with a.trace("ask", host="app"):
        with pytest.raises(ValueError):
            with span("bad"):
                with span("inner"):
                    raise ValueError("x")
        # after the exception, the stack is empty → next span attaches to root
        with span("after", step=2):
            pass

    trace = a.list_traces()[0]
    by_op = {s["operation"]: s for s in a.get_trace_spans(trace["trace_id"])}
    assert by_op["after"]["parent_span_id"] == by_op["ask"]["span_id"]
    assert by_op["bad"]["status"] == "error"
    assert by_op["inner"]["status"] == "error"
