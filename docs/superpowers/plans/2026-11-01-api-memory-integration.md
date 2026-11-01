# API 层与完整 Agent 持久化接入实施计划

## 本轮执行记录（2026-11-01）

- 用户已批准一次完成步骤 1–4；步骤 5–8 暂不实施。
- 步骤 1：已实现独立 ResourceSettings、PG/Redis 池、共享 Saver/Store、startup setup 和失败关闭。
- 步骤 2：完整远程 Agent 显式接收 Saver/Store，保留 skills、guidance、subagents 和中间件；CLI 接入资源上下文，验收入口显式使用内存组件。
- 步骤 3：Agent 在调用方循环运行，同步沙箱进入/退出及 I/O 在同一专用线程；内部线程键包含用户。取消/超时先等待同步 I/O 完成再备份解锁。
- 步骤 4：已添加基础 FastAPI、strict schemas、DI、错误处理、请求日志、两个被动健康接口和 Windows 启动入口。
- 决策：启动仍按单进程，暂不实现多 worker 初始化锁；在增加 worker 前再补迁移串行化。资源配置独立于模型密钥，健康服务不要求模型凭据。
- 独立审查发现同步 backend 的 to_thread 取消风险，已补充阻塞命令取消/超时回归测试并改为 SessionBackend。
- 首轮全量离线测试 185 passed、13 subtests passed；最终修复后的结果见交付说明。
- 取消修复后全量：187 passed、1 skipped、13 subtests passed；跳过的是需真实数据库的显式验收。业务异常映射补充后另行验证基础 API。
- 真实 PG/Redis 验收受 Docker Desktop 保存 settings-store.json 失败阻塞，尚未声称数据库连通。新增显式开启的真实数据库恢复测试。
- 未新增第 5 步以后的业务端点；未提交或推送。

> **For agentic workers:** 使用 superpowers:executing-plans 按批准的步骤实施。先阅读本方案，逐步展示有注释的改动与验证结果；用户确认后再进入下一步。不自动提交、推送或创建后台任务。

**Goal:** 在现有完整开发 Agent 上补齐异步 PostgreSQL/Redis 资源、持久化记忆、API 和可查询的后台任务，不重写既有沙箱和角色能力。

**Architecture:** 每进程生命周期创建一个 PostgreSQL 异步池供 Saver、Store 和业务 Repository 使用；Redis 普通客户端及队列资源明确各自所有权。模型运行与数据库资源处于同一事件循环，同步 Daytona/文件操作隔离到线程。HTTP 路由通过 Depends 获取服务；服务与 Agent 不依赖 FastAPI。

**Tech Stack:** 当前锁定的 FastAPI、Pydantic v2、DeepAgents、LangGraph、psycopg_pool、redis.asyncio、Daytona；若批准后台队列方案，复用已声明的 ARQ 可选依赖。

**Spec:** 本文以下“已确定要求”和“建议确认项”为当前讨论的设计基线。文档状态：待用户审核，未开始功能实现。

## 已确定要求 / Global Constraints

- 基于已有代码增量实现，详细中文注释，逐步审核；本次仅新增计划文档。
- 保留 DeepSeek 按角色思考配置、7 个子代理、工具、开发/分析技能、只读 guidance、AGENTS.md 和现有 Agent 中间件。
- 远程正式入口改为显式注入 Saver/Store，不静默回退内存。离线测试显式注入内存组件；本地测试入口保留。
- 每个进程有自己的池；Saver/Store 共享同一进程的 PostgreSQL 池，不跨事件循环、进程使用。
- 在启动阶段执行 await checkpointer.setup()、await store.setup()。开发期只由一个初始化所有者执行；增加 worker 时以 PostgreSQL advisory lock 串行化初始化，锁连接不占死 Saver/Store 用池空间。
- PostgreSQL/Redis Docker 服务和现有数据卷继续使用，不重建数据库或清空数据。API 默认 127.0.0.1:8000；PG/Redis 已配置宿主 5432/6379（允许环境覆盖）。
- 保留 /healthz 和 /readyz 被动接口，不新增周期请求它们的任务。compose 中已有数据库自身 healthcheck 保留。
- 输入/输出使用明确的 Pydantic v2 模型：strict=True、extra=forbid；路径/查询字符串单独明确解析规则。
- 可预期业务失败与 HTTP 解耦；API 返回安全提示和稳定 code，未知故障记录必要的脱敏诊断，不输出密钥、请求正文、推理原文、内部磁盘路径。
- 会话归属先校验，checkpoint 使用稳定的 user_id+thread_id 内部键；用户身份不从请求正文或任意 user_id 请求头信任取得。
- SQLite 保留沙箱映射，服务器磁盘保留源码快照。初期单机部署；不是多机器共享存储方案。
- 长期 Store 绑定不等于自动记忆；先实现明确偏好读写，不做自动抽取或向量检索。
- 不自动重新执行可能产生副作用的 Agent 任务，不将进程恢复等同于可以安全重放。

