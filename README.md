# Astrolabe 星盘

记录 AI 请求的执行路径：模型调用、工具调用、耗时、异常和宿主提供的内容。
用 `trace/atrace` 包住请求入口，在业务步骤中加 `span/aspan`，自动生成调用树，
请求结束后写入本地 SQLite。核心和随包安装的 CLI 都只有标准库依赖。

## 安装与接入

```bash
uv add astrolabe --path ~/Documents/project/astrolabe
```

```python
from astrolabe import Astrolabe, aspan

observability = Astrolabe("./data/traces.db")

async def ask(question):
    async with observability.atrace(
        "ask", host="my-agent", question=question,
    ) as request:
        async with aspan("llm.chat", model="my-model") as call:
            response = await llm.chat(question)
            call.set_attrs(content=response)

        async with aspan("tool.search", tool_args={"q": question}) as tool:
            results = await search(question)
            tool.set_attrs(result_count=len(results))

        request.set_root_attrs(answer=response)
        return response
```

同步代码使用 `with observability.trace(...)` 和 `with span(...)`。
一个应用共享一个 Astrolabe 实例；关闭应用时调用 `observability.close()`，
也可以用 `with Astrolabe(...) as observability` 管理连接。

operation 和 metadata 的字段名都由宿主定义，无需修改表结构。
未激活请求时，模块级 `span/aspan` 和 `set_attrs` 自动 no-op。
Astrolabe 保存宿主埋点提供的数据；没有埋点的调用和模型未返回的内部推理不会自动捕获。

## 运维 CLI：看一次或几次 AI 调用

安装库后即可运行 `astrolabe`，也支持 `python -m astrolabe`。
使用 uv 的项目在命令前加 `uv run`。CLI **只读打开已有数据库**，不创建数据库、
不执行 schema 初始化。默认路径是 `~/.astrolabe/astrolabe.db`。
`--db` 和 `--json` 可以放在子命令前或后。

```bash
# 最近请求：id、系统、状态、耗时、时间和问题摘要
uv run astrolabe --db ./data/traces.db list --limit 20

# 最近一次 / 最近五次请求的调用树
uv run astrolabe --db ./data/traces.db show
uv run astrolabe --db ./data/traces.db show --latest 5

# 指定请求；同一 trace 多个系统时，每个系统各显示一棵树
uv run astrolabe --db ./data/traces.db show TRACE_ID

# 按系统或失败状态筛选
uv run astrolabe show --latest 5 --host knowledge-news-mcp --status failed

# 按任意 metadata 字段筛选；多个条件取交集
uv run astrolabe list --filter 'question__contains=RAG' --filter 'hop_count__gte=2'

# 所有节点的完整 metadata；或只查看某个节点
uv run astrolabe show TRACE_ID --host knowledge-news-mcp --details
uv run astrolabe span TRACE_ID SPAN_ID

# 请求统计、每种 operation 的次数和耗时
uv run astrolabe stats --host knowledge-news-mcp
uv run astrolabe stats --operation tool.search

# JSON 导出，便于进一步分析；show 导出摘要和按时间排序的扁平 span
uv run astrolabe show TRACE_ID --json
```

树的输出包含嵌套关系、operation、状态、总耗时、相对请求开始的时间、span id，
以及模型、token 用量、工具结果数量等已记录的 metadata 摘要。例如：

```text
trace=...  host=demo  done  42ms  ...
4 spans  recorded tokens: input=120 output=32
question: 最近有哪些新闻？
ask [ok] 42ms +0ms id=...
└── hop [ok] 42ms +0ms id=... hop_number=1
    ├── llm.chat [ok] 20ms +0ms id=... model=demo
    └── tool.search [ok] 21ms +20ms id=... result_count=2
```

父节点耗时包含子节点；并发节点的耗时不能直接相加。
显示的 token 用量只汇总节点上已记录的 `llm_prompt_tokens/llm_completion_tokens`，
不是模型计费接口的数据。`--details` 和 `span` 用于查看工具参数、响应内容、异常和采集诊断。

不连接真实模型也能试用，示例中的数据明确标记为模拟调用：

```bash
uv run python examples/demo.py --db /tmp/astrolabe-demo.db
uv run astrolabe --db /tmp/astrolabe-demo.db show --latest 3
uv run astrolabe --db /tmp/astrolabe-demo.db stats
```

