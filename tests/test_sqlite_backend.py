"""StorageBackend / SQLiteBackend tests."""

from __future__ import annotations

import pytest

from astrolabe.backends.sqlite import SQLiteBackend


@pytest.fixture
def backend(tmp_path):
    b = SQLiteBackend(str(tmp_path / "test.db"))
    yield b
    b.close()


def _mk_trace(b, trace_id, *, host="app", status="done",
              duration_ms=100, metadata=None, created_at="2026-01-01T00:00:00+00:00",
              root_span_id=None):
    b.write_batch([dict(
        trace_id=trace_id, host=host, root_span_id=root_span_id or "root-" + trace_id,
        status=status,
        duration_ms=duration_ms, error="",
        metadata=metadata or {"question": "q" + trace_id},
        created_at=created_at, updated_at=created_at,
    )], [])


def _span(span_id, trace_id, parent_span_id, operation, duration_ms, status, metadata, created_at):
    return dict(span_id=span_id, trace_id=trace_id, parent_span_id=parent_span_id,
                operation=operation, duration_ms=duration_ms, status=status,
                metadata=metadata, created_at=created_at)


def test_batch_and_get_trace(backend):
    _mk_trace(backend, "t1", metadata={"question": "你好", "mode": "sync"})
    row = backend.get_trace("t1")
    assert row is not None
    assert row["trace_id"] == "t1"
    assert row["host"] == "app"
    assert row["status"] == "done"
    assert row["metadata"] == {"question": "你好", "mode": "sync"}  # decoded


def test_batch_retry_updates_same_root(backend):
    _mk_trace(backend, "t1", metadata={"hop_count": 1})
    _mk_trace(backend, "t1", metadata={"hop_count": 5})
    assert backend.count_traces() == 1
    assert backend.get_trace("t1")["metadata"]["hop_count"] == 5


def test_insert_and_get_spans(backend):
    _mk_trace(backend, "t1", root_span_id="s1")
    backend.write_batch([], [
        _span("s1", "t1", None, "ask", 50, "ok", {"question": "q"}, "2026-01-01T00:00:00+00:00"),
        _span("s2", "t1", "s1", "tool.search", 10, "ok", {"result_count": 3}, "2026-01-01T00:00:01+00:00"),
    ])
    spans = backend.get_trace_spans("t1")
    assert [s["span_id"] for s in spans] == ["s1", "s2"]
    assert spans[1]["metadata"] == {"result_count": 3}


def test_list_traces_order_and_paging(backend):
    for i in range(5):
        _mk_trace(backend, f"t{i}", created_at=f"2026-01-0{i+1}T00:00:00+00:00")
    rows = backend.list_traces(limit=2)
    assert [r["trace_id"] for r in rows] == ["t4", "t3"]
    rows = backend.list_traces(limit=2, offset=2)
    assert [r["trace_id"] for r in rows] == ["t2", "t1"]


def test_filter_by_status_and_host(backend):
    _mk_trace(backend, "t1", host="a", status="done")
    _mk_trace(backend, "t2", host="b", status="failed")
    assert backend.count_traces(status="done") == 1
    assert backend.count_traces(host="a") == 1
    assert [r["trace_id"] for r in backend.list_traces(host="b")] == ["t2"]


def test_filter_metadata_json(backend):
    _mk_trace(backend, "t1", metadata={"question": "什么是RAG", "mode": "sync"})
    _mk_trace(backend, "t2", metadata={"question": "什么是LLM", "mode": "async"})
    _mk_trace(backend, "t3", metadata={"question": "RAG和LLM的区别", "mode": "sync"})

    got = backend.list_traces(filters={"question__contains": "RAG"})
    assert {r["trace_id"] for r in got} == {"t1", "t3"}

    got = backend.list_traces(filters={"mode__eq": "async"})
    assert [r["trace_id"] for r in got] == ["t2"]

    got = backend.list_traces(filters={"question__contains": "RAG",
                                       "mode__eq": "sync"})
    assert {r["trace_id"] for r in got} == {"t1", "t3"}