## 当前代码事实

- agent/main.py 已完整组装 backend、skills、memory、subagents 和中间件，但固定 checkpointer=MemorySaver()，未传 store。
- agent/dispatcher.py 的 async run_task 把全任务放进 to_thread，内部 asyncio.run(invoke())；必须调整才能安全使用生命周期创建的异步池。
- sandbox/manager.py 的 SDK、SQLite、FileLock 生命周期以及 sandbox/workspace.py 的文件保存是同步操作。
- api/、services/、repositories/、workers/ 以及 infra/resources.py 等本轮讨论模块尚未落地。
- compose.yaml 已有 PG17+pgvector、Redis、持久卷和端口声明；尚未在本次审核中验证容器运行状态。

## 建议确认项

1. 后台运行采用 POST 返回 202 + run_id、ARQ worker 执行、GET 查询；不采用长时间占用 HTTP 请求。以下后半计划按此建议编写。
2. MVP 认证采用服务端配置的 Bearer token 到用户身份映射，不新建账号注册系统；后续可替换成 JWT/OIDC。不能把示例 token 写入仓库。
3. 任务 succeeded 要求 Agent 完成且源码备份成功；备份失败返回 failed/WORKSPACE_SAVE_FAILED，保留沙箱和此前快照。失败结果另外保留可用的部分回复与保存状态，避免丢失可展示成果。
4. 第一阶段长期记忆仅允许用户明确保存/修改偏好，任务开始时按白名单读取有限条偏好注入上下文；不修改全局 AGENTS.md。
5. 文件上传下载、删除会话全链清理列为后续独立阶段，避免阻塞引擎持久化主线；若本次 API 必须包含，可将步骤 8 纳入本轮范围。

## Review Focus

- 初始化中途失败：已创建资源均关闭，未就绪时不接收业务；关闭一个资源失败仍尝试关闭其他资源。
- 同会话并发/取消：锁覆盖准备、模型执行、备份、释放；取消 to_thread 等待不会停止同步线程，不能提前解锁。
- 跨用户访问/重启：同外部 thread_id 不串数据；同用户同线程重新编译、进程重启后恢复消息且不重复追加。
- 数据库记录与队列投递非原子：入队失败不报告成功；重复任务不重复执行；worker 死亡任务不永久 running。
- 状态与源码不是原子提交：明确 checkpoint 可能领先快照，错误时报告恢复边界，不宣称重启可继续任意执行中步骤。

## 目录与职责（按步骤创建，避免空目录先行）

```text
api/app.py, lifespan.py, dependencies.py, exception_handlers.py
api/middleware/request_context.py
api/schemas/common.py, errors.py, health.py, threads.py, runs.py, memories.py
api/routers/health.py, threads.py, runs.py, memories.py
domain/errors.py, models.py, ports.py
services/threads.py, runs.py, memories.py
repositories/threads.py, runs.py
infra/postgres.py, redis.py, resources.py, auth.py, task_queue.py
infra/migrations/001_threads_runs.sql
workers/settings.py, tasks.py, maintenance.py
sandbox/async_session.py
memory/namespaces.py, policy.py
tests/test_resources.py, test_agent_persistence.py, test_async_sandbox.py
tests/api/, tests/services/, tests/integration/
```

新增包包含 __init__.py，更新 pyproject.toml 包发现规则；现有 agent/main.py、dispatcher.py、settings.py、CLI 与验收脚本同步适配，不留下只能新入口运行的旧脚本。

## 步骤 1：异步资源与启动初始化

**文件：** 新增 infra/postgres.py、redis.py、resources.py；修改 infra/settings.py；测试 tests/test_resources.py。

**接口：** build_pg_pool(settings)、build_redis(settings)；AppResources(pg_pool, checkpointer, store, redis)；async context manager open_resources(settings)。后者供 API lifespan 与 worker 复用。

- [ ] 写资源成功初始化、PG 打开失败、setup 失败、Redis PING 失败、关闭动作抛异常时仍继续清理的测试并确认旧代码不满足。
- [ ] 添加池大小、借连接/建连超时校验；PG open=False、autocommit=True、dict_row、prepare_threshold=0（明确这不是禁用预备语句）。Redis.from_pool 明确所有权，ARQ 不复用普通 Redis 客户端。
- [ ] 用 AsyncExitStack 打开池、SELECT 1、构造 Saver/Store、setup、Redis PING；全部成功才返回资源。只打印依赖名与安全状态，不输出 URL。
- [ ] 执行 `.venv/Scripts/python.exe -m pytest tests/test_resources.py -q`；资源关闭和失败路径均通过后展示结果并等待用户确认。

