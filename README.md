# my-claw

基于 DeepAgents / LangGraph 的开发 Agent，支持 **Web、CLI 和飞书**接入。Agent 与模型调用运行在服务器，生成代码的文件操作和命令执行交给 **Daytona 远程沙箱**；对话保存在 PostgreSQL，项目快照保存在服务器磁盘。

当前是**本机单用户、单实例部署**，尚未实现登录鉴权、可靠后台任务队列或多服务器协调。

## 已实现的能力

| 模块 | 当前行为 |
| --- | --- |
| Web | 网页聊天、新建/切换会话、读取历史、上传文件、确认删除会话 |
| CLI | 连续对话、切换线程、查看历史、上传文件、删除会话 |
| 飞书 | 自建应用长连接接收文本，群成员共享 chat_id 对应的会话与项目，回复原消息 |
| Agent | 主代理、7 个角色配置、Skills、AGENTS.md、工具审计、用量计量、调用限制、task 串行化 |
| 沙箱 | 同用户同线程复用；丢失且有有效快照时重建；不默认上传平台源码 |
| 持久化 | 一个异步 PostgreSQL 池同时供 AsyncPostgresSaver / AsyncPostgresStore 使用；Redis 独立异步池 |
| 文件 | 用户上传进入沙箱，任务结束保存源码与项目素材，恢复时校验文件哈希 |
| 清理 | 空闲回收保留项目快照；用户主动删除会话时清理沙箱、快照和对应数据库记录 |
| API 基础 | Pydantic 严格模型、依赖注入、统一异常响应、请求 ID、liveness/readiness |

## 快速启动

以下命令在仓库根目录执行。需要 **Python 3.12+、uv、Docker**，以及有效的模型和 Daytona 凭据。项目 `.python-version` 默认使用 3.12；PyCharm 解释器选择 `.venv/Scripts/python.exe`。

```powershell
uv sync --locked --all-extras
```

### 1. 配置模型与基础资源

在项目根目录创建或补充 `.env`，已有文件请保留现有值。下面的密钥是占位符，数据库连接需与实际 Docker 配置一致：

```dotenv
SYC_MODEL_NAME=deepseek-flash
SYC_MODEL_PROVIDER=openai
SYC_BASE_URL=https://api.deepseek.com
SYC_API_KEY=填写模型密钥
SYC_MAX_RETRIES=12
SYC_TIMEOUT=60
SYC_LOG_LEVEL=INFO

DAYTONA_API_KEY=填写Daytona密钥
DAYTONA_API_URL=https://app.daytona.io/api
SYC_SANDBOX_SEED_PLATFORM=false
SYC_SANDBOX_TASK_TIMEOUT=600

SYC_POSTGRES_URL=postgresql://my_claw:填写数据库密码@127.0.0.1:5432/my_claw
SYC_REDIS_URL=redis://127.0.0.1:6379/0
LANGSMITH_TRACING=false
```

连接串中的密码若包含 `@`、`:` 等特殊字符，需要 URL 编码。模型名、端点和权限须以实际账号可用配置为准；上面对应项目默认设置。搜索/网页读取工具目前仅支持 DeepSeek 官方服务，更换模型供应商不会自动替换搜索工具。

`.env`、`.env.docker` 已被 Git 忽略。模型和 Daytona 管理密钥只在服务器使用，不注入远程环境。LangSmith 由环境变量控制；开启后可能上传提示词、模型回复和工具记录，不是本地日志开关。

### 2. 启动数据库

根目录 `.env.docker` 使用以下变量，值必须和 `.env` 的数据库连接对应；不要将模型密钥放入这个文件：

```dotenv
MY_CLAW_POSTGRES_USER=my_claw
MY_CLAW_POSTGRES_PASSWORD=填写数据库密码
MY_CLAW_POSTGRES_DB=my_claw
MY_CLAW_POSTGRES_PORT=5432
MY_CLAW_REDIS_PORT=6379
```

```powershell
docker compose --env-file .env.docker up -d --wait
uv run --all-extras python -m tests.verify_env
```

PostgreSQL 使用 `pgvector/pgvector:pg17`，Redis 使用 `redis:8-alpine`，端口仅绑定 `127.0.0.1`。容器数据分别位于 `../docker-data/postgres`、`../docker-data/redis`。已有数据目录不会因修改初始化环境变量自动更改账号密码，详见 [数据库说明](infra/README.md)。

