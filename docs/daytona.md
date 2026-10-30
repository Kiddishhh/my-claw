# Daytona 默认开发环境

## 启动

本机 `.env` 配置 `DAYTONA_API_KEY`；原 SYC 模型和 LANGSMITH 配置继续有效。
管理器使用本机 SDK 管理凭据，不把配置对象或宿主环境变量注入沙箱。

```powershell
uv sync --locked --all-extras
uv run --extra daytona python -m scripts.devmate_with_mw --thread-id issue-001 --task "检查项目并总结，不修改文件"
```

同一 user-id/thread-id 复用远程文件；首次上传目录由 manager.py 的 SOURCE_DIRS/EXTRA_FILES 声明。
每次取得环境后 `uv sync --locked --extra daytona`，修复缺失的虚拟环境并同步远程依赖声明。
不自动同步后续本地源码修改，以免覆盖远程工作成果。不会自动下载产物，删除前应先导出。
当前主 Agent 启用 task 委派，主子代理共享远程 backend。主代理不直接挂载表格工具；`run_pytest` 只留在显式本地验证入口。

## 管理函数与运行位置

- `_collect_project_files`：本机收集指定目录、检查路径和密钥，输出 `(远程绝对路径, bytes)`。
- `seed_project_into_sandbox`：查询真实工作目录，创建父目录、检查上传结果、同步依赖。
- `get_or_create_sandbox_backend`：获取四元组，只保护准备阶段；实际任务使用下一个上下文。
- `sandbox_for_thread`：锁覆盖准备、构建和整个 Agent 调用，最后更新空闲时间。
- `cleanup_idle_sandboxes`：尝试获取同会话锁，活跃会话跳过；删除确认后才清除映射。

模型、搜索请求、LangSmith 上传和管理 SDK 在本机运行；文件操作和 execute 在远程沙箱运行。
聊天 checkpoint 仍是图实例内存，不随 SQLite 沙箱映射恢复；跨进程只保留远程文件。
SQLite 在 `.sandbox-state/`，请勿随意删除。标签辅助找回创建请求中断后未登记 ID 的资源。
不同用户必须由可信调用方提供身份；CLI 的 user-id 是本地开发标识，不是鉴权系统。

## 回收与配置

| 本机环境变量 | 默认 | 含义 |
| --- | --- | --- |
| DAYTONA_API_URL | https://app.daytona.io/api | SDK 管理端点 |
| SYC_SANDBOX_IDLE_SECONDS | 3600 | 任务结束后空闲多少秒允许回收 |
| SYC_SANDBOX_CLEANUP_SECONDS | 60 | 应用回收扫描间隔 |
| SYC_SANDBOX_COMMAND_TIMEOUT | 300 | 后端命令默认等待秒数 |
| SYC_SANDBOX_TASK_TIMEOUT | 600 | Agent 调用等待上限，不包含环境准备 |
| SYC_SANDBOX_AUTO_STOP_MINUTES | 30 | 平台自动停止间隔 |
| SYC_SANDBOX_AUTO_DELETE_MINUTES | 1440 | 持续停止多久后平台自动删除 |

主入口存活期间运行扫描线程。若程序不运行时也要持续按本地空闲时间回收：

```powershell
uv run --extra daytona python -m scripts.sandbox_cleanup --watch
```

不加 --watch 仅执行一轮，可交给 Windows 任务计划程序。代码不会自行安装开机服务。
主进程退出后平台自动停止/删除继续生效，平台规则独立于本地锁，不应设得短于正常任务。
初始化/停止失败标记 cleanup_pending，回收器重试；网络/鉴权错误不当作“没有沙箱”。
正常退出不删除沙箱；模型超限/本地超时/任务异常时先尝试停止沙箱。
本地取消不等于远程所有进程必定停止，停止失败会禁止复用并标记回收。

## 密钥与边界

不上传 `.env*`、`.git`、`.venv`、日志和凭据文件。允许 `.agents/skills`，限制文件类型、大小和总量。
上传前检测本机已配置的凭据值以及常见 API Key/私钥格式，错误只说明类别，不打印秘密。
只向沙箱注入 APP_ENV=test、PYTHONUNBUFFERED=1。自定义镜像和任务文本也不应包含密钥。
扫描不是秘密识别的绝对保证，未知格式、编码后的秘密仍需人工审核上传清单。
当前实现信任上传的源码与锁文件；包安装会运行远程代码，不能用于未经审查的依赖来源。
单主机多进程共享状态目录可互斥；多服务器部署需要共享数据库和分布式锁。