旧 `tools/tui.py` 命令保留为兼容入口，参数转换后直接调用同一个 CLI；
树展示、metadata 摘要和只读查询只有一套实现，无需 Rich：

```bash
uv sync
uv run python tools/tui.py --db /tmp/astrolabe-demo.db --list
uv run python tools/tui.py --db /tmp/astrolabe-demo.db --trace TRACE_ID --host demo
```

## 请求生命周期与可靠性

- 每个 collector 有独立的 contextvar span 栈，保存不可变 tuple，以 token 恢复。
  嵌套系统不会共享栈；并发 asyncio Task 自动继承父节点，之后各自维护栈。
- 不传 `parent_id` 自动挂当前父节点；显式传入覆盖父节点；`parent_id=None` 开独立 trace。
  悬空显式 parent 降级为独立 trace，并记录 `_dangling_parent`。
- span 创建时确定所属 trace id、记录开始时间。`set_attrs` 拷贝传入数据，
  span 结束后冻结，后续修改原字典或使用旧 handle 都不会改变记录。
- collector 生命周期为 `active → closing → closed`。入口退出时封存采集并 flush；
  尚未结束的子 span 以 `error/IncompleteSpan` 收尾，根 metadata 标记 `unfinished_spans`。
  请求结束后，继承旧上下文的后台任务继续正常工作，埋点自动 no-op。
  **要完整记录并发工作，请在 trace 范围内 await/gather 它们。**
- 普通异常、异步取消、KeyboardInterrupt/SystemExit 都结束 span，再原样传播。
  请求摘要状态为 `done/failed`，span 状态为 `ok/error`。
- SQLite 预先序列化批次，以完整事务写入两张表；失败回滚，不遗留给下一次请求。
  共享连接用 RLock 覆盖整次事务和查询。span 写入按 id 幂等，批次可以安全重试。
  同一 `(trace_id, host)` 只允许相同根 span 重试；不同根冲突时整批拒绝并记日志，
  保留已有请求，不覆盖摘要或留下新 span。
- 入口捕获持久化异常并记日志，始终恢复 collector 上下文，不覆盖宿主异常。
  无自动重试队列；需要重试时，可保留 collector 并显式 `flush()`。
- SQLite flush 仍同步执行；这是轻量本地存储的取舍，数据库繁忙时会等待 SQLite 锁。

## Metadata 快照、容错与限制

支持 JSON 类型和 tuple（存为列表）。不支持的对象、非有限浮点数、循环引用、
超过 64 层的 metadata 会被替换/跳过，并在保留键 `_astrolabe.metadata_issues`
中记录字段路径；不会调用任意对象的 repr/str/deepcopy，也不会拖垮整条请求。
`_astrolabe` 是库的诊断命名空间；宿主传入该顶层键时会跳过并记诊断。
输入和 `set_attrs` 只拷贝本次提供的新值；span 结束后不接受属性修改，
持久化前统一执行限幅并封存记录，重试直接使用已封存的 metadata。
库诊断单独管理，字符串限制只处理宿主值，保留原始问题路径。

可选限制默认为关闭：

```python
from astrolabe import Astrolabe, CaptureLimits

observability = Astrolabe(
    "./data/traces.db",
    limits=CaptureLimits(
        max_spans=2000,             # 每个 collector，含根；超出后的 span no-op
        max_string_length=16000,    # 单个宿主字符串最多保存的字符数
        max_metadata_bytes=64000,  # 每个 span 的 metadata JSON 字节上限，至少 128
    ),
)
```

字符串截断会记诊断；metadata 超限时优先移除大字段，保留小字段并标记截断。
span 超限时，根 metadata 记录 `dropped_spans`。
`max_spans × max_metadata_bytes` 限制一个 collector 的 span metadata 总体积；
数据库还会包含骨架字段、索引及根摘要副本，不等于数据库文件大小上限。
设定限制作用于持久化记录，不是采集期间的内存上限。
字节限制很小时，完整诊断路径可能压缩为诊断数量；采集截断仍会标记。

## 跨系统传播与查询

