# AGENTS.md — Astrolabe 星盘

记录 AI 行为路径的可观测库。低侵入接入 agent / LLM 系统：记录每次请求的
推理链路（LLM 决策、工具调用、耗时、状态），写入本地 SQLite，支持嵌套
span 树查询、聚合指标与 TUI 树状可视化。

## 核心设计原则

1. **核心零运行时依赖**：仅标准库（sqlite3 / contextvars / contextlib）。
   运营工具（tools/）可依赖 rich，不进核心包——被安装的库只含运行时。
2. **两表模型**：
   - `traces`：根 span 物化摘要（一请求每系统一行）——服务列表/聚合查询
   - `trace_spans`：完整 span 树（`parent_span_id` 组层级）——服务详情
   - 根 span 在 flush 时写两张表各一次（物化冗余，换取列表/聚合走窄表）
   - **主键 `(trace_id, host)` 复合**：跨系统链路共享全局 trace_id（首跳生成、
     下游 `trace_id=` 沿用），每系统独立 db 文件、各写一行，互不覆盖
   - `collector.global_trace_id`（全局，可上游传入）≠ `collector.root_span_id`（本系统根 span id）；
     traces 行带 `root_span_id` 列，查询按 host 过滤时用它定位该系统子树
   - flush 失败只记日志不重抛（可观测是旁路，不打断宿主业务），context 必复位
3. **宿主字段全进 metadata JSON**：schema 列只保留跨宿主通用骨架
   （trace_id / host / status / duration_ms / error / 时间戳）。任何宿主特定
   字段（question、mode、hop_count、citations…）一律写 metadata，**绝不新增
   schema 列**。查询用 `json_extract` 过滤。
4. **span 栈自动挂载**：contextvar 维护 per-task span 栈，嵌套 span 自动挂
   栈顶，宿主零参数埋点。`parent_id` 不传（`_UNSET`）→ 自动取栈顶；
   显式传 → 覆盖；显式传 `None` → 开新独立 trace。
5. **ambient 透明降级**：未激活 collector 时模块级 `span()`/`aspan()` 是
   no-op，宿主代码零影响（测试环境可直接跑）。
6. **状态双语义**：`traces.status` = done | failed（请求级），
   `trace_spans.status` = ok | error（span 级）。flush 时 root 状态映射
   ok→done / error→failed。

## 目录结构

```
astrolabe/
├── pyproject.toml           # uv 管理；dev 依赖 pytest + rich
├── src/astrolabe/
│   ├── api.py               # Astrolabe 门面（trace/atrace 入口 + 查询委托）
│   ├── collector.py         # SpanCollector 内核 + span/aspan + span 栈
│   ├── context.py           # contextvars 传播（get/set/reset collector）
│   ├── storage.py           # StorageBackend 抽象接口（写/查/聚合契约）
│   ├── schema.py            # DDL（traces + trace_spans + 索引）
│   └── backends/
│       └── sqlite.py        # SQLiteBackend（默认实现，WAL + 单连接）
├── tools/
│   └── tui.py               # 运营工具：树状可视化（import astrolabe + rich）
└── tests/                   # pytest：api / collector / span_stack / sqlite_backend / tui
```

**包与运营工具的边界**：`src/astrolabe/` 是会被其他项目安装的核心库
（`uv add astrolabe --path ...`），只含运行时；`tools/` 是仓库自带的运营
工具，通过 `import astrolabe` 消费核心，不进安装包（rich 在 dev 组）。

## 常用命令

```bash
cd ~/Documents/project/astrolabe
uv sync                      # 安装 dev 依赖（pytest + rich）
uv run python -m pytest      # 全量测试（当前 50 个）
uv run python -m pytest tests/test_span_stack.py -v   # 单文件

# TUI（运营工具，仓库自带）：渲染指定 trace 的树 / 列出最近 trace
uv run python tools/tui.py --db <db路径> --trace <TRACE_ID>
uv run python tools/tui.py --db <db路径> --list
```

宿主集成：

```bash
uv add astrolabe --path ~/Documents/project/astrolabe
```

