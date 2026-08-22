"""SQLite implementation of StorageBackend."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..schema import SCHEMA
from ..storage import StorageBackend

# Filter operators supported on metadata JSON values (jsonpath__op → value).
_OPS = {
    "eq": "= ?",
    "contains": "LIKE ?",
    "gt": "> ?",
    "gte": ">= ?",
    "lt": "< ?",
    "lte": "<= ?",
}


def _norm_value(op: str, v) -> object:
    if op == "contains":
        return f"%{v}%"
    if op in ("gt", "gte", "lt", "lte") and isinstance(v, str):
        # json_extract returns INTEGER/REAL/TEXT; a string-bound value would
        # sort after all numbers (SQLite type order) and silently match
        # nothing. Coerce numeric-looking strings so HTTP query params work.
        s = v.strip()
        try:
            if s.lstrip("+-").isdigit():
                return int(s)
            if s.replace(".", "", 1).lstrip("+-").isdigit():
                return float(s)
        except ValueError:
            pass
    return v


class SQLiteBackend(StorageBackend):
    """SQLite-backed storage. One connection, WAL mode, schema auto-created."""

    def __init__(self, path: str = "~/.astrolabe/astrolabe.db"):
        path = str(Path(path).expanduser())
        parent = Path(path).parent
        if str(parent) not in (".", ""):
            parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # ── write ─────────────────────────────────────────────────
    def upsert_trace(
        self, trace_id, *, host, root_span_id, status, duration_ms,
        error, metadata, created_at, updated_at,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO traces(trace_id, host, root_span_id,
                 status, duration_ms, error, metadata, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (trace_id, host, root_span_id, status, duration_ms, error,
             json.dumps(metadata, ensure_ascii=False), created_at, updated_at),
        )

    def insert_span(
        self, span_id, trace_id, parent_span_id, operation,
        duration_ms, status, metadata, created_at,
    ) -> None:
        self.conn.execute(
            """INSERT INTO trace_spans(span_id, trace_id, parent_span_id,
                 operation, duration_ms, status, metadata, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (span_id, trace_id, parent_span_id, operation, duration_ms,
             status, json.dumps(metadata, ensure_ascii=False), created_at),
        )

    # ── query ─────────────────────────────────────────────────
    def get_trace(self, trace_id: str, *, host: str = "") -> dict | None:
        if host:
            row = self.conn.execute(
                "SELECT * FROM traces WHERE trace_id = ? AND host = ?",
                (trace_id, host),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        return self._row_dict(row) if row else None

    def list_traces(self, limit: int = 20, offset: int = 0, *,
                    status: str = "", host: str = "",
                    filters: dict | None = None) -> list[dict]:
        sql = "SELECT * FROM traces"
        where, params = self._where(status=status, host=host, filters=filters)
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._row_dict(r)
                for r in self.conn.execute(sql, params).fetchall()]

    def count_traces(self, *, status: str = "", host: str = "") -> int:
        sql = "SELECT COUNT(*) c FROM traces"
        where, params = self._where(status=status, host=host)
        if where:
            sql += " WHERE " + where
        return self.conn.execute(sql, params).fetchone()["c"]

    def get_trace_spans(self, trace_id: str, *, host: str = "") -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM trace_spans WHERE trace_id = ?
               ORDER BY created_at, rowid""",
            (trace_id,),
        ).fetchall()
        spans = [self._row_dict(r) for r in rows]
        if not host:
            return spans
        # host-scoped: only the subtree rooted at that host's root span
        root_id = self._root_span_id(trace_id, host)
        if root_id is None:
            return []
        children: dict[str, list[dict]] = {}
        for s in spans:
            children.setdefault(s["parent_span_id"] or "", []).append(s)
        out: list[dict] = []
        queue = [root_id]
        while queue:
            sid = queue.pop(0)
            for s in spans:
                if s["span_id"] == sid:
                    out.append(s)
                    break
            queue.extend(c["span_id"] for c in children.get(sid, []))
        return out

    def _root_span_id(self, trace_id: str, host: str) -> str | None:
        row = self.conn.execute(
            "SELECT root_span_id FROM traces WHERE trace_id = ? AND host = ?",
            (trace_id, host),
        ).fetchone()
        return row["root_span_id"] if row and row["root_span_id"] else None

    def build_span_tree(self, trace_id: str, *, host: str = "") -> dict | None:
        spans = self.get_trace_spans(trace_id, host=host)
        if not spans:
            return None
        trace = self.get_trace(trace_id, host=host)

        children_by_parent: dict[str, list[dict]] = {}
        for s in spans:
            pid = s["parent_span_id"] or "__root__"
            children_by_parent.setdefault(pid, []).append(s)

        root = next((s for s in spans if s["parent_span_id"] is None), None)
        if root is None:
            return {"trace": trace, "tree": None}

        def nest(span: dict) -> dict:
            span = dict(span)
            span["children"] = [
                nest(dict(c))
                for c in children_by_parent.get(span["span_id"], [])
            ]
            return span

        return {"trace": trace, "tree": nest(root)}

    def stats(self) -> dict:
        conn = self.conn
        overall = conn.execute(
            """SELECT COUNT(*) as total,
                      COALESCE(AVG(duration_ms), 0) as avg_ms,
                      COALESCE(MIN(duration_ms), 0) as min_ms,
                      COALESCE(MAX(duration_ms), 0) as max_ms
               FROM traces WHERE status = 'done'"""
        ).fetchone()
        # Distribution by hop count (host sets hop_count in metadata when
        # the concept exists; traces without it are excluded)
        by_hops = conn.execute(
            """SELECT json_extract(metadata, '$.hop_count') as hop_count,
                      COUNT(*) as cnt,
                      COALESCE(AVG(duration_ms), 0) as avg_ms
               FROM traces
               WHERE status = 'done'
                 AND json_extract(metadata, '$.hop_count') IS NOT NULL
               GROUP BY json_extract(metadata, '$.hop_count')
               ORDER BY hop_count"""
        ).fetchall()
        tool_stats = conn.execute(
            """SELECT operation, COUNT(*) as calls,
                      COALESCE(AVG(duration_ms), 0) as avg_ms,
                      COALESCE(MAX(duration_ms), 0) as max_ms
               FROM trace_spans
               WHERE operation LIKE 'tool.%'
               GROUP BY operation ORDER BY avg_ms DESC"""
        ).fetchall()
        failed = conn.execute(
            "SELECT COUNT(*) c FROM traces WHERE status = 'failed'"
        ).fetchone()["c"]
        return {
            "overall": dict(overall),
            "failed_count": failed,
            "by_hops": [dict(r) for r in by_hops],
            "tool_stats": [dict(r) for r in tool_stats],
        }

    # ── helpers ───────────────────────────────────────────────
    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["metadata"] = json.loads(d["metadata"] or "{}")
        return d

    @staticmethod
    def _where(*, status: str = "", host: str = "",
               filters: dict | None = None) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if host:
            clauses.append("host = ?")
            params.append(host)
        for key, value in (filters or {}).items():
            if "__" in key:
                path, op = key.rsplit("__", 1)
            else:
                path, op = key, "eq"
            if op not in _OPS:
                raise ValueError(
                    f"unsupported filter op {op!r} (key={key!r}); "
                    f"supported: {', '.join(sorted(_OPS))}")
            clauses.append(f"json_extract(metadata, ?) {_OPS[op]}")
            params += [f"$.{path}", _norm_value(op, value)]
        return " AND ".join(clauses), params
