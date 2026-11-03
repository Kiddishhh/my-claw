# CLI / Web 与会话清理实施计划

> 使用 executing-plans 在当前工作区实施；用户已确认简单网页，并追加文件上传。

**目标**：复用完整 Agent，提供本机单用户 CLI、网页聊天、文件上传和会话删除。
**架构**：渠道调用 dispatcher；统一 session service 负责持锁上传、历史读取、删除。保留已有 API/飞书行为。不增加队列、登录系统或新数据库。
**技术**：FastAPI / Pydantic v2 / PostgreSQL Saver + Store / SQLite / Daytona / 原生 HTML CSS JS。

## 约束与决策
- Web/CLI 共享本机 local-user 身份，channel 分别为 web/cli；旧 api 来源保持不变。
- 不接收外部 user_id 或服务器文件路径；默认监听 127.0.0.1。
- 网页会话列表只在浏览器保存 ID/标题，历史从 checkpoint 读取。
- 上传单文件最多 20 MiB，服务端生成唯一安全文件名，放入沙箱 inputs/ 并立即备份；不自动解压或执行。
- 删除获取原会话非阻塞锁，忙时 409；标记删除中，远程删除、checkpoint 和会话范围 Store 清理、本地快照删除完成后才删除映射。失败保留标记供重试。
- 不删除用户级共享长期记忆，不 flush Redis，不改飞书身份映射，不清空真实现有数据做测试。
- 服务端对新网页写操作校验 Origin 与 Host，页面显示文本使用 textContent。

## 步骤
- [x] 新增 session service 与 sandbox 公共生命周期接口；测试锁冲突、失败重试、路径边界、上传及 Saver 删除。
- [x] 新增严格模型和注入路由：web/cli tasks，sessions/{id}/messages、files、DELETE；保留 /v1/agent/tasks。
- [x] 新增 CLI 入口与静态网页，提供会话切换、历史、删除确认、文件上传与错误提示；静态资源纳入打包。
- [x] 聚焦测试、全量 pytest、unittest；真实基础服务检查和浏览器交互验收。文档记录限制，不自动 commit/push。

## 实施记录
- 在当前用户工作区继续实施，不新建 worktree、不提交既有工作区改动。
- 原始二进制流上传替代 multipart，逐块检查 20 MiB 上限，不增加上传解析依赖。
- 独立 review 后修复中文文件名字节限制、创建未确定时保留删除映射。
- 全量 221 passed / 2 skipped / 16 subtests；unittest 6 passed。最后边界修复相关测试 9 passed / 1 skipped（Windows symlink 权限）。
- 真实服务 /readyz 就绪；Edge 验证上传路径传递、聊天、历史恢复、删除及手机无横向溢出；使用 HTTP 测试响应隔离模型和真实沙箱。
- 真实 PostgreSQL 随机线程删除集成测试 1 passed：checkpoint/Store 清理、重复删除；CLI 实际读取历史与正常退出通过。未删除任何已有会话，未进行真实模型/Daytona 新任务验收。

## Review focus
上传大小/路径；不同会话隔离；删除与运行竞争；部分删除重试；浏览器恢复和网络失败不能误报成功。