## 验证

`python -m pytest tests/test_sandbox_manager.py tests/test_remote_agent.py -q` 不创建云端资源。
完整回归：`python -m pytest -q`；LangSmith tracing 继续遵循环境配置。
真实验收：`python -m tests.verify_daytona`，会创建临时会话并在结束后删除其沙箱。

## 远程 Agent 组件与委派

`build_agent(backend=..., workdir=...)` 返回已编译的图：主代理加载开发技能和 AGENTS.md；
六个专职子代理与只读 general-purpose 共用同一个 backend/workdir。
每个子代理显式配置 memory、skills、角色请求上下文、工具审计、Token 计量和模型调用上限。
主代理默认最多 12 次模型调用，每次子代理委派默认最多 12 次；这不是全局 Token 预算。
可通过 subagent_models 按角色注入模型，通过 max_subagent_model_calls 设置子代理上限。

- planner / researcher / reviewer / general-purpose：只读文件工具，无 execute、write_file、edit_file。
- coder / tester：远程文件工具与 execute，tester 在沙箱虚拟环境运行 pytest。
- data_analyst：远程文件工具与 execute，专属分析技能指导在同一个远程 Python 进程调用现有表格库。
- reviewer：通过 ReviewResult 返回结构化审查结果。

开发默认顺序 coder → tester → reviewer，由 AGENTS.md 指导主代理按证据选择下一次 task；
不添加固定工作流或删除 task 的中间件；TaskSerializationMiddleware 对整个委派执行加锁，保留每个调用。
每轮委派数量与角色顺序仍是提示词约束；同一 Agent 实例内的 task 互斥是执行层保证。
外部会话锁仍覆盖整个主子代理执行期；图内部 task 另由异步锁串行化，普通工具不受这把锁保护。
角色工具白名单不是同一沙箱内的 OS 权限隔离，execute 可运行任意受授权项目代码。

已有沙箱不会自动覆盖源码、规则和技能。要使用本次新规则，建议以新的 thread-id 创建会话；
若继续旧会话，需要显式更新其远程 AGENTS.md、skills 和所需源码。聊天 checkpoint 仍在内存中。
上传清单不包含 data/inputs/；远程分析前需另行准备经过授权的输入文件。

DeepSeek 官方接口的思考模式不接受 ToolStrategy 的强制工具选择。
构建时仅复制 reviewer 模型并设置 thinking=disabled，保留主代理及其他角色的思考配置；
其他提供商不自动改写。reviewer 仍通过 ReviewResult 做结构校验。

## 串行化与数据分析验收

TaskSerializationMiddleware 每次构建 Agent 时独立实例化；只支持同一事件循环的异步调用。
不要把同一个 Agent 实例跨 asyncio.run 重用，也不要把它理解为跨服务分布式锁。
等待、开始和结束日志包含角色、调用 ID、等待时间与执行时间。取消或异常会释放锁，
但远程后台进程仍需由沙箱生命周期管理停止。锁不保证 coder/tester 的业务顺序。

```powershell
# 离线：强制同时提出多个委派，验证互斥、取消释放、独立实例与真实图注册
uv run --extra daytona python -m pytest tests/test_task_serialization.py tests/test_remote_agent.py tests/test_analysis_verification.py -q
# 真实模型 + 临时 Daytona：自动生成虚构 CSV，分析、绘图、导出，再交 reviewer
uv run --extra daytona python -m tests.verify_daytona --scenario analysis
```

模拟输入：按商品统计已付款金额，生成柱状图和 CSV/XLSX/Markdown 报告。
程序独立断言 A=0.30 元、B=12.50 元、计入 3 条记录、原始 CSV 未变及产物存在。
验收还记录 task 时间区间，检查没有重叠；真实模型自然顺序委派不能单独证明锁，
因此并发互斥由离线强制竞争测试证明。最终下载产物、保存时间线，并删除临时沙箱。
记录位于 logs/daytona-verification/<run-id>/：report.json、task-timeline.json、
analysis-summary.json 和 artifacts/。只上传程序生成的虚构数据，不读取用户业务数据。

数据分析验收脚本每次子代理委派默认最多 16 轮，可通过 --max-subagent-model-calls 调整；
主代理最多 12 轮，总调用等待上限 480 秒。正常开发入口仍使用默认子代理 12 轮限制。

该场景是受控集成验收：预置 run_analysis 计算入口，由真实模型委派执行和审查；
它验证模型工具调用、远程计算及串行化，不用于证明模型自主编写分析脚本的可靠性。
