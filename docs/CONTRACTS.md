# AgentWeave（织流）· 模块契约与协作规范

> 本文件是团队协作的**唯一接口事实**。任何跨模块改动先改本文件，再改代码。
> 代码基线：`agentweave/core/types.py`（数据结构）、`agentweave/core/events.py`（事件总线）、
> `agentweave/core/expr.py`（安全表达式）、`agentweave/runtime/context.py`（运行时上下文）。

## 0. 项目定位

**作品名**：织流 AgentWeave —— 面向复杂科研任务的多智能体编排、调度与可信验证平台
**定位**：多智能体编排、调度与可信验证平台
**对标**：LobeHub / DeepSeek Harness 一类 Agent Harness，但聚焦“编排 + 调度 + 可信验证 + 自循环”四件事。
**硬性约束**：

- 纯 Python 标准库（禁止第三方依赖；numpy/pandas 等禁止进入平台代码，示例分析代码也必须用标准库）
- Python 3.10+（验证机使用 `py -3.14`；入口 `python server.py`）
- 平台必须能**离线运行**：默认使用确定性 `MockProvider` 演示，配置 API Key 后可切换任意 OpenAI 兼容大模型
- 中文 UI、中文文档；代码注释中文；变量/函数英文
- 所有写文件动作限制在仓库目录内

## 1. 目录与写权（写作用域）

| 路径 | 负责人 | 内容 |
|---|---|---|
| `agentweave/core/**` | Lead | types/events/expr（**冻结**，改动需 Lead 同意） |
| `agentweave/runtime/context.py`、`agentweave/runtime/orchestrator.py` | Lead | 运行时上下文、运行生命周期编排 |
| `agentweave/workflow/**`、`agentweave/scheduler/**`、`agentweave/verify/**`、`agentweave/loop/**`、`agentweave/runtime/state.py` | core-engine | 工作流模型/DSL/执行引擎、调度器、验证流水线、自循环、运行状态 |
| `agentweave/llm/**`、`agentweave/agents/**`、`agentweave/tools/**` | agents-tools | LLM 提供方与路由、Agent ReAct 运行时、工具注册表/内置工具/MCP、Agent 注册表与记忆 |
| `agentweave/api/**`、`web/**`、`server.py` | web-ui | HTTP/SSE 服务、单页可视化应用 |
| `agentweave/demo/**`、`examples/**`、`scripts/**`、`tests/**` | Lead | 演示场景、数据集、基准脚本、测试 |
| `docs/**` | docs | 方案书、PPT、视频脚本等参赛材料 |

> 跨模块只允许通过本文件定义的接口调用；禁止 import 对方内部私有函数。
> 需要对方新增接口时，发消息给 Lead，由 Lead 更新契约后统一实施。

## 2. 运行时上下文（Lead 提供，各模块只读使用）

`agentweave/runtime/context.py`：

```python
@dataclass
class RuntimeContext:
    bus: EventBus                      # 事件总线
    llm: Any                           # ModelRouter：await ctx.llm.complete(messages, tools=None, ...) -> LLMResult
    tools: Any                         # ToolRegistry：await ctx.tools.call(name, args, ctx=None) -> ToolResult
    agents: Any                        # AgentRegistry：ctx.agents.get(id) / match() / acquire() / release()
    verifier: Any                      # VerificationPipeline：await ctx.verifier.verify(spec, node, output, run) -> VerifyResult
    scheduler: Any                     # Scheduler：scheduler.pick(candidates, now) -> ScheduleDecision|None
    workflows: dict[str, Workflow]     # 已注册工作流
    config: dict                       # 平台配置（llm 配置等）
    artifacts_dir: str                 # 本次运行产物目录
    workspace_dir: str                 # 仓库根目录
    cancel_event: threading.Event      # 取消信号
    auto_approve: bool                 # 人工节点自动通过（演示）
    # 方法（Lead 实现）
    def emit(self, type_, run_id, node_id=None, agent_id=None, payload=None, level="info") -> None
    def log(self, run_id, message, level="info", node_id=None, agent_id=None) -> None
    async def call_agent(self, agent_id, prompt, run, node, context=None, feedback="") -> AgentResult
    async def call_tool(self, name, args) -> ToolResult
    async def human_gate(self, node, run) -> bool
```

