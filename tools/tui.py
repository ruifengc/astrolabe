"""TUI — terminal tree visualization for traces (ops tool, not part of the
installed library).

This file lives in tools/ and is a repository-side operations utility: it
consumes the astrolabe library like any external user would. Run it from the
repo (rich is a dev dependency):

    uv run python tools/tui.py --db ~/.astrolabe/astrolabe.db --trace <TRACE_ID>
    uv run python tools/tui.py --db ~/.astrolabe/astrolabe.db --list
"""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from astrolabe import Astrolabe


def _span_label(span: dict) -> "Text":
    """Human-readable colored label for one span."""
    from rich.text import Text

    op = span["operation"]
    status = span["status"]
    md = span.get("metadata") or {}

    text = Text()
    if op == "ask":
        text.append(op, style="bold cyan")
    elif op.startswith("hop"):
        text.append(op, style="yellow")
    else:
        text.append(op, style="white")

    if "hop_number" in md:
        text.append(f" #{md['hop_number']}", style="yellow")
    text.append(f" ({span['duration_ms']}ms)", style="dim")

    # status marker
    if status == "error":
        text.append(" [error]", style="bold red")
    else:
        text.append(" [ok]", style="green")

    # metadata summary
    bits: list[tuple[str, str]] = []
    if "llm_model" in md:
        bits.append((str(md["llm_model"]), "magenta"))
    if "result_count" in md:
        bits.append((f"{md['result_count']} results", "blue"))
    if md.get("is_final"):
        bits.append(("final", "bold"))
    if isinstance(md.get("tool_args"), dict):
        args = md["tool_args"]
        joined = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
        bits.append((f"args({joined})", "dim"))

    for value, style in bits:
        text.append(f" {value}", style=style)

    return text


def build_tree(a: Astrolabe, trace_id: str) -> "Tree":
    """Build a rich Tree for a trace (root + nested children)."""
    from rich.tree import Tree

    data = a.build_span_tree(trace_id)
    if data is None:
        raise KeyError(f"trace not found: {trace_id}")
    trace, root = data["trace"], data["tree"]

    root_md = trace.get("metadata") or {}
    root_text = _span_label(root)
    if root_md.get("question"):
        root_text.append(f"  「{root_md['question']}」", style="italic")
    if trace.get("host"):
        root_text.append(f"  host={trace['host']}", style="dim")

    tree = Tree(root_text)
    _attach_children(tree, root)
    return tree


def _attach_children(node: "Tree", span: dict) -> None:
    for child in span.get("children", []):
        branch = node.add(_span_label(child))
        _attach_children(branch, child)


def list_traces_table(a: Astrolabe, limit: int = 20) -> "Table":
    """Recent traces as a rich Table (for --list)."""
    from rich.table import Table
    from rich.text import Text

    table = Table(title="Recent traces", show_lines=False)
    table.add_column("trace_id", style="cyan")
    table.add_column("status")
    table.add_column("ms", justify="right")
    table.add_column("host", style="dim")
    table.add_column("question")

    for t in a.list_traces(limit=limit):
        md = t.get("metadata") or {}
        status = Text(t["status"])
        status.stylize("green" if t["status"] == "done" else "red")
        question = str(md.get("question", ""))[:40]
        table.add_row(
            t["trace_id"], status, str(t["duration_ms"] or "-"),
            t["host"], question,
        )
    return table


def main(argv: list[str] | None = None, *, out: TextIO | None = None,
         err: TextIO | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    ap = argparse.ArgumentParser(
        prog="tools/tui.py",
        description="Astrolabe trace tree visualizer (ops tool)",
    )
    ap.add_argument("--db", default="~/.astrolabe/astrolabe.db",
                    help="path to the SQLite file (default: %(default)s)")
    ap.add_argument("--trace", metavar="TRACE_ID",
                    help="trace id to render as a tree")
    ap.add_argument("--list", action="store_true",
                    help="list recent traces instead of rendering a tree")
    args = ap.parse_args(argv)

    try:
        from rich.console import Console
    except ImportError:  # pragma: no cover
        print("rich is required for the TUI: run `uv sync` in the repo "
              "or `pip install rich`", file=err or sys.stderr)
        return 2

    a = Astrolabe(args.db)
    console = Console(file=out, force_terminal=(out is not None))
    try:
        if args.list:
            console.print(list_traces_table(a))
            return 0
        if not args.trace:
            ap.print_usage(file=err or sys.stderr)
            print("error: --trace TRACE_ID is required (or use --list)",
                  file=err or sys.stderr)
            return 2
        console.print(build_tree(a, args.trace))
        return 0
    except KeyError as exc:
        print(f"error: {exc}", file=err or sys.stderr)
        return 1
    finally:
        a.close()


if __name__ == "__main__":
    raise SystemExit(main())
