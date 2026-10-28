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
uv run --all-extras python -m scripts.devmate_with_mw
uv run --all-extras python -m pytest -q
uv run --all-extras python -m unittest discover -s tests -v
uv pip check --python .venv\Scripts\python.exe
```

Cursor 使用 `.venv\Scripts\python.exe`，当前文件调试使用仓库的 launch 配置。
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
- 测试默认不依赖真实模型、真实密钥或数据库。真实 API 调用、容器操作与数据修改须在任务授权范围内；测试隔离临时数据并清理。
- 保留数据库持久化目录。删除、重建已有数据库或迁移数据前明确影响；不要为修复连接错误删除数据。

## 验证与交付

- 代码改动附带对应的 pytest 测试，覆盖本次相关的成功、失败和边界路径；现有 unittest 用例可由 pytest 收集运行并保留。纯文档或低影响配置变更进行适当校验即可。
- 涉及导入、包布局、入口脚本时，验证文件路径启动和模块启动，并从项目外目录、无 PYTHONPATH 条件下验证导入；测试时避免误触发真实模型调用。
- 导入部分模块也会加载配置和初始化文件日志。隔离测试使用临时工作目录、占位凭据并关闭真实追踪，不能把“不执行 main”当成无副作用保证。
- 单元测试通过不能证明真实模型鉴权、数据库可用或持久化正确。区分单元测试、导入检查和集成验证。
- 交付说明包括实际改动、执行过的检查及结果、尚未验证的部分；不把预期结果写成实际结果。
- 除非用户要求，不自动提交、推送、部署或扩大重构范围。

## 项目技能

处理本仓库的代码实现、修复、重构或代码 review 时，读取
[my-claw-code-quality](.agents/skills/my-claw-code-quality/SKILL.md)，选择匹配的开发或 review 模式。
单纯解释已有代码时不强制启动开发或 review 流程。
