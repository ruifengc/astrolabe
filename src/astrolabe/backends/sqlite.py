"""SQLite storage: atomic batches, a shared connection lock, read-only inspection."""

from __future__ import annotations

import json
import math
import sqlite3
from functools import wraps
from pathlib import Path
from threading import RLock

from ..schema import SCHEMA
from ..storage import StorageBackend
from ..tree import subtree

_OPS = {"eq": "= ?", "contains": "LIKE ? ESCAPE '\\'", "gt": "> ?",
        "gte": ">= ?", "lt": "< ?", "lte": "<= ?"}
_TRACE_SQL = """INSERT INTO traces(trace_id, host, root_span_id, status,
    duration_ms, error, metadata, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trace_id, host) DO UPDATE SET root_span_id=excluded.root_span_id,
    status=excluded.status, duration_ms=excluded.duration_ms, error=excluded.error,
    metadata=excluded.metadata, created_at=excluded.created_at, updated_at=excluded.updated_at"""
_SPAN_SQL = """INSERT INTO trace_spans(span_id, trace_id, parent_span_id, operation,
    duration_ms, status, metadata, created_at) VALUES(?,?,?,?,?,?,?,?)
    ON CONFLICT(span_id) DO UPDATE SET trace_id=excluded.trace_id,
    parent_span_id=excluded.parent_span_id, operation=excluded.operation,
    duration_ms=excluded.duration_ms, status=excluded.status,
    metadata=excluded.metadata, created_at=excluded.created_at"""


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def _norm_value(op: str, value):
    if op == "contains":
        # contains means literal substring, including '%' and '_'.
        escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"
    if op in ("gt", "gte", "lt", "lte") and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            try:
                numeric = float(value.strip())
                if math.isfinite(numeric):
                    return numeric
            except ValueError:
                pass
    return value


def _trace_values(trace):
    return tuple(trace[k] for k in ("trace_id", "host", "root_span_id", "status",
                                   "duration_ms", "error")) + (
        json.dumps(trace["metadata"], allow_nan=False), trace["created_at"], trace["updated_at"])


def _span_values(span):
    return tuple(span[k] for k in ("span_id", "trace_id", "parent_span_id", "operation",
                                  "duration_ms", "status")) + (
        json.dumps(span["metadata"], allow_nan=False), span["created_at"])


