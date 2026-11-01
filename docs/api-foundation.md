# API 基础层与 Agent 持久化（步骤 1–4）

## 启动

在项目根目录使用现有 Docker 数据卷启动 PostgreSQL 和 Redis：

```powershell
docker compose --env-file .env.docker up -d postgres redis
uv sync --locked --all-extras
uv run python -m scripts.serve_api --port 8000
```

请先启动 Docker Desktop。推荐上述 Python 入口：Windows 的 psycopg 异步连接需要 Selector 事件循环，该入口会选好循环。当前按单进程运行，暂不使用 `--workers` 或自动重载。

启动过程：创建 PG 池 → 打开并探测 → 用同一个池构造 Saver/Store → 分别 `await setup()` 自动建表 → Redis ping → 将 AppResources 挂到 app.state。任一步骤失败时关闭已经创建的资源；应用退出时关闭 Redis 和 PG 池。

`setup()` 需要数据库账号具有建表权限。当前只由单进程启动者执行；未来加入 worker 前再实现集中迁移或迁移锁。

## 环境变量

沿用现有 `.env`，不要提交真实密码：

```dotenv
SYC_POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/DATABASE
SYC_REDIS_URL=redis://127.0.0.1:6379/0
SYC_PG_MIN_SIZE=1
SYC_PG_MAX_SIZE=10
SYC_REDIS_MAX_CONNECTIONS=20
SYC_RESOURCE_TIMEOUT=5
```

数据库名、账号密码须与现有 `.env.docker` 和已初始化的数据卷一致；更改容器环境变量不会自动修改已有数据库账号。

API 端口默认 8000，PostgreSQL 默认 5432，Redis 默认 6379；Compose 已将数据库端口绑定到本机地址。

## 当前可用接口

新增本机单用户接口 `POST /v1/agent/tasks`，通过 `http://127.0.0.1:8000/docs` 测试：

```json
{"thread_id":"web-demo-001","message":"创建一个简单的待办事项网页，完成后执行适合该项目的检查并总结结果"}
```

请求会等待完整 Agent 执行及源码磁盘保存，可能持续数分钟。同一 thread_id 后续消息继续当前项目和历史；换一个 thread_id 创建独立会话。请求不接受 user_id、服务器路径或模型配置，本机身份固定为 local-user。

响应包含 thread_id、reply、workspace_saved、status。status=save_failed 表示模型已返回但源码备份失败，不能当作完整成功。接口不公开服务器路径和内部图状态；客户端超时后不要自动重试可能有副作用的任务。

当前接口供本机测试，无身份认证，保持绑定 127.0.0.1。真实提交会调用已配置的模型和 Daytona；健康检查和参数校验不会调用模型。

| 接口 | 验证内容 |
| --- | --- |
| `GET /healthz` | HTTP 进程能响应，返回 `{"status":"ok"}` |
| `GET /readyz` | 实际执行 PG `SELECT 1` 和 Redis `PING`；三秒内就绪返回 200，否则 503 |

健康接口是被动的，不定期调用，也不验证模型/Daytona 或全部业务功能。启动探测失败时应用不会开始接收请求。

请求日志有服务端生成的 `request_id`，响应头为 `X-Request-ID`。错误体统一为 `code/message/request_id`；原始异常文本和请求正文不回显。入参验证错误 422，HTTP 错误保留原状态码，未知异常 500。`APIModel` 禁止多余字段和隐式类型转换。

## 完整 Agent 如何使用资源

```python
from infra.resources import open_resources
from infra.async_runtime import run_async
from agent.dispatcher import run_task

async def main():
    async with open_resources() as resources:
        result = await run_task(
            "thread-001", "为现有页面添加表单校验",
            user_id="trusted-server-user",
            checkpointer=resources.checkpointer,
            store=resources.store,
        )
        print(result["reply"])

run_async(main())
```

这个示例会真实调用模型和 Daytona，只有配置完模型/沙箱凭据后才运行。`user_id` 应由可信应用上下文提供；当前尚未开放接收用户身份的任务 HTTP 接口。

主 Agent 继续加载完整的 skills、AGENTS.md、子代理、模型角色配置及中间件。Saver 使用内部 `user_id + thread_id` 派生键；每次只追加新用户消息。Store 已绑定，但不会自动提取偏好或自动实现长期记忆业务。规则文档 memory 与数据库 store 是两种不同机制。

模型与数据库都在调用方事件循环运行。同步沙箱准备、SQLite、源码保存由同一专用线程执行；锁覆盖准备、执行和保存。任务失败/取消时尽力备份后释放资源。SQLite 仍维护沙箱映射，源码仍按既有策略保存在磁盘。

checkpoint 与源码快照不是一个事务：异常时 checkpoint 可能领先最后一次成功保存的源码，不应将其理解为任意中间步骤均可安全重放。

取消和超时会等待正在执行的同步 SDK 调用结束，因此实际返回可能晚于任务超时；底层命令还有自身超时。这个保证不覆盖用户命令主动启动的后台服务。

## 数据库验收

Docker 启动后可单独执行真实数据库测试（使用假模型，不消耗模型 token）：

```powershell
$env:SYC_TEST_DATABASE="1"
uv run python -m pytest tests/test_postgres_integration.py -q
Remove-Item Env:SYC_TEST_DATABASE
```

该测试关闭并重建连接池和 Agent，验证消息与 Store 数据恢复；只清理测试生成的随机记录。默认离线测试会跳过它，不能用离线测试通过替代真实数据库验收。

## 暂未实现

已按本地测试需求补充等待式任务调用接口；身份认证、会话管理、后台任务队列、长期偏好业务、文件上传下载和删除流程仍保留给后续阶段。本轮未提交或推送代码。