`open_resources()` 负责建池、连通检查、Saver/Store 的 `setup()` 和统一关闭。Web 与飞书是独立进程，各自创建自己的资源池；同一进程内复用。

### 3. Web 与 CLI

```powershell
uv run --all-extras python scripts/serve_api.py
```

- 网页：<http://127.0.0.1:8000>
- 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /healthz` 纯探活，`GET /readyz` 检查 PostgreSQL / Redis；不代表模型或 Daytona 已连通。

另开一个终端运行 CLI：

```powershell
uv run --all-extras python scripts/serve_cli.py
# 指定已有会话
uv run --all-extras python scripts/serve_cli.py --thread-id example-session
```

CLI 命令：`/new`、`/use ID`、`/history`、`/upload 本地文件路径`、`/delete`、`/exit`。删除需输入 `DELETE` 确认。PyCharm 可直接运行 `scripts/serve_api.py`、`scripts/serve_cli.py`。

Web / CLI / 原 HTTP API 的身份固定为 `user_id=local-user`，相同 thread_id 可续聊；它们不是互相隔离的用户。浏览器生成会话 UUID，并在 localStorage 保存 ID、标题和上传路径；历史消息来自 checkpoint。当前没有服务器会话列表接口，更换浏览器不会自动发现旧会话。

### 4. 飞书

在 `.env` 增加 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`，应用开启机器人能力、相关消息权限，并使用长连接订阅 `im.message.receive_v1`：

```powershell
uv run --all-extras python scripts/serve_feishu.py
```

同应用只启动一个接收进程。飞书不依赖 Web API 进程；需要数据库、模型和沙箱可用。长连接模式不需要公网回调或 Verification Token。

飞书以应用、租户、chat_id 派生共享归属，同群成员共享项目；发送者 open_id 只标识发言来源，不做成员鉴权。只处理文本，尚不接收飞书附件或提供群内删除命令。设置和收发验收见 [飞书说明](channels/feishu/README.md)。

## 一次请求如何执行

```text
Web / CLI / 飞书
  → 渠道解析，确定 user_id、thread_id、channel
  → dispatcher 取得跨进程会话文件锁
  → 复用 Daytona；或创建空工作区 / 从有效快照恢复
  → 构建主 Agent + subagents + skills + memory + middleware
  → 模型在服务器决策，文件工具和 execute 操作远程项目
  → PostgreSQL checkpoint 持久化对话状态
  → 下载并校验项目文件，发布本地快照，更新 SQLite 映射
  → 返回文字回复及 workspace_saved 状态
```

内部会话键由 `SHA256(JSON([user_id, thread_id]))` 派生，同时关联 checkpoint、沙箱映射和磁盘目录。`channel=web/cli/api/feishu` 是请求来源，`agent_role` 是执行角色；两者不改变会话键。哈希不是用户鉴权。

`build_agent()` 只组装完整 Agent，`agent.dispatcher.run_task()` 才是包含沙箱生命周期和文件落盘的一次任务入口。不要绕过 dispatcher 另写一套渠道调用。

## Agent 组件与边界

主代理挂载 `web_search`、`fetch_url` 和框架的规划、文件、委派工具。六个专职角色加一个显式覆盖的通用角色如下：

| 角色 | 作用及工具边界 |
| --- | --- |
| planner | 只读规划步骤和验收标准 |
| researcher | 只读调研，可使用搜索和网页读取 |
| coder | 远程读写代码、执行命令 |
| tester | 远程补测试、运行测试/构建 |
| reviewer | 只读审查，使用 ReviewResult 结构化输出 |
| data_analyst | 在沙箱中用 Python 计算、绘图和导出，不调用宿主表格工具 |
| general-purpose | 只读背景整理，不替代编码/测试角色 |

主子代理共享同一会话沙箱。`TaskSerializationMiddleware` 让同一主 Agent 的 task 委派排队，角色顺序由 AGENTS.md 指导；它不锁住主代理的其他工具，也不管理远程后台进程。角色工具白名单不等于沙箱内部的 OS 用户隔离。

平台 `AGENTS.md`、三个开发技能以及两个分析技能保存在服务器。`CompositeBackend` 将 `/guidance/` 路由到只读指导副本，项目路径与 execute 交给 Daytona。Skills 使用框架渐进式披露；Memory 指的是加载规则文件，不等于对话数据库。技能附带的可执行脚本不会自动上传到沙箱。