**执行引擎不得直接 import `llm/agents/tools` 子包**，只能通过 `ctx` 调用（保证可测试、可替换）。

## 3. 模块接口清单

### 3.1 core-engine

#### `agentweave/workflow/model.py`
```python
class WorkflowStore:
    def __init__(self, workflow_dirs: list[str]) -> None
    def load(self) -> None                              # 读取 */workflow.json
    def all(self) -> list[Workflow]
    def get(self, workflow_id: str) -> Workflow | None
    def save(self, wf: Workflow) -> str                 # 写回 workflows/{id}.json
    def validate(self, wf: Workflow) -> list[str]
```

#### `agentweave/workflow/dsl.py`
```python
class WorkflowBuilder:
    def __init__(self, id: str, name: str, description: str = "")
    def var(self, key: str, value: Any) -> "WorkflowBuilder"
    def node(self, id: str, type: str, label: str = "", **kwargs: Any) -> "WorkflowBuilder"
    def edge(self, source: str, target: str, condition: str | None = None,
             label: str = "", kind: str = "data") -> "WorkflowBuilder"
    def build(self) -> Workflow
```

#### `agentweave/workflow/engine.py`（核心）
```python
@dataclass
class EngineResult:
    status: str                    # succeeded | failed | cancelled
    error: str = ""
    variables: dict[str, Any] = field(default_factory=dict)

class WorkflowEngine:
    def __init__(self, ctx: RuntimeContext) -> None
    async def execute(self, workflow: Workflow, run: RunState, inputs: dict[str, Any]) -> EngineResult
```
要求：
- 支持顺序、并行分支（同一节点多条出边 => 并发）、条件分支（`edge.condition`，无 condition 的边视为默认边；条件节点取第一个为真的出边）、JOIN 汇聚（等待所有入边）、SUBWORKFLOW 递归、循环节点（`node.loop.max_iterations`，循环体为 loop 节点到其 loop_back 边目标的子图，每轮发 `loop.iteration` 事件）、HUMAN_GATE、VERIFY。
- AGENT 节点：`await ctx.call_agent(agent_id, prompt, run, node, context)`；输出写入 `run.variables[node.id]` 及解析出的 output 变量。
- 节点失败按 `node.retry` 重试；验证失败且 `verify.allow_self_repair` 时进入自循环（由 `SelfLoopController` 驱动，最多 `node.loop.max_iterations` 轮），反馈文本注入下一次 prompt 的 `feedback` 变量。
- 每个节点状态变化必须发事件（`node.scheduled/started/succeeded/failed/retrying/skipped`），调度必须走 `ctx.scheduler`，并用事件 `plan.ready`/`node.scheduled` 暴露决策依据（score/reason/agent_load）。
- 可取消：检查 `ctx.cancel_event`，尽快退出并返回 cancelled。
- 无法并行时保持确定性的节点顺序（拓扑序）。

#### `agentweave/scheduler/engine.py`
```python
@dataclass
class ScheduleCandidate:
    node: WorkflowNode
    priority: float = 0.0            # 数值越大越优先
    agent_id: str = ""
    agent_load: float = 0.0          # 0~1
    capability: float = 0.0          # 0~1
    est_cost: float = 0.0
    data_ready: bool = True

@dataclass
class ScheduleDecision:
    node_id: str
    agent_id: str
    score: float
    reason: str

class Scheduler:
    POLICY_NAMES = ["fifo", "priority", "capability", "load_aware", "adaptive"]
    def __init__(self, policy: str = "capability") -> None
    def set_policy(self, policy: str) -> None
    def fork(self, policy: str) -> "Scheduler"   # 隔离策略、共享统计（per-run 调度用）
    def pick(self, candidates: list[ScheduleCandidate], now: float | None = None) -> ScheduleDecision | None
    def describe(self) -> dict[str, Any]     # 当前策略说明（前端展示）
```
`capability`/`load_aware`/`adaptive` 必须给出可解释打分（weighted sum），`adaptive` 根据运行态成功率动态调整权重。