class SQLiteBackend(StorageBackend):
    def __init__(self, path: str = "~/.astrolabe/astrolabe.db", *, readonly: bool = False):
        self._lock = RLock()
        self.readonly = readonly
        self.path = str(Path(path).expanduser())
        if readonly:
            # mode=ro never creates missing files or runs DDL/changes WAL mode.
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            if readonly:
                self.conn.execute("PRAGMA query_only=ON")
            else:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SCHEMA)
                self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    @_locked
    def close(self) -> None:
        self.conn.close()

    def _commit(self) -> None:
        self.conn.commit()

    def write_batch(self, traces: list[dict], spans: list[dict]) -> None:
        # Validate the entire batch before any SQL is executed.
        trace_values = [_trace_values(t) for t in traces]
        span_values = [_span_values(s) for s in spans]
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                roots = {}
                for trace in traces:
                    key = (trace["trace_id"], trace["host"])
                    if key not in roots:
                        existing = self.conn.execute(
                            "SELECT root_span_id FROM traces WHERE trace_id=? AND host=?", key
                        ).fetchone()
                        roots[key] = existing[0] if existing else trace["root_span_id"]
                    if roots[key] != trace["root_span_id"]:
                        raise ValueError(f"trace {key[0]!r} for host {key[1]!r} already has a different root; use a new trace_id")
                self.conn.executemany(_TRACE_SQL, trace_values)
                self.conn.executemany(_SPAN_SQL, span_values)
                self._commit()
            except BaseException:
                self.conn.rollback()
                raise

    @_locked
    def get_traces(self, trace_id: str) -> list[dict]:
        return [self._row_dict(row) for row in self.conn.execute(
            "SELECT * FROM traces WHERE trace_id = ? ORDER BY host", (trace_id,))]

    @_locked
    def get_trace(self, trace_id: str, *, host: str | None = None) -> dict | None:
        if host is not None:
            row = self.conn.execute("SELECT * FROM traces WHERE trace_id = ? AND host = ?",
                                    (trace_id, host)).fetchone()
            return self._row_dict(row) if row else None
        traces = self.get_traces(trace_id)
        if len(traces) > 1:
            hosts = ", ".join(repr(t["host"]) for t in traces)
            raise ValueError(f"trace {trace_id} has multiple hosts ({hosts}); specify host or use build_span_trees")
        return traces[0] if traces else None

    @_locked
    def list_traces(self, limit: int = 20, offset: int = 0, *, status: str = "",
                    host: str | None = None, filters: dict | None = None) -> list[dict]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        where, params = self._where(status=status, host=host, filters=filters)
        sql = "SELECT * FROM traces" + (f" WHERE {where}" if where else "")
        sql += " ORDER BY created_at DESC, trace_id, host LIMIT ? OFFSET ?"
        return [self._row_dict(row) for row in self.conn.execute(sql, [*params, limit, offset])]

    @_locked
    def count_traces(self, *, status: str = "", host: str | None = None,
                     filters: dict | None = None) -> int:
        where, params = self._where(status=status, host=host, filters=filters)
        sql = "SELECT COUNT(*) FROM traces" + (f" WHERE {where}" if where else "")
        return self.conn.execute(sql, params).fetchone()[0]

    @_locked
    def get_trace_spans(self, trace_id: str, *, host: str | None = None) -> list[dict]:
        spans = [self._row_dict(row) for row in self.conn.execute(
            "SELECT * FROM trace_spans WHERE trace_id = ? ORDER BY created_at, rowid", (trace_id,))]
        if host is None:
            return spans
        trace = self.get_trace(trace_id, host=host)
        return subtree(spans, trace["root_span_id"]) if trace else []

    @_locked
    def build_span_tree(self, trace_id: str, *, host: str | None = None) -> dict | None:
        return super().build_span_tree(trace_id, host=host)

    @_locked
    def build_span_trees(self, trace_id: str) -> list[dict]:
        return super().build_span_trees(trace_id)

    @_locked
    def operation_stats(self, *, host: str | None = None, filters: dict | None = None,
                        operation: str | None = None) -> list[dict]:
        sql, params = "", []
        if host is not None or filters:
            where, params = self._where(host=host, filters=filters)
            sql = """WITH RECURSIVE selected(span_id) AS (
                SELECT root_span_id FROM traces WHERE """ + where + """
                UNION SELECT s.span_id FROM trace_spans s JOIN selected p
                ON s.parent_span_id = p.span_id) """
        sql += "SELECT operation, COUNT(*) calls, COALESCE(AVG(duration_ms),0) avg_ms, COALESCE(MAX(duration_ms),0) max_ms FROM trace_spans"
        clauses = []
        if host is not None or filters:
            clauses.append("span_id IN (SELECT span_id FROM selected)")
        if operation is not None:
            clauses.append("operation = ?")
            params.append(operation)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY operation ORDER BY avg_ms DESC, operation"
        return [dict(row) for row in self.conn.execute(sql, params)]

    @_locked
    def stats(self, *, host: str | None = None, filters: dict | None = None) -> dict:
        where, params = self._where(host=host, filters=filters)
        successful = (where + " AND " if where else "") + "status = 'done'"
        overall = self.conn.execute("""SELECT COUNT(*) total,
            COALESCE(AVG(duration_ms),0) avg_ms, COALESCE(MIN(duration_ms),0) min_ms,
            COALESCE(MAX(duration_ms),0) max_ms FROM traces WHERE """ + successful, params).fetchone()
        failed = (where + " AND " if where else "") + "status = 'failed'"
        failures = self.conn.execute("SELECT COUNT(*) FROM traces WHERE " + failed, params).fetchone()[0]
        operations = self.operation_stats(host=host, filters=filters)
        # Compatibility views for existing hosts; generic consumers use operations.
        by_hops = self.conn.execute("""SELECT json_extract(metadata,'$.hop_count') hop_count,
            COUNT(*) cnt, COALESCE(AVG(duration_ms),0) avg_ms FROM traces WHERE """ + successful + """
            AND json_extract(metadata,'$.hop_count') IS NOT NULL
            GROUP BY hop_count ORDER BY hop_count""", params).fetchall()
        return dict(overall=dict(overall), failed_count=failures, operations=operations,
                    by_hops=[dict(row) for row in by_hops],
                    tool_stats=[row for row in operations if row["operation"].startswith("tool.")])

    @staticmethod
    def _row_dict(row) -> dict:
        result = dict(row)
        if "metadata" in result:
            result["metadata"] = json.loads(result["metadata"] or "{}")
        return result

    @staticmethod
    def _where(*, status: str = "", host: str | None = None, filters: dict | None = None):
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if host is not None:
            clauses.append("host = ?")
            params.append(host)
        for key, value in (filters or {}).items():
            path, op = key.rsplit("__", 1) if "__" in key else (key, "eq")
            if op not in _OPS:
                raise ValueError(f"unsupported filter op {op!r} (key={key!r}); supported: {', '.join(_OPS)}")
            clauses.append(f"json_extract(metadata, ?) {_OPS[op]}")
            params.extend((f"$.{path}", _norm_value(op, value)))
        return " AND ".join(clauses), params
