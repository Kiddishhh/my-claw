# 按线程保存及恢复工作区

## 当前入口

`agent.dispatcher.run_task(thread_id, message, user_id=...)` 是一次任务的应用入口。
CLI `python -m scripts.devmate_with_mw --thread-id web-001 --task "创建首页"`
也调用它。入口不创建 Web API，不持久化对话；每次构建 Agent 的 MemorySaver
是独立的。恢复的是项目文件，不是聊天记录或运行中的进程。

同步 SDK、会话锁和磁盘 I/O 在后台线程执行；任务超时或调用方取消后，取消 Agent，
尽力保存，再结束会话上下文。取消工具等待不保证远程后台命令已终止；扫描前后
清单不一致会拒绝保存。需要持续写业务数据的服务，应使用独立持久存储。

## 存储位置

默认在服务器项目根目录下：

```
.sandbox-state/
  sessions.sqlite3
  workspaces/<sha256(user_id, thread_id)>/
    versions/<version_uuid>/
      project/            # 源码、测试、配置和静态素材
      manifest.json       # 相对路径、大小、SHA256
    staging/              # 下载中临时目录
  guidance/<content_hash>/ # 仅 Markdown 的服务器指导副本
```

`--project-root` 指定可信服务器状态存储根，不是要自动上传的本地源码目录。
生产环境请把该位置放在持久磁盘；服务器管理员可读写，用户不能提交任意存储路径。
SQLite 原地增加 snapshot_path/saved_at/backup_status/backup_error，保留旧映射。

## 保存与恢复

1. 用户与线程计算存储标识，同会话文件锁覆盖准备、执行、保存全过程。
2. 默认新建空的 `workspace`，不上传 my-claw 平台。镜像需要 Node.js、npm、Python；
   pnpm/yarn 项目还需要相应 Corepack 环境，uv 项目需要 uv。
3. 正常结束保存；执行失败尽力保存并抛出原异常。保存失败时旧版本不变，返回
   `workspace_saved=false`；不会主动删除沙箱。
4. 保存扫描全部纳入范围的文件，逐文件下载与哈希校验，再次扫描确认未变化。
   完整版本发布后才更新 SQLite；当前及上一版本保留。失败或进程崩溃遗留的
   staging/未引用版本不会当作可恢复版本，需运维清理，不能直接删除整个状态目录。
5. 明确 404 且存在有效备份时新建沙箱并上传；文件全部恢复且基础解释器可用后标记 ready。依赖命令非零退出只记录 dependency_warning，交给 Agent 修复；连接异常仍阻断。
   网络超时/鉴权失败不当作 404，无有效备份也不冒充空项目恢复。
6. 仅新建/恢复时尝试按项目锁文件安装依赖；复用已有沙箱时不再强制重装。依赖安装脚本只在沙箱内运行。
   开发服务器、预览链接及进程须重新创建，本次未实现自动预览服务管理。

只保存用于重建项目的源码、测试、配置、锁文件和静态资源。
任意层级的 node_modules、.venv、.git、Python 缓存等不保存；项目根目录的
dist、build、.next、out、output、outputs、logs、coverage、test-results、playwright-report
等运行输出不保存。src/build 等内部源码目录仍然保留；.log、.pyc 等文件及根目录
pytest_result.txt、junit.xml 等报告不保存。自定义运行报告统一写入 outputs/ 或 logs/，
任意命名的文件无法仅凭内容可靠判断是源码还是运行结果。
旧全量备份恢复时同样应用此规则，不重新上传历史运行产物；历史磁盘版本不批量删除。
恢复后重新安装依赖、执行构建，运行结果不作为恢复依据。
没有 100 MiB/10000 项上限；磁盘不足、传输失败或清单截断时整次保存失败，旧版本不变。
空目录一并保存；项目内符号链接和普通权限位保存在 manifest.json，恢复到沙箱时重建。
服务器磁盘不创建远程符号链接，也不将 Linux 可执行权限施加到本地文件。
越出项目的链接、特殊设备文件、路径穿越和 Windows 不支持的名称会明确失败，不静默遗漏。
保存和下载均由服务器程序执行，不运行备份内容；这不是 Windows 的禁止执行策略。
项目配置（包括项目自身的 .env）仍会保存；平台密钥不会注入沙箱或此备份。

## 清理边界

空闲清理先保存再删远程沙箱，保留 SQLite 行和磁盘版本，将状态改为 archived。
备份失败保留环境并重试。平台自动停止/自动删除和进程强杀不能保证触发应用备份，
意外删除最多恢复到最近一次成功保存；按实际要求配置平台 TTL。
`cleanup_pending` 的初始化失败资源不覆盖旧快照；明确丢失且无备份时拒绝恢复。

前端删除会话接口和聊天 checkpoint 清理本阶段不实现，不要将空闲回收等同于用户删除。

## 指导文件与旧验收

服务器 AGENTS.md 与 Markdown skills 发布到 `/guidance/` 只读虚拟路由；
主、子 Agent 都禁止修改该路由，渐进式披露由 DeepAgents 提供。
execute 仍走 Daytona；该路由不是沙箱操作系统挂载，技能附带脚本不会自动上传。

`SYC_SANDBOX_SEED_PLATFORM=true` 仅用于平台自身开发验收，明确选择上传平台源码。
旧 `tests.verify_daytona` 显式开启此模式；不能把其结果当成新 Web 工作区恢复流程的验收。

离线测试覆盖备份、删除语义、损坏检测、扫描、回收保护、重建、取消和只读规则。
真实 Daytona 依赖安装及预览仍需在目标镜像上单独验收。
