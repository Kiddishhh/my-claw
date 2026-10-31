# my-claw

## 默认运行：Daytona 远程沙箱

`build_agent(backend=..., workdir=...)` 只接收支持 execute 的沙箱后端，不回退本机。
运行入口按 user-id/thread-id 获取、准备并锁定沙箱；详细配置、生命周期和密钥边界见 [Daytona 使用说明](docs/daytona.md)。

```powershell
uv sync --locked --all-extras
uv run --extra daytona python -m scripts.devmate_with_mw --thread-id issue-001 --task "检查项目并总结，不修改文件"
uv run --extra daytona python -m scripts.sandbox_cleanup --watch
```

本地验收脚本显式使用 `build_local_agent()`，不会创建云端资源。
默认远程模式注册搜索/网页和 task 委派，主子代理工作文件与命令由 Daytona 提供，规则和技能通过 /guidance/ 读取服务器只读版本。
开发任务按 AGENTS.md 指导推进 coder → tester → reviewer；多个 task 由中间件排队执行，角色顺序仍由主 Agent 决策。
远程组件与角色工具边界见 [Daytona 使用说明](docs/daytona.md)。


默认不上传平台仓库；统一调用入口为 `agent.dispatcher.run_task`。任务结束保存工作文件，沙箱删除后可从磁盘恢复。详见 [工作区持久保存](docs/workspace-persistence.md)。对话持久化和前端接口本阶段未实现。

### Web 团队端到端验收

```powershell
uv run --extra daytona python -m scripts.verify_web_team
```

直接运行会调用真实模型并创建临时 Daytona。固定需求为零第三方依赖的商品展示页，
检查 coder → tester → reviewer、规则和技能、真实 Node 测试/构建、磁盘源码哈希（不保存构建输出），
随后删除沙箱，同线程从磁盘恢复，再次测试与构建。最后只清理本次临时云端资源。
它通过正式 dispatcher 执行，不上传平台源码，不在宿主运行生成的项目。

默认主代理最多 6 次模型调用，每次子代理最多 10 次，Agent 阶段超时 480 秒；
参数为 `--max-model-calls`、`--max-subagent-calls`、`--timeout`，准备和传输不计入该超时。
默认主代理 30 次、每次子代理委派 40 次模型调用，集中定义于 `infra/agent_limits.py`；
dispatcher 图步数上限 1000（包括中间件节点，不等于模型调用次数）。超限仍报错并尽力保存源码。
普通 dispatcher 任务超时默认 600 秒，可通过 `SYC_SANDBOX_TASK_TIMEOUT` 配置；
Web 验收脚本用 `--timeout` 覆盖该值，默认仍为 480 秒。
每次仅尝试一次，避免无意重复消费。日志输出调用摘要和脱敏异常，不展开完整模型内容。
记录位于 `logs/web-team-verification/<线程>/`：`events.jsonl`、`report.json`，
该目录内 `.sandbox-state/workspaces/` 保留磁盘项目。清理失败也会写报告并返回非零退出码。
不属于浏览器视觉/交互验收，也不验证聊天持久化。`--help` 不调用模型或创建沙箱。

## 当前目录与入口

- `agent/`、`middleware/`：Agent 构建和中间件。
- `sandbox/`：Daytona 会话复用、项目初始化和生命周期管理。
- `tools/`、`subagents/`：工具及保留的可选子代理能力。
- `infra/`：配置、日志和数据库初始化。
- `scripts/`：默认运行入口、沙箱回收和验收公共辅助函数。
- `tests/`：自动回归与手动验收；`verify_env.py` 检查模型配置和数据库连接。
- `.agents/skills/`、`docs/`：开发技能和使用说明。
- `data/`、`outputs/`：分析输入和产物。

旧 TODO 普通/流式演示已删除。日常使用 `scripts.devmate_with_mw`；
远程真实验收使用 `tests.verify_daytona`；本地文件通路和开发工具验收分别使用
`tests.verify_agent`、`tests.verify_development_tools`。后三者不会被 pytest 自动运行。
`.venv/` 是唯一项目虚拟环境；`.sandbox-state/` 保存沙箱映射和项目文件备份，`logs/` 保存运行与验收记录。