策略名统一由模块函数 `normalize_policy(policy)` 归一化（去空格/小写/连字符转下划线/别名），非法值抛 `ValueError`；API 层在 `POST /api/runs` 用它做白名单校验，非法策略返回 400 `INVALID_POLICY`，run 记录中保存归一化后的策略名。每个 run 通过 `Scheduler.fork(policy)` 获得独立策略的调度器，统计（agent 成功率、决策数、权重）与平台主调度器共享，因此 adaptive 的学习结果跨运行累积，而并发运行之间互不串策略。

#### `agentweave/verify/verifier.py`
```python
@dataclass
class VerifyContext:
    node: WorkflowNode
    output: Any
    run: RunState
    spec: VerifySpec
    ctx: RuntimeContext

class VerificationPipeline:
    def __init__(self, ctx_provider: Callable[[], RuntimeContext] | None = None) -> None
    def register(self, name: str, checker: Callable[[VerifyContext], Awaitable[VerifyCheckResult]]) -> None
    def available(self) -> list[str]
    async def verify(self, spec: VerifySpec, node: WorkflowNode, output: Any, run: RunState) -> VerifyResult
```
内置检查器（名称即 `VerifySpec.checks` 取值）：
| 名称 | 说明 |
|---|---|
| `schema` | 输出必须是 JSON 对象且包含节点声明的 `outputs` 契约字段 |
| `non_empty` | 输出非空 / 文本长度 ≥ 20 |
| `numeric_ground_truth` | 用 `spec.ground_truth` 与输出中的 `metrics` 做相对误差比对（容差 `spec.tolerance`） |
| `code_rerun` | 独立重跑输出中的 `code` 字段，比对重算结果与申报指标 |
| `cross_agent` | 让另一个 Agent（`spec.config.verifier_agent` 或 role=verifier）独立复算并比对 |
| `statistical` | 样本量/拟合优度/显著性等统计阈值检查（`spec.config.thresholds`） |
| `llm_judge` | 结构化 LLM 评审（可离线降级为跳过并标注） |
| `artifact_exists` | 声明的产物文件存在且非空 |
置信度 = 加权通过率（考虑权重、检查器可信度），`passed = confidence >= spec.min_confidence 且无硬性失败`。

#### `agentweave/loop/selfloop.py`
```python
@dataclass
class LoopState:
    iteration: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

class SelfLoopController:
    def __init__(self, spec: LoopSpec) -> None
    def begin(self) -> None
    def should_continue(self, verify: VerifyResult, state: LoopState) -> bool
    def build_feedback(self, verify: VerifyResult, state: LoopState) -> str   # 中文、可执行、包含失败项与证据
    def record(self, state: LoopState, verify: VerifyResult, output: Any) -> None
    def summary(self, state: LoopState) -> dict[str, Any]                  # 前端展示 self_loop 指标
```

#### `agentweave/runtime/state.py`
```python
class RunState:
    run_id: str; workflow_id: str; status: str; variables: dict[str, Any]
    node_states: dict[str, NodeState]; started_at: float; finished_at: float
    self_loop: dict[str, Any]; verification: list[dict[str, Any]]
    def set_var(self, key: str, value: Any) -> None
    def snapshot(self, label: str = "") -> int          # 状态快照索引
    def rollback(self, index: int) -> None              # 回滚到快照
    def metrics(self) -> dict[str, Any]                 # 节点数/成功率/耗时/Token/循环轮数

class RunStore:
    def __init__(self, max_runs: int = 200) -> None
    def create(self, req: RunRequest, wf: Workflow) -> RunState
    def get(self, run_id: str) -> RunState | None
    def list(self, limit: int = 50) -> list[dict[str, Any]]
    def finish(self, run: RunState, status: str, error: str = "") -> None
    def to_summary(self, run: RunState) -> dict[str, Any]
    def cancel_flags(self, run_id: str) -> threading.Event
```
`RunState` 可被 orchestrator/engine 共享；`RunStore` 线程安全。

