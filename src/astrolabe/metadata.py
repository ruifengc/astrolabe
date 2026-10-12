"""Copy input once; limit captured data once when sealing a persistence record."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureLimits:
    """Optional persisted-data bounds; diagnostics use reserved _astrolabe."""

    max_spans: int | None = None
    max_string_length: int | None = None
    max_metadata_bytes: int | None = None

    def __post_init__(self):
        for name, minimum in (("max_spans", 1), ("max_string_length", 1),
                              ("max_metadata_bytes", 128)):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError(f"{name} must be an integer >= {minimum}")


def _issue(diagnostics: dict, path: str, reason: str):
    issues = diagnostics.setdefault("metadata_issues", [])
    if len(issues) < 20:
        issues.append(f"{path[:160]}: {reason}")


def snapshot(attrs: dict, diagnostics: dict) -> dict:
    """Copy only newly supplied attributes and report unsupported values.

    Avoid user __str__/__repr__/__deepcopy__; protect diagnostic paths from
    string limits. Captured containers are at most 64 levels deep.
    """
    active: set[int] = set()

    def copy(value, path: str, depth: int):
        kind = type(value)
        if kind is int and value.bit_length() > 2000:
            try:
                str(value)
            except ValueError:
                _issue(diagnostics, path, "integer exceeds JSON conversion limit")
                return "<integer exceeds JSON conversion limit>"
        if value is None or kind in (bool, int, str):
            return value
        if kind is float and math.isfinite(value):
            return value
        if kind not in (dict, list, tuple):
            _issue(diagnostics, path, f"unsupported {kind.__name__}")
            return f"<unsupported {kind.__name__}>"
        if id(value) in active or depth >= 64:
            _issue(diagnostics, path, "cycle or depth limit")
            return "<cycle or depth limit>"
        active.add(id(value))
        try:
            if kind is dict:
                result = {}
                for key, item in value.items():
                    if type(key) is not str:
                        _issue(diagnostics, path, "non-string key omitted")
                    elif depth == 0 and key == "_astrolabe":
                        _issue(diagnostics, "$._astrolabe", "reserved key omitted")
                    else:
                        result[key] = copy(item, f"{path}.{key}", depth + 1)
                return result
            return [copy(item, f"{path}[{i}]", depth + 1)
                    for i, item in enumerate(value)]
        finally:
            active.remove(id(value))

    return copy(attrs, "$", 0)


def freeze(attrs: dict, diagnostics: dict, limits: CaptureLimits) -> dict:
    """Seal a record; internal diagnostics bypass host-string truncation."""
    diagnostics = {**diagnostics}
    if "metadata_issues" in diagnostics:
        diagnostics["metadata_issues"] = list(diagnostics["metadata_issues"])

    def limit_strings(value, path):
        if isinstance(value, str) and len(value) > limits.max_string_length:
            _issue(diagnostics, path, "string truncated")
            return value[:limits.max_string_length]
        if isinstance(value, dict):
            return {key: limit_strings(item, f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [limit_strings(item, f"{path}[{i}]") for i, item in enumerate(value)]
        return value

    result = limit_strings(attrs, "$") if limits.max_string_length else dict(attrs)
    if diagnostics:
        result["_astrolabe"] = diagnostics
    if limits.max_metadata_bytes:
        def size():
            return len(json.dumps(result, allow_nan=False).encode())

        if size() > limits.max_metadata_bytes:
            issues = diagnostics.pop("metadata_issues", [])
            if issues:
                diagnostics["metadata_issue_count"] = len(issues)
            diagnostics["metadata_truncated"] = True
            result["_astrolabe"] = diagnostics
            keys = sorted((key for key in result if key != "_astrolabe"),
                          key=lambda k: len(json.dumps(result[k])) + len(json.dumps(k)), reverse=True)
            for key in keys:
                if size() <= limits.max_metadata_bytes:
                    break
                result.pop(key)
                diagnostics["omitted_fields"] = diagnostics.get("omitted_fields", 0) + 1
            for key in ("metadata_issue_count", "omitted_fields"):
                if size() <= limits.max_metadata_bytes:
                    break
                diagnostics.pop(key, None)
            if size() > limits.max_metadata_bytes:
                result = {"_astrolabe": {"metadata_truncated": True}}
    return result