## 保留的本地验证模式

显式本地验证入口直接使用 `web_search`、`fetch_url`、`run_pytest` 和内置文件工具，加载三个开发技能。
暂不挂载表格工具、数据分析技能、子代理和 `task`。子代理代码保留，显式传入
`build_local_agent(enable_subagents=True)` 才启用；下文数据分析/子代理章节描述的是保留能力。

验证脚本（项目根目录，使用 `.venv/Scripts/python.exe`）：

```powershell
python -m tests.verify_development_tools           # 默认真实模型和联网服务
python -m tests.verify_development_tools --offline # 模拟模型/联网，真实执行隔离 pytest
```

PyCharm 可直接运行 `tests/verify_development_tools.py`。每次使用新的隔离目录，
验证三个工具调用和开发技能发现；最多 8 次模型调用、180 秒总超时。
终端只打印调用摘要和脱敏报错，报告在 `logs/development-verification/`。
此脚本遵循 LangSmith 环境配置上传追踪；联网失败会明确报告，不作为通过。

## 本地 Excel / CSV 数据分析

显式启用子代理时，data_analyst 注册 `inspect_table`、`analyze_table`、`plot_analysis`、`export_report`，
并自动发现 `local-data-analysis` 与 `analysis-reporting` 两个技能。
将文件放在当前工作目录 `data/inputs/`；产物保留在 `outputs/analysis/<ID>/`。
使用 `build_local_agent(workspace_root=...)` 时，输入、计算和输出都跟随该工作目录。
除虚构的 `example_sales.csv` 外，输入文件及结果已加入 Git 忽略规则。

可以向 Agent 提问：

> 分析 data/inputs/example_sales.csv，只统计 status=paid，按 product 汇总 amount，
> 金额单位为元，生成产品销售额柱状图和报告。保留原始数据。

能力与限制：

- 逗号分隔 CSV、XLSX；CSV 支持 UTF-8 / GB18030；多工作表需要明确 sheet_name。
- 缺失值、重复行、字段与样本检查；原始字段按文本保留，编号不会自动丢失前导零。
- 条件筛选、显式去重、Decimal 求和/均值/最小值/最大值、行数统计、日期分组、排序和 Top N。
- 多表仅支持同名键的多对一 left/inner 关联；右表键重复或非键字段重名会报错。
- 三种 PNG 图表：bar、line、scatter；导出 CSV、XLSX、Markdown，附带来源哈希和计算参数。
- 输入上限10 MiB、100000行、100列；图表上限1000行。暂不支持旧 XLS、公式计算、任意 Python/SQL 和统计建模。
- Excel 公式请先另存为值；不自动补零、剔除异常、丢弃缺失值或去重。无效数字/日期返回工具错误。
- Decimal 数值在结果表中按文本保存以保证精度，图表才转换为浮点显示。报表不自动做金额舍入。
- Excel 超长单元格或不支持的控制字符会明确报错，避免静默截断；CSV 对公式式文本做单引号转义。
- 工具输出少量样本/结果预览，完整表格留在本地。已启用的 LangSmith 可能记录工具消息，分析前确认数据适合该追踪设置。

不调用模型也可验证工具链：

```python
from pathlib import Path
from tools.data_tools import create_data_tools

tools = {t.name: t for t in create_data_tools(Path.cwd())}
data = tools["inspect_table"].invoke({"file_path": "example_sales.csv"})
result = tools["analyze_table"].invoke({
    "dataset_id": data["dataset_id"], "metric": "amount", "group_by": ["product"],
    "filters": [{"column": "status", "operator": "eq", "value": "paid"}],
})
tools["plot_analysis"].invoke({"result_id": result["result_id"], "x": "product"})
print(tools["export_report"].invoke({"result_id": result["result_id"]}))
```