### 3.2 agents-tools

#### `agentweave/llm/base.py`
```python
@dataclass
class LLMResult:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"id","name","arguments"(dict)}]
    tokens_in: int = 0; tokens_out: int = 0
    model: str = ""; latency_s: float = 0.0; raw: dict | None = None

class LLMProvider(Protocol):
    name: str
    async def complete(self, messages: list[dict[str, str]], tools: list[dict] | None = None,
                       temperature: float = 0.2, max_tokens: int = 2048,
                       response_format: str | None = None) -> LLMResult
    async def list_models(self) -> list[str]
```

#### `agentweave/llm/openai_compat.py`
`class OpenAICompatProvider(base_url, api_key, model, timeout=60)`：使用 `urllib.request` 调 `/chat/completions`，支持 tools、JSON mode、超时与错误包装；不打印 Key。

#### `agentweave/llm/mock.py`
`class MockProvider`：**确定性**、零网络。按 system prompt 角色（planner/data/statistician/verifier/critic/writer）与用户提示关键词返回结构化结果，用于离线演示与单元测试。输出必须与 `agentweave/demo/scenario.py` 中定义的输出 schema 一致（见 §5）。

#### `agentweave/llm/router.py`
```python
class ModelRouter:
    def __init__(self, config: dict[str, Any] | None = None) -> None
    def configure(self, cfg: dict[str, Any]) -> dict[str, Any]   # 运行时切换；不返回 Key 明文
    def status(self) -> dict[str, Any]                            # provider/model/健康状态/用量
    async def complete(self, messages, tools=None, role: str = "", **kw) -> LLMResult
    def usage(self) -> dict[str, Any]                             # tokens、调用次数、平均延迟
```
要求：失败重试（指数退避，最多 2 次）、provider 回退到 mock、每次调用发 `llm.called/llm.result` 事件（payload: model/tokens/latency，不含 prompt 全文）。

#### `agentweave/tools/registry.py`
```python
@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: str = ""
    duration_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

class ToolRegistry:
    def __init__(self, workspace_dir: str) -> None
    def register(self, spec: ToolSpec, fn: Callable[..., Awaitable[Any]]) -> None
    def unregister(self, name: str) -> None
    def get(self, name: str) -> ToolSpec | None
    def specs(self) -> list[ToolSpec]
    async def call(self, name: str, args: dict[str, Any], ctx: Any = None) -> ToolResult
    def openai_tools(self, names: list[str] | None = None) -> list[dict]   # Function Calling schema
```
要求：参数 JSON Schema 校验（宽松但报错清晰）、超时、异常捕获、`risk_level=high` 的工具在 `require_approval` 模式下拒绝并提示、发出 `tool.called/tool.result` 事件。

#### `agentweave/tools/builtin.py`
注册以下工具（名称固定）：`python_exec`、`calculator`、`dataset_stats`、`chart_svg`、`file_read`、`file_write`、`http_get`、`json_query`、`report_render`。
- `python_exec(code, timeout_s=20)`：子进程执行（工作目录=运行产物目录），捕获 stdout/stderr，返回 `{stdout, stderr, exit_code, value_json?}`；禁止网络访问（尽力限制，文档注明）；
- `dataset_stats(path, columns)`：标准库读取 CSV，返回 count/mean/std/min/max/相关系数；
- `chart_svg`：用标准库生成 SVG 折线/柱状图，落盘到产物目录；
- `http_get`：仅允许 http(s)，超时 10s，返回截断文本；
- `file_read/file_write`：路径必须限定在仓库 `workspace_dir` 或本次运行 `artifacts_dir` 内，防目录穿越。