## 步骤 2：完整 Agent 注入持久化

**文件：** 修改 agent/main.py、subagents/profiles.py（仅必要兼容）、相关离线和真实验收调用方；新增 tests/test_agent_persistence.py。

**接口：** build_agent(..., checkpointer: BaseCheckpointSaver, store: BaseStore)。保留所有现有组装参数与预算校验。

- [ ] 测试显式内存 Saver/Store 在两个新建图实例之间恢复同线程消息，保证只传本轮消息；保留既有工具、角色、权限、技能注册断言。
- [ ] 去掉远程入口内建 MemorySaver；明确长期 store 与文档 memory 互不替代。子代理继续每次委派隔离，不误配独立持久化线程导致子状态混入主状态。
- [ ] 更新显式本地/CLI/验收入口的资源选择，测试不连接真实数据库；不要把 HTTP dependencies 引入 Agent 包。
- [ ] 运行 agent persistence、remote agent、subagent、model policy 相关回归；展示编译后的组件清单，等待确认。

## 步骤 3：重构 dispatcher 异步边界与会话锁

**文件：** 新增 sandbox/async_session.py；修改 agent/dispatcher.py、scripts/devmate_with_mw.py、scripts/verify_web_team.py；测试 tests/test_async_sandbox.py、test_dispatcher.py。

**接口：** 异步会话适配器持有专用串行执行线程，在同线程进入/退出原 sandbox_for_thread；提供 async save()，异常类型传回原同步上下文；run_task(..., checkpointer, store) 在调用方事件循环运行 Agent。

- [ ] 先用同步假 SDK 阻塞模拟验证事件循环仍有心跳；PG 假组件记录 loop identity；验证获取、备份、释放锁发生在同一线程。
- [ ] 移除 dispatcher 工作线程内 asyncio.run；同步生命周期/SQLite/文件操作使用适配器，Agent ainvoke 留在共享池所属事件循环。
- [ ] checkpoint 使用已有 _session_key(user_id, thread_id)；沿用取消时等待必要清理、成功保存/失败保留沙箱的行为；不让连续取消跳过释放。
- [ ] 测试取消发生在排队锁、创建沙箱、执行模型、保存文件各阶段；失败保留旧快照；不同线程任务可进展；同会话不得并发写。
- [ ] 运行 dispatcher、sandbox、workspace 回归；用独立测试数据库做假模型两轮对话和重启恢复测试，不调用真实模型/云端。
- [ ] 向用户展示完整持久化引擎验收结果，再进入 API。

## 步骤 4：API 基础层、错误和健康接口

**文件：** api/app.py、lifespan.py、dependencies.py、schemas/common.py/errors.py/health.py、exception_handlers.py、middleware/request_context.py、routers/health.py；domain/errors.py；tests/api/test_foundation.py。

**接口：** create_app()；lifespan 使用 open_resources；get_resources(Request)->AppResources；ResourcesDep；register_exception_handlers(app)。

- [ ] 写严格类型、拒绝额外字段、500 响应不泄露内部数据、404/405、请求 ID 一致、初始化失败的 HTTP/生命周期测试。
- [ ] APIModel 严格控制请求/响应；简洁 ErrorResponse(code,message,request_id)。BusinessError 不含 HTTP，API 集中映射。日志保留安全定位信息，默认不用未脱敏 logger.exception。
- [ ] 原生 ASGI 请求中间件生成服务端 request_id，记录方法、路由模板、状态和耗时；不缓存正文、不修改流内容；取消不伪装成成功。
- [ ] /healthz 不依赖数据库；/readyz 用 Depends 取资源并在总计 3 秒内 SELECT 1 + Redis PING，200/503 返回 HealthResponse。resources 不存在时由全局处理器给标准 503。
- [ ] 验证异常 500 的外层处理和响应头 request_id、流开始后失败不能重写 JSON；提供启动命令 `uv run uvicorn api.app:app --host 127.0.0.1 --port 8000`。

## 步骤 5：身份、会话与短期历史查询

**文件：** infra/auth.py、infra/migrations/001_threads_runs.sql、domain/models.py/ports.py、repositories/threads.py、services/threads.py、api/schemas/threads.py、api/routers/threads.py；tests/services/test_threads.py、tests/api/test_threads.py。

**接口：** get_current_user；ThreadService.create/list/get；GET /v1/threads/{id}/messages 从 Saver 取可展示历史。