针对功能变更运行一次 `uv run --all-extras python -m pytest tests/test_data_tools.py -q`。
该测试使用临时样例和离线模型，验证计算结果、路径边界、Excel/CSV、图表导出和 Agent 工具注册。

## 开发环境

在项目根目录安装依赖及项目自身：

```powershell
uv sync --locked --all-extras
```

uv 会将项目以可编辑方式安装到 `.venv`。修改源码会立即生效，无需重复安装。
在现有目录下新增模块也无需向每个脚本添加 `sys.path`。
`.env` 固定从项目根目录读取，不依赖终端当前目录。

## 启动

```powershell
uv run --all-extras python -m scripts.devmate_with_mw --thread-id issue-001 --task "检查项目并总结"
uv run --all-extras python tests/verify_env.py
```

也可直接使用 `.venv` 中的 Python 按文件路径运行：

```powershell
.\.venv\Scripts\python.exe scripts/devmate_with_mw.py --thread-id issue-001 --task "检查项目并总结"
```

Cursor 中选择 `.venv\Scripts\python.exe` 为解释器，调试时选择
`Python: 当前文件（项目环境）`。系统 `F:\Python` 是独立环境，不包含本项目的安装。

不使用 uv 时，激活同一个虚拟环境后运行 `python -m pip install -e .`，
也可注册项目模块。uv 创建的环境默认可能没有 pip，优先使用 `uv sync`。

## 验证与数据库

```powershell
uv run --all-extras python -m unittest discover -s tests -v
```

数据库启动方式和本地数据目录见 [infra/README.md](infra/README.md)。

## 关键更新后的单次验收

PyCharm 中直接运行 `tests/verify_agent.py`：只执行一个真实模型测试，
使用固定模拟用户输入和 `.env` 的模型配置，验证技能读取、文件写入与回读。
会产生 API 费用；默认最多 6 次模型调用、120 秒、累计输入 Token 软上限 50000。
Token 在响应返回后检查，可能超出上限。

```powershell
.\.venv\Scripts\python.exe tests/verify_agent.py
```

终端以中间件风格打印每轮模型摘要、Token 用量、工具状态和最终回复摘要；
不展开完整提示词和历史消息，工具参数与结果最多预览 160 字。报错保留脱敏异常堆栈。
事件、报告和实际生成文件保留在
`logs/agent-verification/<run-id>/` 下，不再随测试结束删除。
本验收使用简短通路任务，不再重复执行两个折扣代码生成示例。

日常离线回归（不调用模型）使用：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

其余测试覆盖独立的工具、配置和失败边界；关键更新时执行相应回归即可，
无需为了同一验收重复运行普通与流式两套真实模型任务。
真实模型验收只通过直接运行该脚本触发，pytest / unittest discovery 不会重复执行。

## Agent 的 Skills 与 Memory

`build_local_agent()` 使用项目根目录作为 `FilesystemBackend` 根目录，并启用虚拟路径：

- `skills=["/.agents/skills/"]`：发现项目技能的名称和描述，需要时通过文件工具读取完整 `SKILL.md`。
- `memory=["/AGENTS.md"]`：将项目规则加载到系统提示词。
- `MemorySaver()`：在当前 Agent 实例的进程内保存会话状态，不等于数据库持久化。

带 checkpointer 的调用需要传 `thread_id`：

```python
agent = build_local_agent(max_model_calls=5)
config = {"configurable": {"thread_id": "local-session-1"}}
result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "检查项目代码"}]},
    config=config,
)
```

同一 Agent 实例复用 `thread_id` 会延续历史；不同 ID 隔离会话。
现有示例脚本每次生成新 ID。新建 Agent 或重启进程会丢失内存 checkpoint，磁盘上的项目文件仍保留。
更新已有会话中的技能后，可在下一次输入设置 `skills_metadata=None` 使技能列表重新加载。

`max_model_calls=None` 表示不额外限制，正整数表示一次运行最多调用模型多少轮。
达到上限后如需继续调用模型，框架以提示消息结束；这不是 Token 或金额预算，工具调用也不单独计为模型调用。
不根据 `openai` 兼容协议自动强制开启 `parallel_tool_calls`，因为参数支持还取决于实际服务和模型。