#### `agentweave/tools/mcp_client.py`
```python
class MCPClient:
    def __init__(self, name: str, transport: str = "stdio", command: list[str] | None = None,
                 url: str | None = None, env: dict[str,str] | None = None) -> None
    async def connect(self) -> None
    async def list_tools(self) -> list[ToolSpec]
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any
    async def close(self) -> None
```
JSON-RPC 2.0 over stdio（换行分隔）与 HTTP；实现 `initialize`、`tools/list`、`tools/call`。`MCPManager` 读取 `config/mcp_servers.json`，把 MCP 工具注册进 `ToolRegistry`（`source="mcp"`）。

#### `agentweave/agents/registry.py`
```python
class AgentRegistry:
    def __init__(self, load_fn: Callable[[], list[AgentSpec]] | None = None) -> None
    def register(self, spec: AgentSpec) -> None           # 同 id 新版本覆盖并保留版本历史
    def get(self, agent_id: str) -> AgentSpec | None
    def list(self) -> list[AgentSpec]
    def history(self, agent_id: str) -> list[str]
    def match(self, skills: list[str] | None = None, tools: list[str] | None = None) -> list[tuple[AgentSpec, float]]
    def acquire(self, agent_id: str, max_concurrency: int = 2) -> bool
    def release(self, agent_id: str) -> None
    def load_report(self) -> list[dict[str, Any]]          # [{agent_id, inflight, max, utilization}]
    def save(self, path: str) -> None                      # team.json
```

#### `agentweave/agents/agent.py`
```python
class AgentRuntime:
    def __init__(self, spec: AgentSpec, llm: Any, tools: Any, bus: EventBus,
                 memory: AgentMemory | None = None) -> None
    async def run(self, prompt: str, run_id: str, node_id: str, *,
                  context: dict[str, Any] | None = None, feedback: str = "",
                  max_iterations: int | None = None) -> AgentResult
```
要求：ReAct 循环（LLM → 工具调用 → 观测 → 结论），最多 `spec.max_iterations` 轮；每步发 `agent.thought` / `tool.called` / `tool.result` / `agent.decision` 事件；输出解析支持 ```json``` 代码块；工具错误回注给模型；记录 tokens/latency/confidence（confidence 由模型自评或解析字段，缺省按是否成功给出）；`feedback` 非空时拼入 user prompt。

#### `agentweave/agents/memory.py`
```python
class AgentMemory:
    def __init__(self, run_id: str) -> None
    def note(self, content: str, tags: list[str] | None = None) -> None
    def recall(self, query: str, k: int = 3) -> list[str]      # 词法相似度即可
    def artifacts(self) -> dict[str, str]
    def store_artifact(self, name: str, content: Any) -> str
```

### 3.3 web-ui

#### `agentweave/api/server.py`
```python
def create_app(platform) -> ThreadingHTTPServer
def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None
```
`server.py`（仓库根）：
```
python server.py [--host 127.0.0.1] [--port 8765] [--open]
```
要求：`ThreadingHTTPServer` + 后台 asyncio 事件循环线程；REST 处理经 `asyncio.run_coroutine_threadsafe`；SSE 长连接不阻塞其他请求；静态文件带正确 MIME；`/api/health` 立即可用。

#### REST API（全部 JSON；错误统一 `{"error": {"code": "...", "message": "..."}}`，HTTP 4xx/5xx）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{status:"ok", version, uptime_s, llm:{...}, agents:n}` |
| GET | `/api/overview` | 首页统计：runs/agents/tools/events/成功率/平均耗时/最近事件 |
| GET | `/api/agents` | `{agents:[AgentSpec...], load:[{agent_id,inflight,utilization}]}` |
| POST | `/api/agents` | 新建/更新 Agent（body=AgentSpec JSON），返回 `{agent}` |
| GET | `/api/workflows` | `{workflows:[{id,name,version,description,node_count,tags}]}` |
| GET | `/api/workflows/{id}` | 完整 Workflow JSON |
| POST | `/api/workflows` | 保存工作流（body=Workflow JSON） |
| GET | `/api/tools` | `{tools:[ToolSpec...], mcp_servers:[...]}` |
| POST | `/api/mcp/servers` | 添加 MCP server 配置并尝试连接 |
| POST | `/api/runs` | body=`RunRequest`，返回 `{run_id, status}`，**异步启动** |
| GET | `/api/runs` | 最近运行摘要列表 |
| GET | `/api/runs/{id}` | `RunSummary` 全量（含节点状态、验证、自循环指标） |
| GET | `/api/runs/{id}/events` | SSE：回放历史 + 实时推送 |
| POST | `/api/runs/{id}/cancel` | 取消 |
| GET | `/api/runs/{id}/artifacts` | 产物列表 |
| GET | `/api/runs/{id}/artifacts/{name}` | 下载产物（图片/报告） |
| GET | `/api/verify/strategies` | 可用验证检查器说明 |
| GET | `/api/config/llm` | 当前模型配置（Key 打码） |
| POST | `/api/config/llm` | 切换 provider/base_url/model/api_key，可选 `test:true` 返回连通性 |
| POST | `/api/demo/run` | 启动内置演示场景（body 可选 `{policy, scenario}`），等同 POST /api/runs |
| GET | `/api/stats/timeseries` | 运行耗时/成功率/Token 时序（前端图表） |

