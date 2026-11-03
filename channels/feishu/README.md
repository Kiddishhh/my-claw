# 飞书自建应用接入

入口：在项目根目录运行 `uv run --all-extras python -m channels.feishu`。
只启动一个该应用的渠道进程。此版本不是多副本队列系统。

PyCharm 直接运行：打开 `scripts/serve_feishu.py`，选择项目 `.venv/Scripts/python.exe`
解释器，右键 Run 或点击运行，不需要额外参数。该文件的 `if __name__ == "__main__"`
会调用原有正式入口，并自动把工作目录切换到项目根目录。
不要同时运行模块入口和这个文件，否则同应用的长连接会分摊消息。

## 平台配置

1. 开启机器人能力，配置群 @ 消息接收和机器人发送权限。
2. 订阅 `im.message.receive_v1`，选择「使用长连接接收事件」。
3. 将 `.env.example` 的配置加入项目根目录 `.env`，填写真实 App ID/Secret。
4. 保持数据库、Redis 运行，模型和沙箱配置沿用现有项目设置。
5. 启动进程后完成平台长连接配置，发布生效，并将机器人加入目标群。

不需要公网 HTTP 回调接口、Verification Token、成员登录或机器人 open_id。
单聊需确认单聊接收权限和应用可用范围已生效。

## 每块代码的职责

| 文件 | 功能 |
| --- | --- |
| settings.py | 读取飞书凭据和队列容量，不改变引擎配置 |
| adapter.py | 严格解析文本事件、生成群共享归属、拆分长回复并回复原消息 |
| handler.py | 有界内存排队和去重，调用现有 run_task，发送失败不重跑任务 |
| events.py | 正式服务与验收脚本共用事件注册；进入单聊事件只确认，不调用 Agent |
| __main__.py | SDK/业务线程启动，资源就绪后收消息，退出时等待队列处理完 |

收到事件 → 文本解析 → 内存去重/入队 → 单消费者调用 run_task → 回复原消息。
现有 run_task 继续负责 skills、memory、subagent、中间件组装，checkpoint、
沙箱复用/恢复及项目文件落盘。渠道不自行 build_agent，也不新增数据库表。

会话归属为应用+租户+chat_id，传给引擎的 thread_id 是 chat_id。
发送者 open_id 仅标记发言来源；同群不同成员共享同一会话和项目。

## 接收规则和边界

- 只处理用户文本；图片、文件、语音及机器人消息当前忽略。
- 当前群 @ 权限由飞书过滤推送。若申请群全量权限，需要另行增加触发规则。
- 一个消费者对所有群顺序执行，避免共享工作区写入竞争。
- 内存队列和最近 10000 条消息的去重记录不持久化，强制退出会丢失待处理任务。
- checkpoint/项目文件仍由现有引擎持久化；飞书回复只发文字，不上传项目源码。
- 发送失败只记录错误类别，不自动重跑 Agent，不保证回复可靠送达。
- Ctrl+C 停止接收后会等待所有已接受任务完成，可能需要较长时间。
- 入口透传 `channel=feishu`，子代理继承渠道；角色单独记录为 `agent_role`。
- 网络故障仍由 SDK 重连；业务消费者异常退出则结束服务，不继续重连接收。

## 验证

离线：`.venv/Scripts/python -m pytest tests/test_feishu.py -q`。
这些测试替代外部服务，不证明飞书、模型或沙箱云端已经连通。

真实验收：同群两位成员先后 @ 机器人修改同一项目；另一群应保持隔离。
确认能收到最终回复、checkpoint 和项目文件正常保存。

### 先单独验证渠道收发

不知道 chat_id 时，在项目根目录运行：

```powershell
uv run --all-extras python -m scripts.verify_feishu_send --listen
```

请关闭其他该应用的长连接进程。在飞书单聊发送「渠道自检」，或在群内
@机器人发送「渠道自检」。脚本只接收一条文本并打印真实 chat_id，
不调用 Agent、数据库或沙箱，默认等 60 秒后退出。

取得 oc_ 开头的真实 ID 后运行（替换示例值）：

```powershell
uv run --all-extras python -m scripts.verify_feishu_send oc_真实会话ID
```

脚本发送一条「DevMate 自检消息」，只有飞书返回成功和 message_id 才报告成功。
返回非零退出码代表失败。接口成功还需要你在对应会话确认可见；
这只验证渠道发送，不代表 Agent 端到端已经通过。