def test_filter_unsupported_op_raises(backend):
    _mk_trace(backend, "t1")
    with pytest.raises(ValueError, match="unsupported filter op"):
        backend.list_traces(filters={"question__bogus": 1})


def test_numeric_filter_string_values(backend):
    """Numeric-looking strings (HTTP query params) must match numbers, not
    silently return empty (SQLite type order would put TEXT after INTEGER)."""
    _mk_trace(backend, "t1", metadata={"hop_count": 1, "score": 3.5})
    _mk_trace(backend, "t2", metadata={"hop_count": 5, "score": 0.5})
    _mk_trace(backend, "t3", metadata={"hop_count": 3, "score": 2.0})

    assert {r["trace_id"] for r in
            backend.list_traces(filters={"hop_count__gt": "2"})} == {"t2", "t3"}
    assert {r["trace_id"] for r in
            backend.list_traces(filters={"hop_count__lte": "1"})} == {"t1"}
    assert {r["trace_id"] for r in
            backend.list_traces(filters={"score__gte": "2"})} == {"t1", "t3"}


def test_build_span_tree(backend):
    _mk_trace(backend, "t1", root_span_id="root")
    backend.write_batch([], [
        _span("root", "t1", None, "ask", 50, "ok", {}, "2026-01-01T00:00:00+00:00"),
        _span("h1", "t1", "root", "hop", 40, "ok", {"hop_number": 1}, "2026-01-01T00:00:01+00:00"),
        _span("tool1", "t1", "h1", "tool.search", 5, "ok", {}, "2026-01-01T00:00:02+00:00"),
        _span("tool2", "t1", "h1", "tool.get_article", 5, "error", {}, "2026-01-01T00:00:03+00:00"),
    ])

    tree = backend.build_span_tree("t1")
    assert tree is not None
    assert tree["trace"]["trace_id"] == "t1"
    root = tree["tree"]
    assert root["operation"] == "ask"
    assert len(root["children"]) == 1
    hop = root["children"][0]
    assert hop["operation"] == "hop"
    assert {c["operation"] for c in hop["children"]} == {
        "tool.search", "tool.get_article"}


def test_build_span_tree_missing(backend):
    assert backend.build_span_tree("nope") is None


def test_stats(backend):
    for i in range(3):
        _mk_trace(backend, f"t{i}", status="done",
                  duration_ms=100 + i * 50, metadata={"hop_count": 2})
    _mk_trace(backend, "tf", status="failed", duration_ms=10)
    _mk_trace(backend, "tno", status="done", duration_ms=30)  # no hop_count
    backend.write_batch([], [
        _span("s1", "t0", None, "tool.search", 20, "ok", {}, "2026-01-01T00:00:00+00:00"),
        _span("s2", "t1", None, "tool.search", 40, "ok", {}, "2026-01-01T00:00:00+00:00"),
        _span("s3", "t2", None, "tool.web_search", 90, "ok", {}, "2026-01-01T00:00:00+00:00"),
    ])

    s = backend.stats()
    assert s["overall"]["total"] == 4          # 3 done with hop + 1 done without
    assert s["overall"]["avg_ms"] == pytest.approx(120.0)  # (100+150+200+30)/4
    assert s["failed_count"] == 1
    # traces without hop_count in metadata are excluded from by_hops
    assert [h["hop_count"] for h in s["by_hops"]] == [2]
    assert s["by_hops"][0]["cnt"] == 3
    assert s["tool_stats"][0]["operation"] == "tool.web_search"  # sorted by avg desc
    assert s["tool_stats"][0]["avg_ms"] == pytest.approx(90.0)


def test_memory_db(tmp_path):
    b = SQLiteBackend(":memory:")
    _mk_trace(b, "t1")
    assert b.get_trace("t1")["status"] == "done"
    b.close()


def test_default_path_expansion(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("HOME", str(tmp_path))
    b = SQLiteBackend("~/.astrolabe/astrolabe.db")
    assert b.path == str(tmp_path / ".astrolabe" / "astrolabe.db")
    assert (tmp_path / ".astrolabe" / "astrolabe.db").exists()
    b.close()