```python
from astrolabe import get_trace_context

# 上游：在活跃 trace 内读取，随 HTTP header / MCP 参数传给下游
context = get_trace_context()  # {"trace_id": ..., "host": ...} 或 None

# 下游：沿用全局 id，自建本系统根节点
async with downstream.atrace("ask", host="downstream", trace_id=context["trace_id"]):
    ...
```

每个系统可写自己的 db，共享 trace id 与本系统 root span id 是两个概念。
同一 db 内 `(trace_id, host)` 是摘要复合主键，每系统一行。
同一 host 的同一 trace id 只属于一个本地根 span；相同根的批次可以幂等重试，
新建不同根不能替换已有请求。每次独立请求生成新的 trace id；同一请求内的
多次操作用子 span 记录。

```python
observability.list_traces(host="my-agent", filters={"question__contains": "RAG"})
observability.count_traces(host="my-agent", filters={"question__contains": "RAG"})
observability.get_trace(trace_id, host="my-agent")
observability.get_trace_spans(trace_id, host="my-agent")  # 按开始时间排序
observability.build_span_tree(trace_id, host="my-agent") # {trace, tree}
observability.build_span_trees(trace_id)                  # 多系统：[{trace, tree}, ...]
observability.get_traces(trace_id)                        # 所有系统摘要
observability.stats(host="my-agent")
observability.operation_stats(operation="my-step")       # 通用 operation 指标
```

`host=None`（默认）表示未指定系统；`host=""` 精确选择空 host。
`get_trace/build_span_tree` 在多个 host 时要求显式指定 host，以免混配摘要和树。
`get_trace_spans` 不指定 host 时仍返回全局 id 下所有 span。
CLI 的 `show TRACE_ID` 自动使用多树查询。

metadata 过滤语法为 `jsonpath__op`：`eq/contains/gt/gte/lt/lte`。
`contains` 匹配字面子串（`%/_` 不作为通配符），数值比较支持数字字符串和科学计数法。
`stats.operations` / `operation_stats` 不要求宿主采用特定名字；
`stats.by_hops` 和 `stats.tool_stats` 保留为原有 hop_count/tool.* 约定的兼容视图。

## 数据模型与后端

```text
traces:      trace_id, host, root_span_id, status, duration_ms,
             error, metadata(JSON), created_at, updated_at
             PRIMARY KEY (trace_id, host)
trace_spans: span_id, trace_id, parent_span_id, operation, duration_ms,
             status, metadata(JSON), created_at
```

列表/聚合读摘要表，详情读 span 表，通过 parent 指针还原树。
宿主字段始终进 metadata，本次更新无 schema 变更。
树构建和子树选择是可复用的标准库纯函数，支持超过 Python 递归限制的树深度。

自定义存储通过 `StorageBackend` 接入，写入核心契约是
`write_batch(traces, spans)`：原子提交、失败回滚、批次并发隔离、重试幂等。
查询返回普通 dict，metadata 已解码；树构建有默认实现可复用。
存储接口只有一个写入口；不要求自定义后端实现逐条插入和手动 commit。
冲突检查与写入处于同一事务，来自不同连接的竞争也遵循相同规则。

### 0.4 升级说明

- 0.3 的现有数据库直接使用，无需迁移。
- 原有 `trace/atrace/span/aspan/set_attrs` 和新闻库埋点方式继续使用。
- 查询的空 host 现在表示精确过滤，未指定请省略或传 None。
- 多 host 的单树查询会提示消歧；调用 `build_span_trees` 可显式读取全部系统。
- 自定义后端需要实现新增的批次/多摘要/通用指标接口，并支持新增查询过滤参数。
- 原有逐条写入/commit 接口已移除，自定义后端只需实现批次写入契约。
- 旧 TUI 参数保持兼容，输出统一使用 CLI 的标准库树展示。
- span 结束后的属性更新变为 no-op；后台任务应在请求范围内等待完成。
- 固定 Git revision 的宿主需要更新依赖 revision，才会使用此版本和 CLI。

## 开发

```bash
uv sync
uv run python -m pytest
```

`src/astrolabe/` 是安装包（含标准库 CLI）；`tools/` 是仓库运营工具，不打包。
测试只使用临时文件或内存数据库，不写操作者默认追踪库。

后续方向：OpenAI 兼容代理捕获、MCP 查询接口、多数据库链路聚合、Web 时间线。
