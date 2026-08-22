"""TUI tree rendering tests (rich output captured to StringIO)."""

from __future__ import annotations

import io

import pytest

from astrolabe import Astrolabe, aspan, span
from tui import build_tree, list_traces_table, main


@pytest.fixture
def demo_db(tmp_path):
    """A db with one 2-hop trace containing tool calls + metadata."""
    a = Astrolabe(str(tmp_path / "t.db"))

    def fill():
        with a.trace("ask", host="demo", question="什么是RAG?") as c:
            with span("hop", hop_number=1) as h1:
                with span("tool.search", tool_args={"q": "RAG"}):
                    pass
                h1.set_attrs(llm_model="deepseek-v4", is_final=False)
            with span("hop", hop_number=2):
                with span("tool.get_article", tool_args={"id": "a1"}):
                    pass

    fill()
    yield a
    a.close()


def _render(tree) -> str:
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100).print(tree)
    return buf.getvalue()


def test_build_tree_contains_all_operations(demo_db):
    tid = demo_db.list_traces()[0]["trace_id"]
    out = _render(build_tree(demo_db, tid))
    for op in ("ask", "hop #1", "hop #2", "tool.search", "tool.get_article"):
        assert op in out
    assert "什么是RAG?" in out        # root question shown
    assert "deepseek-v4" in out       # hop metadata shown
    assert "args(q=RAG)" in out       # tool args shown


def test_tree_nesting_order(demo_db):
    """Children appear indented under parents, in order."""
    tid = demo_db.list_traces()[0]["trace_id"]
    out = _render(build_tree(demo_db, tid))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # ask first, then hop#1 before its tool.search
    assert lines[0].count("ask") == 1
    hop1_idx = next(i for i, ln in enumerate(lines) if "hop #1" in ln)
    tool_idx = next(i for i, ln in enumerate(lines) if "tool.search" in ln)
    assert tool_idx > hop1_idx
    assert "tool.search" in lines[tool_idx]


def test_build_tree_missing_trace(demo_db):
    with pytest.raises(KeyError, match="trace not found"):
        build_tree(demo_db, "no-such-id")


def test_main_renders_tree(demo_db, tmp_path):
    tid = demo_db.list_traces()[0]["trace_id"]
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--db", str(tmp_path / "t.db"), "--trace", tid],
              out=out, err=err)
    assert rc == 0
    assert "ask" in out.getvalue()
    assert "tool.search" in out.getvalue()
    assert err.getvalue() == ""


def test_main_list(demo_db, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--db", str(tmp_path / "t.db"), "--list"], out=out, err=err)
    assert rc == 0
    assert "trace_id" in out.getvalue()
    assert "什么是RAG?" in out.getvalue()
    assert err.getvalue() == ""


def test_main_missing_trace_id(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--db", str(tmp_path / "t.db")], out=out, err=err)
    assert rc == 2


def test_main_unknown_trace(demo_db, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = main(["--db", str(tmp_path / "t.db"), "--trace", "nope"],
              out=out, err=err)
    assert rc == 1
    assert "trace not found" in err.getvalue()


def test_list_traces_table(demo_db):
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100).print(
        list_traces_table(demo_db))
    assert "demo" in buf.getvalue()


def test_async_spans_render():
    """Tree rendering works for async-collected traces too."""
    import asyncio
    a = Astrolabe(":memory:")

    async def scenario():
        async with a.atrace("ask", host="demo", question="q") as c:
            async with aspan("hop", hop_number=1):
                async with aspan("tool.search", tool_args={"q": "x"}):
                    pass

    asyncio.run(scenario())
    try:
        tid = a.list_traces()[0]["trace_id"]
        out = _render(build_tree(a, tid))
        assert "tool.search" in out
        assert "args(q=x)" in out
    finally:
        a.close()
