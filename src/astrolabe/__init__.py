"""Astrolabe — 记录 AI 行为路径的可观测库。

低侵入接入 agent / LLM 系统：入口处包一个 trace/atrace，业务代码用
span()/aspan() 上下文管理器埋点，span 自动记录耗时/状态/元数据，
flush 后写入本地 SQLite，支持嵌套 span 树查询与聚合指标。

    from astrolabe import Astrolabe, aspan

    a = Astrolabe("~/.astrolabe/astrolabe.db")

    async with a.atrace("ask", host="my-agent", question="..."):
        async with aspan("hop", hop_number=1):
            ...
"""

from .api import Astrolabe
from .collector import SpanCollector, SpanHandle, aspan, span
from .context import (
    get_collector,
    get_trace_context,
    reset_collector,
    set_collector,
)
from .storage import StorageBackend
from .metadata import CaptureLimits

__version__ = "0.4.0"

__all__ = [
    "Astrolabe",
    "SpanCollector",
    "SpanHandle",
    "StorageBackend",
    "CaptureLimits",
    "get_collector",
    "get_trace_context",
    "set_collector",
    "reset_collector",
    "span",
    "aspan",
    "__version__",
]
