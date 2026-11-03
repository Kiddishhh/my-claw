"""飞书收/发链路自检，不初始化数据库、不调用模型或沙箱。

获取真实 chat_id（监听一次，默认 60 秒）：
    uv run --all-extras python -m scripts.verify_feishu_send --listen
发送一条自检消息：
    uv run --all-extras python -m scripts.verify_feishu_send oc_真实会话ID

仅验证渠道；完整 Agent 端到端仍需通过正式接收端测试。
请勿同时启动本监听器和正式接收端，同应用多个长连接会分摊事件。
"""
import argparse
import asyncio
import re

from channels.feishu.adapter import FeishuAdapter, FeishuAPIError, parse_message
from channels.feishu.settings import FeishuSettings
from channels.feishu.events import build_event_handler
from infra.async_runtime import run_async


async def send_check(settings: FeishuSettings, chat_id: str) -> int:
    print(f"发送自检消息：chat_id={chat_id}", flush=True)
    message_id = await FeishuAdapter(settings).send_to_chat(
        chat_id,
        "DevMate 自检消息：你若在飞书看到这条，说明发送链路已通 ✅",
    )
    print(f"飞书 API 已确认发送成功：message_id={message_id}", flush=True)
    print("请在对应会话确认收到。此测试未调用 Agent、模型或沙箱。", flush=True)
    return 0


def listen_once(settings: FeishuSettings, timeout: int) -> int:
    """只捕获首条用户文本事件，打印 ID 后退出；不提交 Agent 任务。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    captured = []
    timed_out = False

    def expire():
        nonlocal timed_out
        timed_out = True
        loop.stop()

    try:
        # SDK 捕获 loop，必须在 set_event_loop 后才导入。
        import lark_oapi as lark

        def on_message(event):
            message = parse_message(event, settings.app_id)
            if message is not None and not captured:
                captured.append(message)
                print(f"收到真实消息：chat_id={message.chat_id} "
                      f"message_id={message.message_id}", flush=True)
                # 给 SDK 发送事件 ACK 留出一次调度机会，再结束专属循环。
                loop.call_later(0.2, loop.stop)

        handler = build_event_handler(on_message)
        client = lark.ws.Client(
            settings.app_id, settings.app_secret.get_secret_value(),
            event_handler=handler, log_level=lark.LogLevel.WARNING,
            # 这是一次性验收，不在凭据/网络故障时无限重连。
            auto_reconnect=False,
        )
        loop.call_later(timeout, expire)
        print(f"开始监听（最多 {timeout} 秒），请在目标群 @机器人发送：渠道自检。"
              "本模式不会调用模型。", flush=True)
        try:
            client.start()
        except RuntimeError:
            # SDK start 使用 run_until_complete；主动 stop 会产生此异常。
            # 只吞掉本脚本明确请求的停止，不掩盖普通运行错误。
            if not captured and not timed_out:
                raise
        if captured:
            print("接收链路已验证。使用上述 chat_id 运行发送自检。", flush=True)
            return 0
        print("未收到符合条件的文本事件。请核查事件订阅、权限、发布和群成员配置。")
        return 1
    finally:
        pending = list(asyncio.all_tasks(loop))
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id", nargs="?", help="真实飞书会话 ID，形如 oc_...")
    parser.add_argument("--listen", action="store_true", help="只监听一次并打印 chat_id")
    parser.add_argument("--timeout", type=int, default=60, help="监听等待秒数，1~300")
    args = parser.parse_args(argv)
    if args.listen:
        if args.chat_id or not 1 <= args.timeout <= 300:
            parser.error("监听模式不需要 chat_id，timeout 必须在 1~300 秒内")
    elif not args.chat_id or not re.fullmatch(r"oc_[A-Za-z0-9]+", args.chat_id):
        parser.error("需要真实 oc_ 开头的 chat_id；不知道 ID 时请先使用 --listen")
    try:
        settings = FeishuSettings()
    except ValueError:
        print("请在项目根目录 .env 填写有效 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return 2
    try:
        return (listen_once(settings, args.timeout) if args.listen
                else run_async(send_check(settings, args.chat_id)))
    except FeishuAPIError as exc:
        print(f"发送失败：code={exc.code} log_id={exc.log_id}")
        return 1
    except KeyboardInterrupt:
        print("自检已取消。")
        return 130
    except Exception as exc:
        # 不回显网络异常原文，避免其中携带凭据或请求正文。
        print(f"自检失败：{type(exc).__name__}；请检查网络和应用配置。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
