# Astrolabe 星盘

记录 AI 行为路径的可观测库。低侵入接入 agent / LLM 系统：记录每次请求的
推理链路（LLM 决策、工具调用、耗时、状态），写入本地 SQLite，支持嵌套
span 树查询与聚合指标。

- **零运行时依赖**：仅标准库（sqlite3 / contextvars / contextlib）
- **双上下文管理器**：`span()`（同步）+ `aspan()`（异步），共享同一内核
- **透明降级**：未安装 collector 时埋点代码自动 no-op，宿主行为零影响
- **存储可插拔**：`StorageBackend` 接口抽象，默认 SQLite 实现，可自定义

## 安装

```bash
uv add astrolabe --path ~/Documents/project/astrolabe
# 或 pip install -e ~/Documents/project/astrolabe
```

## 快速开始

```python
from astrolabe import Astrolabe, aspan

a = Astrolabe("~/.astrolabe/astrolabe.db")   # 默认路径，可省略

async def ask(question):
    # 入口：创建 collector + 根 span，退出时自动 flush
    async with a.atrace("ask", host="my-agent", question=question):
        # operation 名与 attrs 键都由宿主自定义，原样进 span 的 metadata JSON
        async with aspan("llm.chat", model="deepseek-v4") as h:
            msg = await llm.chat(messages)
            h.set_attrs(llm_content=msg)

        async with aspan("tool.search", tool_args={"q": question}):
            result = await executor.search(question)
```

> 示例里的 `"llm.chat"`、`model`、`tool_args` 只是演示：astrolabe 不预定义
> 任何业务字段，operation 名和 metadata 键完全由宿主自己约定
> （`tool.xxx` 前缀、hop_number 这类领域字段都是宿主侧概念）。

同步宿主用 `a.trace(...)` + `span(...)`：

```python
from astrolabe import Astrolabe, span

a = Astrolabe()
with a.trace("job", host="cron", name="nightly"):
    with span("step1"):
        ...
```

> 埋点代码在未激活 tracing 的环境（如测试）里是透明 no-op——不报错、不丢行为，
> 只是不记录。要激活 tracing 只需在入口使用 `trace`/`atrace`。

### 跨系统链路：共享 trace_id

一条请求可能跨多个系统（如 Hermes → knowledge-news）。每个系统只记录自己的
span（各自独立 db 文件），但共享同一个全局 trace_id：

```python
# 系统 A（第一跳）：自动生成全局 trace_id
async with a.atrace("ask", host="my-agent", question=q) as ca:
    async with aspan("retrieve", limit=10):
        ...
    # 调下游前导出传播上下文，随请求带过去（HTTP header / MCP 参数）
    ctx = get_trace_context()      # {"trace_id": "2e5f...", "host": "my-agent"}

# 系统 B（下游）：接收上游 trace_id，沿用同一链路
async with b.atrace("ask", host="knowledge-news", trace_id=ctx["trace_id"]):
    async with aspan("tool.search"):
        ...
```

- 首跳不传 `trace_id` → 自动生成；下游传上游的 → 沿用
- `trace_id` 与本地根 span id 解耦：`collector.global_trace_id`（全局）≠ `collector.root_span_id`（本系统根）
- `traces` 表按 `(trace_id, host)` 复合主键——同一链路各系统各一行，互不覆盖；
  查询 API 支持 `host=` 参数只看某个系统的子树（get_trace/get_trace_spans/build_span_tree）
- 查询聚合工具（跨多 db / 多 host 合并视图）在 MCP server / Web UI 里程碑实现

### Span 栈：零参数自动挂载

Astrolabe 维护一个 per-task 的 span 栈（contextvar）：进入 span 时压栈，
退出时弹栈，新 span 自动挂到栈顶。**嵌套埋点不需要传任何 id**：

```python
async with aspan("step1"):             # 挂到根
    async with aspan("tool.search"):   # 自动挂到 step1
        ...
```

- `parent_id` 显式传入时优先于栈（用于跨任务挂载等特殊场景）
- `parent_id=None` 显式传时开启一条**独立的新 trace**
- 并发 asyncio 任务各自持有独立栈（copy-on-write，互不串扰）

## 查询

```python
a.list_traces(host="my-agent", status="done")          # 列表（最新在前）
a.list_traces(filters={"question__contains": "RAG"})   # metadata JSON 字段过滤
a.get_trace(trace_id)                                   # 单条
a.get_trace_spans(trace_id)                             # 扁平 span 列表
a.build_span_tree(trace_id)                             # 嵌套树 {trace, tree}
a.stats()                                               # 聚合指标
```

过滤器语法 `jsonpath__op`：`eq` / `contains` / `gt` / `gte` / `lt` / `lte`。

## 数据模型

