"""Dependency-free, read-only operations CLI for installed Astrolabe users."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from typing import TextIO

from .api import Astrolabe


def _one_line(value, width: int = 100) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: f"\\u{ord(m[0]):04x}", text)
    return text if len(text) <= width else text[:width - 1] + "…"


def _metadata_lines(metadata: dict) -> list[str]:
    # Escape terminal control characters even in full inspection output.
    text = json.dumps(metadata, ensure_ascii=False, indent=2)
    return [re.sub(r"[\x7f-\x9f]", lambda m: f"\\u{ord(m[0]):04x}", line)
            for line in text.splitlines()]


def _filters(values: list[str]) -> dict:
    result = {}
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key:
            raise ValueError("filters must use KEY[__OP]=VALUE")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def _walk(root: dict):
    stack = [(root, "", "")]
    while stack:
        node, prefix, connector = stack.pop()
        yield node, prefix + connector
        children = node.get("children", [])
        child_prefix = prefix + ("    " if connector == "└── " else "│   " if connector else "")
        for index in range(len(children) - 1, -1, -1):
            stack.append((children[index], child_prefix,
                          "└── " if index == len(children) - 1 else "├── "))


def _print_tree(data: dict, out: TextIO, *, details: bool = False):
    trace, root = data["trace"], data["tree"]
    print(f"trace={_one_line(trace['trace_id'])}  host={_one_line(trace['host']) or '(empty)'}  "
          f"{trace['status']}  {trace['duration_ms']}ms  {_one_line(trace['created_at'])}", file=out)
    if root is None:
        print("  (root span missing from this database)", file=out)
        return
    nodes = list(_walk(root))
    usage = {}
    for node, _ in nodes:
        for key in ("llm_prompt_tokens", "llm_completion_tokens"):
            value = node.get("metadata", {}).get(key)
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    print(f"{len(nodes)} spans" + (f"  recorded tokens: input={usage.get('llm_prompt_tokens', 0)} "
          f"output={usage.get('llm_completion_tokens', 0)}" if usage else ""), file=out)
    question = trace.get("metadata", {}).get("question")
    if question:
        print("question: " + _one_line(question, 160), file=out)
    if trace.get("error"):
        print("error: " + _one_line(trace["error"], 160), file=out)
    diagnostics = trace.get("metadata", {}).get("_astrolabe")
    if diagnostics:
        print("capture: " + _one_line(diagnostics, 180), file=out)
    for node, branch in nodes:
        try:
            offset = int((datetime.fromisoformat(node['created_at']) -
                          datetime.fromisoformat(root['created_at'])).total_seconds() * 1000)
            timing = f"+{offset}ms"
        except (ValueError, TypeError):
            timing = ""
        md = node.get("metadata") or {}
        preferred = ("hop_number", "llm_model", "model", "result_count", "is_final",
                     "llm_prompt_tokens", "llm_completion_tokens", "error")
        keys = [key for key in preferred if key in md]
        if not keys:
            keys = list(md)[:3]
        summary = "  ".join(f"{_one_line(key, 40)}={_one_line(md[key], 65)}" for key in keys)
        print(f"{branch}{_one_line(node['operation'])} [{node['status']}] "
              f"{node['duration_ms']}ms {timing}  id={_one_line(node['span_id'])}"
              + (f"  {summary}" if summary else ""), file=out)
        if details:
            for line in _metadata_lines(md):
                print(branch.replace("├── ", "│   ").replace("└── ", "    ") + "    " + line, file=out)
    print("Durations include children; concurrent span durations cannot be added.\n", file=out)


def _common(parser, *, suppress: bool = False):
    default = argparse.SUPPRESS if suppress else "~/.astrolabe/astrolabe.db"
    parser.add_argument("--db", default=default, help="SQLite trace database (opened read-only)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if suppress else False,
                        help="emit JSON for scripts/export")


def _selection(parser, *, status: bool = False):
    parser.add_argument("--host", help="select a system; --host '' selects the empty host")
    parser.add_argument("--filter", action="append", default=[], metavar="KEY[__OP]=VALUE",
                        help="metadata filter; repeat to combine conditions")
    if status:
        parser.add_argument("--status", choices=("done", "failed"), default="")


def _parser():
    parser = argparse.ArgumentParser(prog="astrolabe", description="Inspect AI request trees and recorded metadata")
    _common(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list recent requests")
    _selection(listing, status=True)
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    show = commands.add_parser("show", help="show a trace's system trees, or the latest requests")
    show.add_argument("trace_id", nargs="?")
    show.add_argument("--latest", type=int, help="show the latest N requests (default: 1 without an id)")
    _selection(show, status=True)
    show.add_argument("--details", action="store_true", help="print full metadata below each node")
    inspect = commands.add_parser("span", help="inspect one node's complete recorded metadata")
    inspect.add_argument("trace_id")
    inspect.add_argument("span_id")
    inspect.add_argument("--host")
    stats = commands.add_parser("stats", help="request latency, failures, and per-operation counts")
    _selection(stats)
    stats.add_argument("--operation", help="restrict operation metrics to one exact operation")
    for command in (listing, show, inspect, stats):
        _common(command, suppress=True)
    return parser


def main(argv: list[str] | None = None, *, out: TextIO | None = None,
         err: TextIO | None = None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        filters = _filters(getattr(args, "filter", []))
        if args.command == "show":
            if args.trace_id and (args.latest is not None or filters or args.status):
                raise ValueError("trace_id cannot be combined with --latest, --filter or --status")
            if args.latest is not None and args.latest < 1:
                raise ValueError("--latest must be >= 1")
        with Astrolabe(args.db, readonly=True) as app:
            if args.command == "list":
                selection = dict(host=args.host, status=args.status, filters=filters)
                rows = app.list_traces(args.limit, args.offset, **selection)
                total = app.count_traces(**selection)
                data = dict(total=total, traces=rows)
                if not args.json:
                    print(f"{total} matching requests; showing {len(rows)}", file=out)
                    print("trace_id          host                  status       ms  created_at                 question / metadata", file=out)
                    for row in rows:
                        md = row.get("metadata") or {}
                        summary = md.get("question") or md
                        print(f"{_one_line(row['trace_id'], 32):16}  {_one_line(row['host'], 20):20}  "
                              f"{row['status']:6}  {str(row['duration_ms']):>8}  {_one_line(row['created_at'], 26):26}  "
                              f"{_one_line(summary, 90)}", file=out)
            elif args.command == "show":
                if args.trace_id:
                    if args.host is None:
                        data = app.build_span_trees(args.trace_id)
                    else:
                        item = app.build_span_tree(args.trace_id, host=args.host)
                        data = [item] if item else []
                    if not data:
                        raise ValueError(f"trace not found: {args.trace_id}")
                else:
                    rows = app.list_traces(args.latest or 1, host=args.host,
                                           status=args.status, filters=filters)
                    data = [app.build_span_tree(row['trace_id'], host=row['host']) for row in rows]
                if not args.json:
                    if not data:
                        print("No matching requests.", file=out)
                    for item in data:
                        _print_tree(item, out, details=args.details)
            elif args.command == "span":
                data = next((s for s in app.get_trace_spans(args.trace_id, host=args.host)
                             if s['span_id'] == args.span_id), None)
                if data is None:
                    raise ValueError(f"span not found: {args.span_id}")
                if not args.json:
                    print(f"{_one_line(data['operation'])} [{data['status']}] {data['duration_ms']}ms "
                          f"span={_one_line(data['span_id'])} parent={_one_line(data['parent_span_id'])}", file=out)
                    print("\n".join(_metadata_lines(data['metadata'])), file=out)
            else:
                data = app.stats(host=args.host, filters=filters)
                if args.operation is not None:
                    data['operations'] = app.operation_stats(host=args.host, filters=filters, operation=args.operation)
                if not args.json:
                    metrics = data['overall']
                    print(f"completed={metrics['total']}  failed={data['failed_count']}  "
                          f"avg={metrics['avg_ms']:.1f}ms  min={metrics['min_ms']}ms  max={metrics['max_ms']}ms", file=out)
                    print("operation                         calls       avg ms       max ms", file=out)
                    for row in data['operations']:
                        print(f"{_one_line(row['operation'], 32):32}  {row['calls']:5}  "
                              f"{row['avg_ms']:11.1f}  {row['max_ms']:11}", file=out)
            if args.json:
                # Flat spans in JSON avoids recursion limits on deep trees.
                if args.command == "show":
                    data = [dict(trace=item['trace'], spans=app.get_trace_spans(
                        item['trace']['trace_id'], host=item['trace']['host'])) for item in data]
                print(json.dumps(data, ensure_ascii=True, indent=2), file=out)
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        print("error: " + _one_line(str(exc), 500), file=err)
        return 1
