# my-claw 项目协作约定

本文件适用于整个仓库。子目录的 AGENTS.md 可补充本目录约定；用户当前指令优先。
使用中文沟通，保留代码和第三方 API 的原始标识符。

## 项目结构

项目使用 Python、DeepAgents、LangChain / LangGraph，目标是可自部署的研发助手。
以当前源码为准，不把依赖声明、空目录或示例当成已完成的生产能力。

| 目录 | 职责 |
| --- | --- |
| agent/ | 模型与主 Agent 构建 |
| middleware/ | 工具审计、Token 计量、请求上下文等钩子 |
| infra/ | 配置、日志和本地数据库初始化 |
| scripts/ | 可运行示例和开发入口 |
| tests/ | 单元测试、导入回归和环境自检 |
| 其他功能目录 | 按现有实现判断能力，新增逻辑放入对应边界 |

## 订单微服务 · 团队约定（DevMate 常驻记忆）

以下业务画像用于 DevMate 开发和审查订单微服务；编码与测试铁律也适用于本仓库后续代码改动。
业务项目的 Python 版本和目录布局不代表当前 my-claw 已完成迁移：my-claw 的运行版本仍以 `.python-version` 为准，现为 Python 3.12。

### 项目

- 技术栈：Python 3.13 + FastAPI；包管理用 uv。
- 目录：源码在 `app/`，测试在 `tests/`，定价逻辑集中在 `app/pricing.py`。

### 必须遵守（铁律）

- 金额一律用 `Decimal` 计算，**禁止用 float 做货币运算**；从字符串或精确整数构造金额，避免 `Decimal(float)` 引入二进制浮点误差。
- 所有公开函数带类型注解和 docstring。
- 任何代码改动都必须附带对应的 pytest 测试，覆盖改动的实际行为；纯文档变更进行内容校验即可。

### 风格

- 内部命名用 snake_case；对外 API 字段用 camelCase，由 Pydantic alias 处理。
- 提交信息用祈使句，一行概述 + 可选正文。该格式约定不代表自动获得提交或推送授权。

## 环境与运行

- Python 版本遵循 `.python-version`；项目和第三方依赖安装在 `.venv`，使用 uv 管理。
- 项目通过 setuptools 可编辑安装。新增导入问题先检查解释器、安装和包发现配置，避免继续逐文件添加 `sys.path` 或修改系统 `PYTHONPATH`。
- 新增常规包时保留 `__init__.py`，确认 `pyproject.toml` 中的包发现规则包含该包。
- 配置统一通过 `infra.settings.get_settings()` 获取，保留现有 `SYC_` 环境变量契约；`.env` 从项目根目录定位。
- 创建或更换环境后执行 `uv sync --locked --all-extras`。有意修改依赖时同步更新 `pyproject.toml` 与 `uv.lock`；不要无关升级依赖。

```powershell
uv sync --locked --all-extras
uv run --all-extras python -m scripts.devmate_with_mw --thread-id issue-001 --task "检查项目并总结"
uv run --all-extras python -m pytest -q
uv run --all-extras python -m unittest discover -s tests -v
uv pip check --python .venv\Scripts\python.exe
```

PyCharm / Cursor 使用 `.venv\Scripts\python.exe`，工作目录为本项目根目录。
未安装的格式化器、静态检查器不得作为“已通过”的检查；确需引入时明确新增依赖和配置。

## 代码规范

- 保留现有模块边界和公开调用方式，修改聚焦用户目标，不覆盖用户未提交的工作。
- Python 使用四空格缩进、snake_case 函数/变量、PascalCase 类；所有公开函数带类型注解和 docstring，对外 API 的 camelCase 字段通过 Pydantic alias 映射。
- 注释和 docstring 解释职责、约束和原因，保持示例命令与实际文件名一致。
- 异步 I/O 使用异步 API；异步回调标为 `Callable[..., Awaitable[...]]`。连接需有合理超时，并通过上下文管理器或 finally 释放资源。
- 中间件业务状态存放在图状态中，逐请求身份可使用运行上下文；不要把不同用户的请求数据放入共享实例属性或进程全局变量。
- `state_schema` 声明字段结构，不初始化字段值。`NotRequired` 字段可能不存在；明确每个累计字段的生命周期、默认值和 reducer。
- 节点式钩子用字典提交状态更新；模型包裹式钩子更新状态时保留响应并返回 `ExtendedModelResponse` 携带 `Command`。按当前安装版本验证具体协议。
- 普通状态字段更新通常是覆盖，不是自动累加；并发分支、重试和会话恢复需检查重复计算及覆盖风险。
- 日志统一使用 `infra.logging.get_logger()`。区分调用返回、业务成功和异常，不将错误 ToolMessage 记录为成功。