#### SSE 格式
```
id: <seq>
event: <type>
data: {"seq":..,"ts":..,"run_id":"..","type":"..","node_id":..,"agent_id":..,"payload":{...},"level":"info"}

```
每 15s 发送 `: heartbeat`。支持 `Last-Event-ID` 从 seq+1 回放。

#### 前端 `web/index.html` + `web/app.js` + `web/styles.css`（无构建、无 CDN）
单页应用，左侧导航 + 内容区，深色科技风，中文字体栈：
```
PingFang SC, Microsoft YaHei, Noto Sans SC, system-ui
```
页面：
1. **概览**：数据卡（运行数/成功率/Agent 数/工具数）、最近运行表、Agent 负载条、事件流缩略、架构示意。
2. **编排台**：工作流选择 + SVG DAG 画布（节点按 position 绘制、可点击查看/编辑节点属性、可缩放平移）+ 运行面板（输入 JSON、调度策略、启动按钮）+ 节点状态实时染色。
3. **运行监控**：运行列表 + 选中运行的实时事件流（带类型图标/颜色）、节点状态矩阵、Agent 时间线、Token/耗时统计、自循环轮次指示、取消按钮。
4. **验证中心**：验证结果表（检查项/通过/证据/置信度）、置信度仪表盘、基准对比（多智能体 vs 单智能体，若数据存在）、失败反馈文本。
5. **Agent 团队**：Agent 卡片（角色/技能/工具/版本/负载）、新建/编辑表单、版本历史。
6. **工具与 MCP**：内置工具列表、MCP server 管理、工具调用示例。
7. **模型设置**：provider 选择（OpenAI 兼容/离线 Mock）、base_url、model、api_key（password，不显示明文）、测试连接、用量统计。
视觉要求：1440×900 与 1920×1080 截图可直接用于 PPT；不使用外部 CDN（离线可演示）；图表用 SVG/CSS 手绘。

## 4. 事件 payload 约定（前端展示依赖）

| 事件 | payload 必备字段 |
|---|---|
| `run.started` | `workflow_id, workflow_name, policy` |
| `plan.ready` | `policy, candidates:[{node_id,priority,agent_id,score,reason}]` |
| `node.scheduled` | `node_id, agent_id, score, reason, agent_load` |
| `node.started` | `node_id, agent_id, attempt` |
| `agent.thought` | `text, step` |
| `agent.decision` | `text, confidence` |
| `tool.called` | `name, args` |
| `tool.result` | `name, ok, duration_s, preview` |
| `node.succeeded` | `node_id, output_preview, confidence, tokens_in, tokens_out, latency_s` |
| `node.failed` | `node_id, error, attempt` |
| `verify.result` | `node_id, passed, confidence, checks:[{name,passed,detail}]` |
| `loop.iteration` | `node_id, iteration, max_iterations, verify_passed, feedback_preview` |
| `run.finished` | `status, duration_s, nodes, tokens, success_rate, self_loop_iterations` |
| `artifact.created` | `name, kind, path` |
| `run.cancel_requested` | `reason` |
| `run.error` | `error, node_id`（失败运行必发，便于前端展示终止原因） |