```
traces:      trace_id, host, status(done|failed), duration_ms,
             error, metadata(JSON), created_at, updated_at
             PRIMARY KEY (trace_id, host)
trace_spans: span_id, trace_id, parent_span_id, operation, duration_ms,
             status(ok|error), metadata(JSON), created_at
```

- `trace_id` 是跨系统共享的全局 id（首跳生成、下游沿用），与根 span 的
  `span_id` 解耦；复合主键 `(trace_id, host)` 保证同一链路各系统互不覆盖
- 宿主特定字段（question、mode、hop_count、citations 等）全部放 `metadata` JSON——
  无需配置、无需迁移，`json_extract` 即可过滤查询。schema 列只保留跨宿主通用的骨架。

## 工作原理

一次请求的数据流：

```
atrace()/trace() 入口                    宿主业务代码
┌──────────────────┐   ┌───────────────────────────────────────┐
│ 创建 SpanCollector │   │ async with aspan("step1"):            │
│ 设置 contextvar   │──▶│   async with aspan("tool.search"):     │
│ 开根 span         │   │     ...  ← 自动挂栈顶，零参数          │
└──────────────────┘   └───────────────────────────────────────┘
                              │ 退出入口时 flush()
                              ▼
                    ┌─────────────────────┐
                    │ 根 span → traces    │  ← 物化摘要（窄表）
                    │ 所有 span → trace_spans │  ← 完整树（全量）
                    └─────────────────────┘
                              │
                    查询：list_traces / stats 走 traces 窄表
                          build_span_tree 从 trace_spans 重建树
```

- **写入时编码层级**：每个 span 创建瞬间就从 span 栈确定 `parent_span_id`，
  树结构在数据里是完整的——查询阶段只是按指针机械还原，无任何推断。
- **flush 由入口代管**：`trace`/`atrace` 退出时自动 flush 全部已结束 span；
  业务代码不接触持久化。
- **状态映射**：flush 时根 span `ok→done`、`error→failed` 写入 traces；
  trace_spans 保留 `ok/error` 原值。

## 设计决策

| 决策 | 理由 |
|------|------|
| 两表（traces + trace_spans） | 列表/聚合走窄表 traces，详情钻取走全量 trace_spans；root span 物化是 OTel 同款模式 |
| 宿主字段全进 metadata JSON | 零配置零迁移；新字段不需要改 schema；`json_extract` 查询兜底 |
| span 栈自动挂载 | 嵌套埋点零参数；contextvar per-task 隔离，并发安全（copy-on-write） |
| 同步/异步双上下文管理器 | 同一内核，sync/async 宿主各取所需 |
| StorageBackend 接口抽象 | 换后端（Postgres/远程）不动 collector 和门面 |
| 透明 no-op | 无 collector 时埋点代码可运行，测试环境零负担 |
| 核心零依赖 | `pip install astrolabe` 即可接入，不拖任何第三方包 |
| 跨系统共享 trace_id + (trace_id, host) 复合主键 | 每系统独立记录自己的 span（独立 db），链路全局可聚合；复合主键防覆盖 |

## TUI 树状可视化（运营工具）

TUI 是仓库自带的**运营工具**，不属于被安装的核心库（核心包只含运行时）。
它像任何外部使用者一样通过 `import astrolabe` 消费库，运行在仓库环境
（rich 是 dev 依赖）：

```bash
# 渲染指定 trace 的树状结构
uv run python tools/tui.py --db <db路径> --trace <TRACE_ID>

# 列出最近 trace 方便找 id
uv run python tools/tui.py --db <db路径> --list
```

输出：操作名 + 耗时 + 状态（ok/error 着色）+ metadata 摘要（宿主自定义键，如模型、结果数、工具参数）。

## 项目结构

```
src/astrolabe/
├── api.py        # Astrolabe 门面（trace/atrace 入口 + 查询委托）
├── collector.py  # SpanCollector 内核 + span/aspan 双上下文管理器 + span 栈
├── context.py    # contextvars 传播
├── storage.py    # StorageBackend 抽象接口
├── backends/
│   └── sqlite.py # SQLiteBackend（默认实现）
└── schema.py     # DDL
tools/
└── tui.py        # 运营工具：树状可视化（不在安装包里，依赖 rich）
```

## 路线图

- [x] v0.1 核心：span 采集 + SQLite 存储 + 查询/聚合 API
- [x] v0.2 span 栈自动挂载 + 宿主字段通用化（metadata JSON）
- [x] v0.3 跨系统传播：共享 trace_id + (trace_id, host) 复合主键 + get_trace_context
- [ ] LLM 代理捕获：OpenAI 兼容 HTTP 代理，LLM 调用零代码自动记录
- [ ] MCP server：把查询能力暴露为 MCP 工具（含多来源聚合查询）
- [ ] Web UI：时间线可视化