- [ ] 用两个用户验证列表、详情、历史查询归属；客户端伪造 user_id/内部 checkpoint 键不能越权。
- [ ] 创建应用自己的 threads 表，用户归属显式存储；Repository 不直接操作 Saver/Store 内部表。应用 DDL 有版本记录，启动期初始化串行执行。
- [ ] 认证依赖按批准方案取服务端身份；路径 ID/分页参数明确转换和边界；模型正文不含原始 AIMessage、工具调用详情、reasoning_content 或内部磁盘路径。
- [ ] 测试分页、空历史、新实例恢复、不重复消息；输出一套会话接口样例，等待确认。

## 步骤 6：后台任务提交、查询与取消（需批准队列方案）

**文件：** repositories/runs.py、services/runs.py、domain/ports.py、infra/task_queue.py、workers/settings.py/tasks.py/maintenance.py、api/schemas/runs.py、api/routers/runs.py；tests/services/test_runs.py、tests/integration/test_queue.py。

**接口：** RunService.submit/get/cancel；TaskQueue.enqueue(run_id)；worker 执行 run_task 并更新持久状态。

- [ ] 定义 queued/running/succeeded/failed/cancelled；失败保留安全 code/message、部分结果和源码保存状态；外部查询失败任务成功时 HTTP 200。
- [ ] PG 原子约束同一会话最多一个活动任务，幂等键按用户+会话隔离；相同键不同内容返回冲突，同内容返回原任务。
- [ ] ARQ 入队仅传 run_id；worker 从 PG 取归属和输入，原子领取，重复投递不重复执行。PG 持久任务行兼作投递恢复记录，入队异常保留可核查状态，不冒充已入队成功。
- [ ] 任务总超时由引擎控制，队列超时留出清理余量；取消写持久标记，worker 合作取消并完成必要备份。worker 进程消失时标记 interrupted，不盲目重跑有副作用的任务。
- [ ] 为 worker 启停复用 open_resources；防止多个进程并行 setup；实现有限频率任务对账而不是周期调用健康接口。
- [ ] 集成验证入队失败、队列重复投递、同会话并发、取消、超时、worker 中断、源码保存失败；正常返回 202 后最终查询结果与磁盘一致。

## 步骤 7：明确的长期记忆读写

**文件：** memory/namespaces.py/policy.py、services/memories.py、api/schemas/memories.py、api/routers/memories.py；tests/test_long_term_memory.py。

**接口：** 用户偏好 namespace=("users", user_id, "preferences")；MemoryService.get/put/delete；任务开始读取有限白名单偏好。

- [ ] 验证两用户隔离、跨会话读取、删除生效、非法键和值拒绝、长度和条数上限。
- [ ] 只保存用户明确要求的稳定偏好；作为低优先级参考注入，不能覆盖系统授权与 guidance。不开启自动总结写入，不新增 embedding 调用。
- [ ] 主代理将相关偏好摘要传给被委派角色，禁止默认复制整段历史；已有 AGENTS.md 和 skills 的加载方式不改变。
- [ ] 用假模型+真实测试 Store 验证实际请求上下文含偏好；确认长期存储不是仅有一个 store= 参数。

## 步骤 8：文件接口与会话删除（建议独立确认）

**文件：** api/schemas/files.py、api/routers/files.py、services/files.py、sandbox/transfers.py；扩展 threads service 和维护任务。

- [ ] 上传先验证大小、相对路径、用户归属，禁止覆盖 guidance/平台文件；活动任务期间禁止直接改动其输入工作区，采用拒绝或排队的明确策略。
- [ ] 对已存在沙箱的新上传文件也执行传输；下载使用 file_id 映射授权后的快照路径，不接受任意宿主路径。
- [ ] 删除会话先标记 deleting 阻止新任务，再取消活动任务、清云端、清会话源码、清 checkpoint、清业务记录；步骤幂等，可重试，不删除其他线程或用户级长期偏好。
- [ ] 失败时保留清理状态，不能报告已彻底删除；验证路径穿越、链接、正在写入、部分删除失败。

## 交付与验证

- 每步先写关键失败路径测试，再实施最小改动并运行相关回归；不为纯文案修改编造测试。
- 单元测试默认使用假模型/假 SDK；PG/Redis 集成测试用独立测试数据库和 Redis 前缀，不清空现有数据；真实模型/Daytona 验收单独说明成本与资源范围。
- 更新 README、环境变量示例和打包入口；Windows 异步 PG 兼容性通过实际测试验证，必要时在进程入口选择兼容事件循环，不在请求内切换。
- 不输出密钥，不上传平台代码，不改变现有备份筛选和回收政策。
- 用户逐步确认后才继续；完成一个阶段不自动 commit/push。最终交付组件清单、启动方式、测试证据和未覆盖的边界。