自定义中间件负责请求上下文、工具审计、Token 计量、task 串行化、模型调用限制。DeepAgents 另外组装框架内置组件；其具体行为以锁定依赖和实际图为准。

| 限制 | 默认值 |
| --- | --- |
| 主代理模型调用次数 / 单次运行 | 30 |
| 子代理模型调用次数 / 每次委派 | 40 |
| dispatcher 图递归步数 | 1000（包含中间件节点，不等于模型调用数） |
| Agent 执行超时 | 600 秒，可由 `SYC_SANDBOX_TASK_TIMEOUT` 修改 |
| 远程命令超时 | 300 秒 |

远程调用超限会报错并尽力保存工作区，不保证“平稳成功”。Agent 超时不包含沙箱准备与后续文件传输。Token 计量不是金额预算，也没有全团队统一 Token 硬限额。

DeepSeek 官方端点使用 `infra/model_policy.py` 的兼容适配：主代理、researcher、tester、reviewer、general-purpose 关闭思考，planner、coder、data_analyst 使用 low 思考；保留工具调用需要的 reasoning_content。其他供应商不套用该策略。

## 文件上传、保存与删除

### 上传

网页选择文件，或 CLI 执行 `/upload 路径`。服务器按流检查大小，单文件上限 **20 MiB**，写入当前沙箱的 `inputs/唯一前缀_文件名`，保存工作区快照后返回路径。不会自动解压、执行或覆盖同名文件。

网页会把已上传路径附到后续任务；CLI 显示路径，由用户在任务中引用。文件位于用户电脑时，必须经上传传递，服务器不能凭用户电脑路径直接读取。分析角色优先使用委派提供的远程绝对路径，兼容 `inputs/` 和已有项目的 `data/inputs/`。

### 保存与恢复

```text
.sandbox-state/
  sessions.sqlite3                 # 沙箱 ID、状态、有效快照路径等映射
  <会话键>.lock                    # 同会话跨进程互斥
  workspaces/<会话键>/
    versions/<版本ID>/
      project/                     # 实际保存的项目文件
      manifest.json                # 相对路径、类型、权限、大小、哈希
    staging/                       # 正在下载的临时版本
  guidance/<内容哈希>/             # 共享的只读指导副本
```

正常任务结束保存；异常时尽力保存。下载和校验成功后才更新有效版本，保留当前和上一版本。`workspace_saved=true` 只代表本次项目快照已保存，不代表代码测试全部通过。备份失败保留旧版本，不冒充成功。

保存源码、测试、配置、锁文件、静态资源和输入文件；排除依赖、缓存、日志、根目录构建输出及 `outputs/`。沙箱清理后恢复文件并按需重新安装依赖，不恢复运行进程、终端历史或预览链接。分析报告若写入 `outputs/analysis/`，当前不会持久保存，Web 也尚无产物下载接口。

更多校验规则、符号链接处理和异常恢复见 [工作区持久保存](docs/workspace-persistence.md)。

### 空闲回收与主动删除不同

```powershell
uv run --all-extras python -m scripts.sandbox_cleanup --watch
```

回收器默认每 60 秒检查，空闲阈值 3600 秒；它是独立进程，不随 API 自动启动。空闲回收先保存再删除远程沙箱，保留本地恢复数据。Daytona 另配置自动停止 30 分钟、停止后自动删除间隔 1440 分钟；平台回收不保证触发应用备份。

用户主动删除通过 `DELETE /v1/agent/sessions/{thread_id}`：取得会话锁后删除远程沙箱、checkpoint、会话级 Store 数据、本地项目快照及 SQLite 映射。忙时返回 409；部分失败保留删除中标记供重复请求继续清理。不会清空数据库、Redis、其他会话、共享指导副本或历史服务日志。空锁文件保留，避免破坏并发锁语义。

## HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/v1/agent/tasks` | 原有入口，来源 api |
| POST | `/v1/agent/channels/web/tasks` | 网页入口 |
| POST | `/v1/agent/channels/cli/tasks` | CLI 入口 |
| GET | `/v1/agent/sessions/{thread_id}/messages` | 可展示的 checkpoint 历史 |
| POST | `/v1/agent/sessions/{thread_id}/files?filename=example.csv` | 原始二进制请求体上传，不是 multipart |
| DELETE | `/v1/agent/sessions/{thread_id}` | 主动清理会话 |
| GET | `/healthz`、`/readyz` | 服务及数据库健康检查 |