## 密钥与数据库

- `.env`、`.env.docker`、真实 API Key、含密码的连接串不进入 Git、日志、截图或 review 输出。
- 排查配置时输出“是否存在/是否一致”等结果；对外展示连接信息须脱敏。
- 模型接口、密钥所属平台和模型 ID 需匹配。`openai` 可表示兼容协议，不代表请求必然发往 OpenAI。
- 当前容器配置见 `compose.yaml`；数据绑定到项目上一层的 `docker-data/postgres` 与 `docker-data/redis`，不要将该位置当成临时目录。
- PostgreSQL 使用 pgvector 镜像和初始化 SQL；容器健康检查之外，还需用应用连接配置验证扩展与连接。
- pytest / unittest 回归默认不依赖真实模型、真实密钥或数据库。`tests/verify_agent.py` 是单独的验收入口，直接运行默认调用真实模型，`--offline` 使用模拟模型；真实模式遵循 LangSmith 配置上传追踪。真实 API 调用、容器操作与数据修改须在任务授权范围内；验收产物保留在 `logs/agent-verification/`，不要将该目录作为生产数据。
- 保留数据库持久化目录。删除、重建已有数据库或迁移数据前明确影响；不要为修复连接错误删除数据。

## 验证与交付

- 代码改动附带对应的 pytest 测试，覆盖本次相关的成功、失败和边界路径；现有 unittest 用例可由 pytest 收集运行并保留。纯文档或低影响配置变更进行适当校验即可。
- 涉及导入、包布局、入口脚本时，验证文件路径启动和模块启动，并从项目外目录、无 PYTHONPATH 条件下验证导入；测试时避免误触发真实模型调用。
- 导入部分模块也会加载配置和初始化文件日志。隔离测试使用临时工作目录、占位模型凭据；追踪遵循环境配置，不能把“不执行 main”当成无副作用保证。
- 单元测试通过不能证明真实模型鉴权、数据库可用或持久化正确。区分单元测试、导入检查和集成验证。
- 交付说明包括实际改动、执行过的检查及结果、尚未验证的部分；不把预期结果写成实际结果。
- 除非用户要求，不自动提交、推送、部署或扩大重构范围。

## 项目技能与调用顺序

技能目录为 `.agents/skills/`；远程 Agent 使用 workdir 开头的绝对路径配置 skills 和 memory，
显式本地模式使用 `skills=["/.agents/skills/"]` 与 `memory=["/AGENTS.md"]`。先查看技能描述，任务匹配时读取完整 SKILL.md，
不要每次任务都加载全部技能。用户指令优先于技能通用建议。

| 场景 | 使用技能 | 目的 |
| --- | --- | --- |
| 本仓库实现、修复、重构或代码审查 | [my-claw-code-quality](.agents/skills/my-claw-code-quality/SKILL.md) | 项目规范、异步协议、测试及审查要求 |
| 报错、测试失败、行为异常、调用中断 | [systematic-debugging](.agents/skills/systematic-debugging/SKILL.md) | 复现、收集证据、定位根因，再最小修复 |
| 准备报告修复完成、测试通过或功能可用 | [verification-before-completion](.agents/skills/verification-before-completion/SKILL.md) | 用本次相关验证结果支持结论 |
| 本地 Excel/CSV 质量检查、筛选、分组、关联分析 | [local-data-analysis](subagents/skills/data-analysis/local-data-analysis/SKILL.md) | 明确指标口径并通过工具计算 |
| 分析图表及结果表/报告导出 | [analysis-reporting](subagents/skills/data-analysis/analysis-reporting/SKILL.md) | 根据已计算结果生成可追溯产物 |

修复任务按“调试定位 → 项目规范下修复 → 完成前验证”推进；普通解释或文档修改不强制走完整开发流程。
同一变更已有有效检查结果时可直接引用，无需为总结、换阶段或重复对话再次运行。
仅在代码/配置变化、检查失败或出现新疑点时重跑对应检查。优先复用现有测试，避免重复验证同一行为。
说明验证范围、退出码、失败原因及未验证部分；未运行的检查不能写成通过。

### 当前工具与运行边界