## 关键实现细节（Agent 必读，改动前先看）

1. **`_UNSET` 哨兵定义在 collector.py 模块顶部**（`StorageBackend` import 之后）。
   函数默认参数在定义时求值——若定义在类之后会 NameError（踩过）。
2. **span 的 created_at 记录 start 时间**（`_SpanState.started_at_iso`），不是
   flush 时间。父先 start 天然有序，`ORDER BY created_at` 即树序；flush 时
   统一时间戳会导致子先父后（内层先 finish 进 pending）。
3. **span 栈 copy-on-write**：`_push` 新建 list 再 set。asyncio Task 的
   `copy_context()` 浅拷贝 var 值，若直接 append 共享 list，并发任务会串栈。
4. **build_span_tree 的 nest() 必须拷贝子节点**（`nest(dict(c))`）——不拷贝
   则子节点是原始引用，缺少 children 键。
5. **SQLite 单连接 + WAL + check_same_thread=False**：一个 Astrolabe 实例
   共享一个连接，跨线程可用但写并发由 SQLite 锁串行化。
6. **stats 的 by_hops 用 `json_extract(metadata,'$.hop_count')`**：宿主未设置
   该字段的 trace 不出现（`IS NOT NULL` 过滤）——通用库不允许假设宿主有
   "轮数"概念。
7. **新增 span 类型（operation 名）不需要改 schema**：operation 是自由字符串，
   `tool.xxx` 前缀约定区分类型，细节全在 metadata。
8. **ambient span 与 collector.span 走同一解析路径**：`_start` 内
   `parent_id is _UNSET` 时调 `_stack_top_or_root()`。**例外：collector 的
   第一个 span（root_span_id 未设）强制 parent=None，绝不挂外层系统的栈**
   ——嵌套 atrace（下游系统入口在外层 trace 内）若挂外层栈顶会串树，
   flush 沿父链越界 KeyError（v0.3 踩过）。
9. **`_resolve_trace_id` 区分主根与独立根**：主根（== root_span_id）用全局
   `self.global_trace_id`；显式 `parent_id=None` 开的独立 trace 用自身 span_id——
   否则多个独立根物化到同一 (trace_id, host) 行互相 REPLACE。
10. **悬空显式 parent_id 降级为独立根**：`_start` 校验显式 parent_id 是否在
    `_spans` 内，不在则记 `_dangling_parent` 进 metadata 并按独立根处理——
    不抛裸 KeyError 中断整条 trace 写入。
11. **数值过滤参数归一化**：`_norm_value` 对 gt/gte/lt/lte 的字符串值做
    int/float 归一化——SQLite 类型排序 INTEGER < TEXT，字符串绑定值会静默
    匹配不到任何数值（HTTP query 参数天然是字符串，曾静默返回空集）。

## 开发约定

- Python 命令一律 `uv run` 前缀（uv 管理，含 venv）。
- 测试用 `tmp_path` 或 `:memory:`，绝不写默认 `~/.astrolabe/astrolabe.db`。
- 异步测试用 `asyncio.run()` 包裹，不引入 pytest-asyncio。
- 查询一律走 `StorageBackend` 接口（storage.py），SQLite 细节不泄漏到上层。
- **改 schema 列 = 破坏性变更**（旧库需迁移/重建）。优先把字段写进
  metadata JSON，而不是加列。
- commit 格式：`feat/fix(模块): 描述`（如 `feat(tui): 树状可视化`）。

## Agent 注意事项

1. 核心库零依赖是卖点——新功能若需第三方库，放 optional extra 或独立模块。
2. 埋点 API 的语义：宿主只接触 `Astrolabe` / `span` / `aspan` /
   `set_attrs`。`_start`/`_finish`/`flush`/`_patch_attrs` 是内部实现，
   业务代码不该直接调用（`trace`/`atrace` 入口代管 flush）。
3. TUI 验证用真实 db：先造 trace 再 `uv run python tools/tui.py --trace <id>`。
4. 修改后全量跑 `uv run python -m pytest` 再交付。