任务请求为 `{"thread_id":"demo-1","message":"创建一个页面"}`，返回 `thread_id/status/reply/workspace_saved`。模型调用、上传和删除都在当前请求内等待完成，没有后台任务队列。错误响应格式为 `code/message/request_id`，响应头包含 `X-Request-ID`。

Web / CLI 新接口校验本地主机名及浏览器 Origin；这不是多人身份认证。请保持默认本机监听，公开部署前补充登录、归属校验、配额和网络访问策略。

## 测试与真实验收

```powershell
# 自动回归：假模型/测试后端为主，不触发真实模型任务
uv run --all-extras python -m pytest -q
uv run --all-extras python -m unittest discover -s tests -v

# 基础资源连通，真实 PostgreSQL / Redis，无模型或 Daytona 调用
uv run --all-extras python -m tests.verify_env

# 显式数据库集成，使用随机测试线程，仅清理测试数据
$env:SYC_TEST_DATABASE="1"
uv run --all-extras python -m pytest tests/test_postgres_integration.py -q
Remove-Item Env:SYC_TEST_DATABASE

# 显式本地离线验证：真实框架/文件工具，模拟模型
uv run --all-extras python -m tests.verify_agent --offline
```

以下命令会调用真实模型或外部服务，并可能产生费用：

```powershell
# 真实远程团队：开发、测试、审查、保存与重建验证
uv run --all-extras python -m scripts.verify_web_team
# 一次远程任务（使用正式 dispatcher）
uv run --all-extras python -m scripts.devmate_with_mw --thread-id issue-001 --task "检查项目并总结"
```

`verify_web_team` 默认主代理 30 次、子代理 40 次、Agent 超时 480 秒，不是网页聊天 UI 验收。结果记录在 `logs/web-team-verification/`。更多旧验收入口的上传范围和限制见 [Daytona 说明](docs/daytona.md)。

`build_local_agent()` 是显式本地验证模式，默认不启用子代理，使用进程内 MemorySaver 和本地文件系统。`enable_subagents=True` 才挂载本地角色与表格工具。它不提供 OS 隔离，不能替代正式远程入口处理不可信代码。

## 当前边界与后续工作

- **用户隔离**：本地渠道共用 local-user，飞书群共享身份；未实现登录、成员授权或多租户服务。
- **对话历史**：checkpoint 是可恢复的图状态，不是独立聊天归档；消息被框架总结/裁剪后，历史接口不会恢复被裁剪原文。
- **长期记忆**：Store 已注入，但没有自动提取用户偏好、语义检索或跨会话记忆策略。Redis 已接入，尚未承担队列、限流或会话缓存。
- **可靠任务**：HTTP 等待完整结果；飞书队列/去重在内存中，强制退出可能丢失待处理任务，回复发送失败不自动重跑 Agent。
- **并发部署**：文件锁和 SQLite 仅协调同一台主机、同一状态目录；当前数据库 setup 按单进程启动设计。
- **结果交付**：已有文字回复与源码快照，未实现产物下载、预览服务管理或运行报告长期保存。
- **边界验证**：离线测试不能证明模型鉴权、Daytona 镜像依赖或真实工具选择正确；真实环境变更后需单独验收。

下一步优先考虑身份与会话列表、产物下载，再按需求引入可靠队列和多实例协调。

## 目录与进一步说明

| 目录 | 职责 |
| --- | --- |
| `agent/` | 模型与 Agent 构建、任务 dispatcher、会话服务、只读指导路由 |
| `api/`、`domain/` | HTTP 组装、严格契约、依赖注入和业务异常 |
| `channels/` | Web 页面、CLI、飞书适配 |
| `sandbox/` | 同步 SDK 异步桥接、沙箱生命周期、磁盘快照 |
| `subagents/`、`middleware/` | 角色、分析技能和运行控制 |
| `tools/` | 搜索/网页工具，以及显式本地模式的测试与表格工具 |
| `infra/` | 配置、日志、异步入口、连接池、模型策略 |
| `.agents/skills/`、`AGENTS.md` | 开发技能与通用运行规范 |
| `tests/`、`scripts/` | 自动回归、服务启动、回收与显式验收 |

详细说明：[本地渠道与接口](channels/README.md) · [飞书接入](channels/feishu/README.md) · [沙箱配置](docs/daytona.md) · [文件持久化](docs/workspace-persistence.md) · [数据库配置](infra/README.md)。