## 5. 演示场景（Lead 实现，MockProvider 需对齐）

场景 id：`ai4s-air-quality`，名称：**AI4S 空气质量与气象因子归因分析**

工作流 `wf-ai4s-air-quality` 节点：
| node id | 类型 | 角色/工具 | 输出契约 |
|---|---|---|---|
| `start` | start | - | - |
| `plan` | agent | `planner`（任务规划） | `{tasks:[{id,goal,owner}], success_criteria:[...]}` |
| `scan` | tool | `dataset_stats` | `{shape, columns, missing, preview}` |
| `research` | agent | `researcher`（文献/方法调研） | `{methods:[...], rationale}` |
| `clean` | agent | `data_engineer` | `{data_path, rows, cleaning_log:[...]}` |
| `analyze` | agent | `statistician`（调用 `python_exec`） | `{code, metrics:{coefficients:{...}, r2, n, p_values:{...}}, conclusion}` |
| `verify` | verify | 检查器 `code_rerun + numeric_ground_truth + statistical + cross_agent` | VerifyResult |
| `critique` | agent | `critic` | `{confidence, risks:[...], improvements:[...]}` |
| `report` | agent | `writer` | `{markdown, title}` |
| `gate` | human_gate | - | 审批（演示自动通过） |
| `end` | end | - | - |

- 并行：`scan || research`（`plan` 之后），再 `clean`，再 `analyze`。
- 自循环：`analyze → verify` 组成 `loop` 节点 `repair`，验证失败时把反馈注入 `analyze` 的 prompt，最多 3 轮；事件必须出现 `loop.iteration`，每轮记录误差指标。
- 数据集：`examples/datasets/air_quality.csv`（2000 行，列 `date,city,pm25,temp,humidity,wind,so2,no2`），由固定随机种子生成，真实关系：`pm25 = 78 - 0.85*temp + 0.42*humidity - 1.6*wind + 0.28*so2 + ε`，真值存 `examples/datasets/air_quality_truth.json`。
- 验证：`numeric_ground_truth` 比对系数相对误差 ≤15%；`code_rerun` 独立重跑；`statistical` 要求 n≥1000、R²≥0.55；`cross_agent` 由 verifier Agent 独立复算。
- 基准对比：`scripts/benchmark.py` 跑单 Agent 基线（baseline 节点/工作流）与多 Agent 编排，输出 `docs/benchmark.json`（成功率、平均轮次、系数误差、耗时、Token）。

MockProvider 依据角色与提示中的关键词直接产出上述 JSON（用固定系数近似真值，首轮带一点偏差以触发一次修复循环，修复后误差达标），从而**离线也能演示“验证失败 → 反馈 → 修复 → 通过”的完整闭环**。

## 6. 编码与验收

- 类型注解齐全；模块 docstring 说明职责；复杂逻辑中文注释。
- 禁止 `eval/exec`（`python_exec` 工具除外，且仅在子进程沙箱）。
- 日志用 `logging`，默认 INFO；不打印 API Key；不把大对象塞进事件。
- 所有模块自测：
  - `py -3.14 -m unittest discover -s tests -v` 全绿（测试由各模块作者补充到 `tests/test_<module>.py`）
  - `py -3.14 -c "import agentweave"` 无副作用
  - `py -3.14 server.py --port 8765` 启动，`curl /api/health` 返回 200
- 性能目标：Demo 工作流离线运行 ≤ 8s；SSE 首事件延迟 < 500ms；200 次运行内存不失控（RingBuffer 上限）。
- 提交物命名：`<队伍名称>+<项目名称>`；代码仓库需含 README、LICENSE、启动说明、演示脚本。

## 7. 实现备注（core-engine 追加，实现与契约的差异/补充说明）