- 当前默认是主 Agent + 远程子代理 + Daytona：build_agent 显式接收 backend/workdir，注册 web_search、fetch_url、task，文件与 execute 由同一会话沙箱提供。
- 主 Agent 加载 .agents/skills/ 中三个开发技能；数据分析技能只绑定 data_analyst，ReviewResult 只绑定 reviewer。主 Agent 不直接注册表格工具。
- 远程子代理与主代理共享 backend/workdir，显式配置各自的 memory、skills、工具和中间件。build_local_agent(enable_subagents=True) 仅保留本地验收模式，不与远程角色混用。
- 默认远程模式通过 execute，在远程工作目录执行 uv run --locked --extra daytona python -m pytest tests -q；不调用本机 run_pytest。
- 开发工具验收：python -m tests.verify_development_tools，默认真实模型；--offline 模拟模型与联网，pytest 仍实际执行。

- 工具以当前模型实际绑定的名称和参数为准。`.agents/skills/`、`tools/` 中存在文件不代表工具已注册。
- `web_search` 查外部资料，`fetch_url` 读已知网页；本地定位使用 grep/glob，读取使用 read_file。
- `run_pytest` 仅用于显式 build_local_agent 验证，不能用于证明远程沙箱中的代码通过测试。
- 若没有命令执行/pytest 工具，应报告“已生成、未执行验证”，不能根据静态阅读宣称测试通过。
- 若没有 write_todos，用简短文字列出步骤，不虚构工具调用。技能不会自动赋予 shell、网络或子 Agent 能力。
- 技能中的 Bash/Unix 示例需按 Windows PowerShell 和现有工具调整，不能盲目执行辅助脚本。
- 调试仅输出必要的脱敏信息，不打印完整环境变量、模型密钥或含密码连接串。
- 终端遵循中间件风格：步骤摘要、模型轮次、Token、工具状态/耗时、最终结果；完整提示词及历史消息不展开。
- 故障保留脱敏报错堆栈；LangSmith 上传由显式配置和任务授权控制，离线验收同样遵循环境配置。

### 子代理使用场景与搭配

简单解释、只读查询由主 Agent 直接完成；按需委派，不要求每次使用全部角色。

| 子代理 | 使用场景 | 常见搭配 |
| --- | --- | --- |
| planner | 复杂需求拆解、明确步骤和验收标准 | 必要时结合 researcher，再交 coder |
| researcher | 查清仓库实现、接口或外部资料 | 为 planner / coder 提供事实依据 |
| coder | 实现功能、修复缺陷、修改代码 | 完成后交 tester，再交 reviewer |
| tester | 补充关键测试、在沙箱运行并定位失败 | 接 coder 产物；失败交回 coder，通过交 reviewer |
| reviewer | 只读审查代码、规范与测试证据，返回 ReviewResult | 接 coder 改动和 tester 结果；阻塞项交回 coder |
| data_analyst | 远程 CSV/XLSX 检查、计算、绘图和报告 | 通常独立完成；需改分析代码时搭配 coder → tester |
| general-purpose | 不适合专职角色的只读背景整理 | 按需辅助主 Agent，不替代开发、测试或审查角色 |

### task 委派规则（远程开发）

- 开发默认 coder → tester → reviewer；等待上游完成、检查产物后再委派下游。测试失败或审查阻塞时回到 coder，自动修复最多两轮，之后报告证据和待处理项。
- 每轮最多提出两个独立只读 task，运行时仍排队执行；最多一个可能写入的委派（coder / tester / data_analyst）。写入期间主 Agent 不并行修改或验证相关文件。
- description 写明目标、沙箱绝对路径、上游产物、允许修改范围和验收标准。tester 返回命令、退出码及测试摘要；reviewer 根据实际证据审查，不能把未验证说成通过。
- 子代理不再向下委派。data_analyst 使用专属技能和远程计算库，输入须已在沙箱 data/inputs/；不得访问宿主数据路径。
- 角色顺序和每轮数量是行为指导；TaskSerializationMiddleware 在同一 Agent 实例内对 task 完整执行加锁排队，但不锁普通文件工具或 execute。各子代理有独立调用上限，入口控制总超时；主代理 Token 统计不含子代理。

### 业务示例与实际仓库

订单微服务的 `app/`、`app/pricing.py` 为业务背景，不代表当前工作目录一定存在这些文件。
先检查实际目录和用户指定目标；不能仅因业务示例就创建新的 app 布局或改变指定输出路径。
隔离验收只使用本次工作目录提供的文件；引用宿主项目规则不代表宿主项目代码也已复制进来。

新增两个技能来源为 obra/superpowers，安装信息和许可见
[技能来源](.agents/skills/SOURCES.md)。新建 Agent 后发现最新技能；复用已有会话时需刷新 skills_metadata。