文件工具能直接修改项目真实文件，`virtual_mode=True` 不是操作系统沙箱。
该构建方式用于受信任的本地开发；对外服务需要另行配置文件访问权限、用户隔离和执行沙箱。

## 脚本公共工具

`scripts/_common.py` 为本地验收提供预算控制、调用证据和摘要输出：

| 工具 | 用途 |
| --- | --- |
| `run_with_budget` | values 流运行，观察到累计输入 Token 超预算后关闭流，每次创建新线程 ID |
| `tool_calls` / `called` | 提取或查询模型发起的工具调用 |
| `written_paths` / `first_turn` | 提取请求写入路径、检查调用出现的轮次 |
| `model_call_count` / `final_reply` | 按 AIMessage 估算轮次、获取末条消息 |
| `Check` / `report` | 区分硬性检查和参考项，汇总结果 |
| `print_run` | 展示轨迹和用量，遮盖常见凭据模式 |
| `run_attempts` | 有限重试，首次通过即停，异常直接向外传播 |
| `FLOAT_MONEY` | 快速筛查常见 float 写法，不能替代金额逻辑审查 |

例如在异步函数中：

```python
from scripts._common import Check, print_run, report, run_with_budget

agent = build_local_agent(max_model_calls=5)
state, budget_hit = await run_with_budget(
    agent, "只读取项目规范并概述要求", max_input_tokens=10000,
)
print_run(state, budget_hit, 10000)
passed = report([Check("未触发预算停止", not budget_hit)])
```

预算依赖 `CostMeterMiddleware`，只在收到用量更新后检查，不能提前阻止单次请求超额，
也不会回滚已经写入的文件。用量缺失时不能确认实际消费；按 AIMessage 计数也包含框架生成的提示。
`called`、`written_paths` 是请求证据，不保证执行成功；核验写入结果要结合 ToolMessage 和实际文件内容。
同轮调用不能证明“读完规范之后再写文件”。`run_attempts` 不自动清理产物，
回调负责隔离和清理，跨重试的总预算需要调用方另行控制。

## Skills / Memory / 文件系统端到端验证

```powershell
# 显式离线：假模型 + 真实 Agent 图、中间件、checkpoint 和文件工具，不消耗 API Token。
uv run --all-extras python tests/verify_agent.py --offline

# 真实模型：使用 .env 的模型与密钥，会产生费用。
uv run --all-extras python tests/verify_agent.py --live --max-model-calls 6 --max-input-tokens 50000 --timeout 120
```

测试入口位于 `tests/verify_agent.py`（沿用项目现有的 `tests/` 目录）。
复用 `scripts/_common.py` 的 `run_with_budget` 控制预算，
用 `called`、`written_paths`、`first_turn` 提取调用证据，`print_run` 和 `report` 在控制台打印轨迹与检查结果。
也可运行 `uv run --all-extras python -m tests.verify_agent --offline`；直接运行脚本会实时显示过程输出。

脚本将项目真实 `AGENTS.md` 和 `my-claw-code-quality/SKILL.md` 复制到独立验证目录，
附加不同的随机标记。Memory 标记不出现在用户任务中；Skill 标记只有读取正文后才能取得。
Agent 需读取技能、将两种标记写入 `/verification.json`，再用文件工具回读。
程序结合实际系统提示词、checkpoint 元数据、成功工具结果和宿主机磁盘内容进行断言。
不修改原始规范和业务文件，也不会复制 `.env`、数据库或密钥到验证工作目录。

终端逐步打印模型开始/结束、每轮 Token、工具名称/参数摘要/结果摘要/状态/耗时、
累计用量以及检查结果。框架模型调用轮次由回调计数，和包含框架提示的 AIMessage 数分开显示；
不包含模型 SDK 内部的 HTTP 重试。输入预算在调用完成后检查，不能保证不超额。

每次运行保留在 `logs/agent-verification/<run-id>/`（已被 Git 忽略）：

