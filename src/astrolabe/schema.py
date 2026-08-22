"""Astrolabe SQLite schema (DDL only, no runtime imports)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  trace_id     TEXT NOT NULL,
  host         TEXT NOT NULL DEFAULT '',
  root_span_id TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'running',
  duration_ms  INTEGER,
  error        TEXT NOT NULL DEFAULT '',
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT '',
  updated_at   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (trace_id, host)
);
CREATE INDEX IF NOT EXISTS idx_traces_status  ON traces(status);
CREATE INDEX IF NOT EXISTS idx_traces_host    ON traces(host);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);

CREATE TABLE IF NOT EXISTS trace_spans (
  span_id        TEXT PRIMARY KEY,
  trace_id       TEXT NOT NULL,
  parent_span_id TEXT,
  operation      TEXT NOT NULL,
  duration_ms    INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'ok',
  metadata       TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_spans_trace     ON trace_spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent    ON trace_spans(parent_span_id);
CREATE INDEX IF NOT EXISTS idx_spans_operation ON trace_spans(operation);
CREATE INDEX IF NOT EXISTS idx_spans_created   ON trace_spans(created_at);
"""
