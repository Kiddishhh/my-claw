# 本地 Web / CLI 与飞书渠道

## 启动

在项目根目录运行（PyCharm 也可直接运行对应文件）：

```powershell
.\.venv\Scripts\python.exe scripts/serve_api.py
```

浏览器访问 http://127.0.0.1:8000 。另开终端启动 CLI：

```powershell
.\.venv\Scripts\python.exe scripts/serve_cli.py
```

CLI 命令：`/new` 新建，`/use ID` 续聊，`/history` 查看历史，`/upload 本地路径` 上传，`/delete` 确认删除，`/exit` 退出。指定初始会话：`--thread-id ID`。

网页和 CLI 共享服务器固定的 `local-user` 身份，用相同 thread_id 即可续聊；来源分别记录 `web` 和 `cli`。飞书保留自己的会话归属规则，不能通过本地接口指定飞书身份。

## 接口

| 方法与地址 | 用途 |
| --- | --- |
| POST `/v1/agent/channels/web/tasks` | 网页任务，JSON 为 thread_id/message |
| POST `/v1/agent/channels/cli/tasks` | CLI 任务，参数同上 |
| GET `/v1/agent/sessions/{thread_id}/messages` | 从 checkpoint 读取可展示历史 |
| POST `/v1/agent/sessions/{thread_id}/files?filename=example.csv` | 请求体为文件二进制，返回沙箱 path/size/workspace_saved |
| DELETE `/v1/agent/sessions/{thread_id}` | 主动删除此会话 |

原有 `/v1/agent/tasks` 保留，来源仍为 `api`。Swagger 在 `/docs`。

## 文件流程

用户选择文件 → HTTP 上传（单文件不超过 20 MiB）→ 复用或创建该线程沙箱 → 写入 `inputs/随机前缀_文件名` → 保存本地工作区快照 → 返回沙箱路径。网页把这些路径附到后续任务，Agent 自行调用文件工具或 execute 读取。CLI 上传后显示路径，在下一条需求中引用即可。

不把文件自动解压或在服务器执行；相同文件名不会覆盖已有文件。中文长文件名按 UTF-8 字节限长。日志类后缀可能增加 `.upload`，避免备份规则将用户上传文件当运行日志排除。

上传成功意味着文件已落盘；沙箱丢失后由原恢复逻辑重建。文件上传和任务运行互斥；调用超时或失败时可能已经产生部分远程操作，请先核对后再重试。

## 主动删除的范围

1. 非阻塞取得原会话锁，忙时返回 409。
2. 写入删除中标记，阻止任务恢复半删除工作区。
3. 删除远程沙箱；云端已不存在视为成功。创建结果尚未确定时保留记录供稍后重试。
4. 调用 Saver 的 `adelete_thread` 删除 checkpoint、blobs、writes。
5. 清理 Store 的 `('sessions', session_key)` 命名空间及子空间；不清空用户共享记忆。目前业务尚未在此空间自动写记忆。
6. 删除 `.sandbox-state/workspaces/{session_key}`（包括历史版本和未完成 staging），最后删除 SQLite 映射。

跨云端/PG/磁盘不是原子事务：失败时返回错误并保留删除标记，重复 DELETE 可继续完成。不要手工删标记绕过检查。空 `.lock` 文件保留，防止另一进程仍持锁时产生两把锁；没有聊天内容。Redis 当前没有会话缓存，不执行 flush。

## 当前边界

- 本机单用户版，默认只监听 127.0.0.1；没有登录鉴权，不能公开部署为多人服务。
- 网页会话 ID、标题和上传路径保存在该浏览器 localStorage，消息内容从 PostgreSQL 读取。更换浏览器不会自动列出旧会话；可在 CLI 用已有 ID 续聊。
- checkpoint 记录的是当前图状态；框架总结/裁剪消息后，历史接口不会恢复被裁剪的原文。
- 删除是不可恢复操作。新请求若再次使用已经删除的 ID，会作为新会话建立空项目；客户端删除后会切换到新 ID。
- 不增加后台任务队列；网页提交时等待完整结果，长任务运行中保持页面开启。

## 验证

`tests/test_sessions.py` 验证清理隔离、重试、锁、上传及历史；`tests/test_session_api.py` 验证渠道、严格输入、大小限制与跨站拦截。测试替换云端与模型，真实外部调用另行验收。
