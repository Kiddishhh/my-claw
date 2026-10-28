# my-claw

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
uv run --all-extras python -m scripts.devmate_with_mw
uv run --all-extras python tests/veify_env.py
```

也可直接使用 `.venv` 中的 Python 按文件路径运行：

```powershell
.\.venv\Scripts\python.exe scripts/devmate_with_mw.py
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

## Agent 的 Skills 与 Memory

`build_agent()` 使用项目根目录作为 `FilesystemBackend` 根目录，并启用虚拟路径：

- `skills=["/.agents/skills/"]`：发现项目技能的名称和描述，需要时通过文件工具读取完整 `SKILL.md`。
- `memory=["/AGENTS.md"]`：将项目规则加载到系统提示词。
- `MemorySaver()`：在当前 Agent 实例的进程内保存会话状态，不等于数据库持久化。

带 checkpointer 的调用需要传 `thread_id`：

```python
agent = build_agent(max_model_calls=5)
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

`scripts/_common.py` 保留原有 `print_all` 等打印接口，并提供：

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

agent = build_agent(max_model_calls=5)
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
# 默认离线：假模型 + 真实 Agent 图、中间件、checkpoint 和文件工具，不消耗 API Token。
uv run --all-extras python tests/verify_agent.py --offline

# 真实模型：使用 .env 的模型与密钥，会产生费用。
uv run --all-extras python tests/verify_agent.py --live --max-model-calls 6 --max-input-tokens 30000 --timeout 120
```

测试入口位于 `tests/verify_agent.py`（沿用项目现有的 `tests/` 目录）。
复用 `scripts/_common.py` 的 `run_with_budget` 控制预算，
用 `called`、`written_paths`、`first_turn` 提取调用证据，`print_run` 和 `report` 在控制台打印轨迹与检查结果。
也可运行 `uv run --all-extras python -m tests.verify_agent --offline`；使用 pytest 时加 `-s` 显示过程输出。

脚本将项目真实 `AGENTS.md` 和 `my-claw-code-quality/SKILL.md` 复制到独立验证目录，
附加不同的随机标记。Memory 标记不出现在用户任务中；Skill 标记只有读取正文后才能取得。
Agent 需读取技能、将两种标记写入 `/verification.json`，再用文件工具回读。
程序结合实际系统提示词、checkpoint 元数据、成功工具结果和宿主机磁盘内容进行断言。
不修改原始规范和业务文件，也不会复制 `.env`、数据库或密钥到验证工作目录。

终端逐步打印模型开始/结束、每轮 Token、工具名称/参数/结果/状态/耗时、图节点更新、
累计用量以及检查结果。框架模型调用轮次由回调计数，和包含框架提示的 AIMessage 数分开显示；
不包含模型 SDK 内部的 HTTP 重试。输入预算在调用完成后检查，不能保证不超额。

每次运行保留在 `logs/agent-verification/<run-id>/`（已被 Git 忽略）：

- `events.jsonl`：带时间戳、轮次和调用 ID 的过程记录，常见凭据脱敏。
- `report.json`：检查结果、调用次数、已观测 Token、停止原因和熔断标记。
- `workspace/verification.json`：通过真实 FilesystemBackend 写入的验证产物；失败时可能不存在。

`--log-dir` 可以指定记录的父目录。停止原因区分 `completed`、`input_budget`、
`model_call_limit`、`timeout` 和 `error`。缺少用量信息时预算不能被可靠验证，验收不会算通过。
退出码 0 表示所有硬性检查通过，1 表示未通过；默认不自动重试。远程 LangSmith 追踪在此验证运行中关闭。
离线通过只证明程序接线和文件通路，不能证明真实模型鉴权、工具选择或规范遵循；这些由 `--live` 验证。
