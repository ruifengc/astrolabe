"""Astrolabe facade (api.py) tests: entry points + query delegation."""

from __future__ import annotations

import asyncio

from astrolabe import Astrolabe, aspan, span
from astrolabe.backends.sqlite import SQLiteBackend


def test_trace_sync_entry(tmp_path):
    a = Astrolabe(str(tmp_path / "t.db"))
    try:
        with a.trace("ask", host="app", question="q1") as collector:
            with span("hop", hop_number=1):
                pass
            collector.root_span_id  # available for manual inspection

        assert a.count_traces() == 1
        t = a.list_traces()[0]
        assert t["status"] == "done"
        assert t["host"] == "app"
        assert t["metadata"]["question"] == "q1"
        spans = a.get_trace_spans(t["trace_id"])
        assert [s["operation"] for s in spans] == ["ask", "hop"]
    finally:
        a.close()


def test_atrace_async_entry(tmp_path):
    a = Astrolabe(str(tmp_path / "t.db"))

    async def scenario():
        async with a.atrace("ask", host="app", question="q2") as collector:
            async with aspan("tool.search", tool_args={"q": "x"}):
                pass
            assert collector.root_span_id

    try:
        asyncio.run(scenario())
        assert a.count_traces() == 1
        t = a.list_traces()[0]
        assert t["metadata"]["question"] == "q2"
    finally:
        a.close()


def test_trace_failure_status(tmp_path):
    a = Astrolabe(str(tmp_path / "t.db"))
    try:
        try:
            with a.trace("ask", host="app", question="boom"):
                raise ValueError("kapow")
        except ValueError:
            pass
        t = a.list_traces()[0]
        assert t["status"] == "failed"
        assert "kapow" in t["error"]
    finally:
        a.close()


def test_host_filtering(tmp_path):
    a = Astrolabe(str(tmp_path / "t.db"))
    try:
        with a.trace("job", host="alpha", name="a"):
            pass
        with a.trace("job", host="beta", name="b"):
            pass

        assert a.count_traces(host="alpha") == 1
        assert [t["host"] for t in a.list_traces(host="beta")] == ["beta"]
        assert [t["host"] for t in a.list_traces()] == ["beta", "alpha"]
    finally:
        a.close()


def test_stats_and_tree_via_facade(tmp_path):
    a = Astrolabe(str(tmp_path / "t.db"))
    try:
        with a.trace("ask", host="app", question="q") as c:
            with span("tool.search"):
                pass

        s = a.stats()
        assert s["overall"]["total"] == 1
        assert s["tool_stats"][0]["operation"] == "tool.search"

        tree = a.build_span_tree(a.list_traces()[0]["trace_id"])
        assert tree["tree"]["operation"] == "ask"
        assert tree["tree"]["children"][0]["operation"] == "tool.search"
    finally:
        a.close()


def test_flush_failure_does_not_mask_business_exception(tmp_path):
    """If flush fails (broken backend), the host's own exception must still
    propagate and the collector context must be reset."""
    class BrokenBackend(SQLiteBackend):
        def _commit(self):
            raise RuntimeError("disk on fire")

    backend = BrokenBackend(str(tmp_path / "t.db"))
    a = Astrolabe(backend=backend)
    try:
        try:
            with a.trace("ask", host="app"):
                raise ValueError("business error")
        except ValueError:
            pass  # business exception not masked by the flush failure
        else:
            raise AssertionError("business exception was swallowed")

        # context reset: ambient spans afterwards are no-ops again
        from astrolabe import get_collector
        assert get_collector() is None
        assert not backend.conn.in_transaction
        assert backend.count_traces() == 0
    finally:
        a.close()


def test_custom_backend_injection(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "custom.db"))
    a = Astrolabe(backend=backend)
    try:
        with a.trace("ask", host="custom"):
            pass
        assert a.count_traces() == 1
        assert backend.count_traces() == 1
    finally:
        a.close()
        backend.close()