- **调度器实现路径**：`agentweave/scheduler/engine.py`（契约只写 `scheduler` 包，未指定文件名）。
- **`ScheduleDecision` 附加字段**：在契约字段之外新增 `agent_load: float = 0.0` 与 `candidate_list: list[dict] = []`。前者便于 `node.scheduled` 事件直接带出负载，后者保留整轮候选打分（含每项 score/reason），供前端「调度决策解释」展示；两者均有默认值，按契约两参数构造仍然可用。
- **`Scheduler.POLICY_NAMES` 为超集**：`["fifo","priority","round_robin","capability","load_aware","adaptive"]`（契约 5 策略 + `round_robin`）；`set_policy` 与 `normalize_policy` 额外接受别名 `roundrobin`/`rr`/`load` 及连字符写法（如 `load-aware`），非法策略抛 `ValueError`。
- **兼容入口**：`agentweave/verify/pipeline.py` 再导出 `verify/verifier.py` 的 `VerificationPipeline`/`VerifyContext`；`agentweave/loop/controller.py` 再导出 `loop/selfloop.py` 的 `SelfLoopController`/`LoopState`。实际实现位于 `verifier.py` 与 `selfloop.py`，两处入口只做转发，避免两套实现漂移。
- **数值比对绝对下限 `ABS_FLOOR = 1e-6`**：`verify/checkers.py` 的 `compare_numeric()`（供 `numeric_ground_truth` / `code_rerun` / `cross_agent` 共用）判定为 `diff <= max(tolerance*|expected|, 1e-6)` 即一致，且两侧都 < 1e-6 时直接视为一致。原因：p 值等「本就接近 0」的指标（如 1.1e-9 vs 0.0）用纯相对误差会得到 110%~2400% 的虚假偏差；正常量级（系数 ~1、R² ~0.7、n=2000）仍严格按容差判定。三个检查器的 `evidence` 均带 `abs_floor: 1e-6` 便于审计。
- **`RunState.node_states[id].output` 与 `run.variables[node.id]` 同值**：保存 `AgentResult.output` 原样（dict）；引擎另将 dict 输出按 `node.outputs` 声明的字段展开到 `run.variables`，供下游 `{{var}}` 模板与条件表达式引用。
- **`WorkflowStore` 默认目录**：`workflow_dirs` 省略时使用仓库根 `workflows/`；`load()` 扫描各目录 `*.json`（每个文件一个 Workflow JSON），坏文件不抛异常而是记入 `errors()`。
- **`.env` 加载与配置优先级**：`load_platform_config()` 先调用 `load_runtime_env()` 读取仓库根 `.env`（默认 `override=False`，已存在的真实环境变量优先；文件缺失/解析失败静默跳过），再用 `apply_llm_env_overrides()` 把 `AGENTWEAVE_PROVIDER` / `AGENTWEAVE_BASE_URL` / `AGENTWEAVE_MODEL` 覆盖到 `llm` 段，最后与内置默认配置深合并。Key 只经环境变量（默认 `AGENTWEAVE_API_KEY`）读取，不写入 `config/platform.json`。
- **并发准入原子性**：`start_run()` 在 `self._admission_lock` 内完成「统计 pending/running/paused 数量 → 创建 run」，避免多线程突发提交时各自读到未超限计数而穿透 `runtime.max_concurrent_runs`（HTTP 层由该 RuntimeError 映射 429 `TOO_MANY_RUNS`）。
- **工作流解析为严格模式**：`_resolve_workflow()` 在 `workflow_id` 非空时只匹配 `workflow_id` 与 `wf-<id>`，不会回退到 `scenario`（避免拼错 id 时静默跑成别的场景）；仅在 `workflow_id` 为空时才按 `scenario` 解析，两者都空返回 `None`，由 `start_run` 抛 `ValueError` → API 映射 404 `WORKFLOW_NOT_FOUND`。
- **验证记录结构**：`run.verification` 为按时间追加的 list[dict]，每项 `{node_id, ts, passed, confidence, layer, checks, feedback, metrics}`，前端验证中心直接读该结构即可；`run.self_loop[node_id]` 存 `SelfLoopController.summary()`（含 iterations/history）。