- `events.jsonl`：带时间戳、轮次和调用 ID 的过程记录，常见凭据脱敏。
- `report.json`：检查结果、调用次数、已观测 Token、停止原因和熔断标记。
- `workspace/verification.json`：通过真实 FilesystemBackend 写入的验证产物；失败时可能不存在。

`--log-dir` 可以指定记录的父目录。停止原因区分 `completed`、`input_budget`、
`model_call_limit`、`timeout` 和 `error`。缺少用量信息时预算不能被可靠验证，验收不会算通过。
退出码 0 表示所有硬性检查通过，1 表示未通过；默认不自动重试。真实验收遵循 `LANGSMITH_TRACING` 开关；离线或注入模型同样遵循该配置。
启动时自动加载 `.env` 的 LangSmith 配置，已有进程环境变量优先。启用后提示词、模型回复及工具调用上传到配置的 LangSmith 项目。
离线通过只证明程序接线和文件通路，不能证明真实模型鉴权、工具选择或规范遵循；这些由 `--live` 验证。
## 本地验收模式的 DeepAgents 子代理

`agent.main.build_local_agent(enable_subagents=True)` 可注册六个专职角色，主代理通过 `task` 按需委派：

| 角色 | 负责内容 | 自定义工具 |
| --- | --- | --- |
| planner | 拆解计划与验收标准，只读 | 无 |
| researcher | 仓库与外部技术资料调研，只读 | web_search、fetch_url |
| coder | 实现代码改动 | 无（使用内置文件工具） |
| tester | 补充必要测试、执行当前工作区 pytest | run_pytest |
| reviewer | 只读审查，返回 ReviewResult JSON | 无 |
| data_analyst | 本地 CSV/XLSX 分析、绘图和报告导出 | inspect_table、analyze_table、plot_analysis、export_report |

框架的 `general-purpose` 被显式覆盖为只读背景整理助手。仅 coder/tester 提供内置写文件工具；
data_analyst 通过表格工具写入限定的分析产物目录。所有角色加载工作区 `/AGENTS.md`，
两个分析技能位于 `subagents/skills/data-analysis/`，只由 data_analyst 自动发现；开发技能仍在 `.agents/skills/`。

示例请求：“把 data/inputs/example_sales.csv 交给 data_analyst，先检查质量，再按商品汇总金额并导出报告。”
主代理负责传递目标、路径和指标口径，子代理返回摘要与产物路径。终端显示角色、工具和 Token 摘要。

默认所有角色使用 `.env` 中配置的主模型，无需增加密钥或服务。可通过
`build_local_agent(subagent_models={"reviewer": another_model})` 为角色传入独立模型对象。
`max_model_calls` 仅限制主代理；`max_subagent_model_calls=40` 限制每次委派，超限报错，不是全局调用预算。
本地后端不提供 execute/shell；pytest 使用当前虚拟环境和当前 workspace_root，超时 120 秒。
这些工具限制不构成操作系统沙箱，pytest 只能执行受信任项目的代码。

## DeepSeek 角色思考策略

`AGENTS.md` 是通用开发运行规则，不再包含平台维护或订单服务专属要求。
Memory 已注入的规则不重复读取；Skills 仍按任务渐进式加载。

策略集中在 `infra/model_policy.py` 的 `ROLE_REASONING`：主代理、researcher、tester、reviewer、general-purpose 关闭思考；planner、coder、data_analyst 启用 low 思考。
官方 DeepSeek 端点自动使用 `langchain-deepseek` 适配器，保留已有 `SYC_MODEL_PROVIDER=openai` 配置；无需改密钥或 URL。
兼容层补齐当前适配器的工具调用 reasoning_content 回传，终端不打印推理原文。
其他供应商和离线模型不套用此策略。显式传入官方 DeepSeek 模型时，思考角色应使用 `DeepSeekChatModel`，避免 ChatOpenAI 丢失推理字段。
这些设置在新建 Agent 时生效；low 思考仍会产生推理 Token，实际成本及质量需真实任务验证。
