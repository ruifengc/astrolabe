"""Backend-independent tree construction and chronological subtree selection."""

from collections import defaultdict, deque


def subtree(spans: list[dict], root_id: str) -> list[dict]:
    children = defaultdict(list)
    for span in spans:
        children[span["parent_span_id"]].append(span["span_id"])
    found = set()
    queue = deque([root_id])
    while queue:
        sid = queue.popleft()
        if sid in found:
            continue
        found.add(sid)
        queue.extend(children[sid])
    return [span for span in spans if span["span_id"] in found]


def build_tree(spans: list[dict], root_id: str) -> dict | None:
    """Build copies iteratively; deep trees need no Python recursion."""
    nodes = {s["span_id"]: {**s, "children": []} for s in subtree(spans, root_id)}
    for sid, node in nodes.items():
        if sid != root_id and node["parent_span_id"] in nodes:
            nodes[node["parent_span_id"]]["children"].append(node)
    return nodes.get(root_id)
