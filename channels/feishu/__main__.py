"""独立飞书入口：uv run --all-extras python -m channels.feishu。

主线程运行 SDK 自己的事件循环；业务线程使用项目的 run_async，
在兼容 Windows psycopg 的循环内管理连接池和执行 Agent。
SDK 回调只向线程安全队列投递，不等待耗时任务。
"""
import asyncio
import threading
from concurrent.futures import Future

from infra.async_runtime import run_async
from infra.logging import get_logger

from .adapter import FeishuAdapter, parse_message
from .handler import MessageQueue, consume
from .settings import FeishuSettings
from .events import build_event_handler

logger = get_logger()


class WorkerFailed(SystemExit):
    """业务故障是进程退出，不是可重试的网络异常。

    SDK 的重连捕获 Exception；SystemExit 可穿过该边界，同时 asyncio
    会将回调中的 SystemExit 传播给主线程，进入本入口的资源清理。
    """


def abort_worker(stopping: threading.Event) -> None:
    """在 SDK 线程执行；清理已开始时不再打断清理循环。"""
    if not stopping.is_set():
        raise WorkerFailed(1)


def main() -> None:
    # 不打印 ValidationError 的 input，防止误填的密钥进入终端。
    try:
        settings = FeishuSettings()
    except ValueError:
        raise SystemExit("请在 .env 配置有效的 FEISHU_APP_ID、FEISHU_APP_SECRET") from None

    sdk_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(sdk_loop)
    queue = MessageQueue(capacity=settings.queue_size)
    ready: Future = Future()
    failures: list[str] = []
    business_thread = None
    stopping = threading.Event()

    try:
        # 当前 SDK 导入 ws.client 时捕获 loop，必须晚于上面的 set_event_loop。
        import lark_oapi as lark

        adapter = FeishuAdapter(settings)

        def business_main() -> None:
            try:
                run_async(consume(queue, adapter, lambda: ready.set_result(None)))
            except BaseException as exc:
                # 不跨线程传播携带请求详情的异常对象，只报告类别。
                failures.append(type(exc).__name__)
                queue.close()
                if not ready.done():
                    ready.set_exception(RuntimeError("飞书业务资源初始化失败"))
                elif not stopping.is_set() and not sdk_loop.is_closed():
                    # 消费者意外退出时停止接收，避免消息持续排入无人处理的队列。
                    try:
                        sdk_loop.call_soon_threadsafe(abort_worker, stopping)
                    except RuntimeError:
                        # 主线程已经关闭 loop；finally 会 join 并报告 failures。
                        pass

        business_thread = threading.Thread(
            target=business_main, name="feishu-business", daemon=False,
        )
        business_thread.start()
        ready.result()  # 连接池初始化完成后，才开始接受消息。

        def on_message(event) -> None:
            message = parse_message(event, settings.app_id)
            if message is None:
                return
            # Full/closed 异常交给 SDK，事件不被误报为接收成功。
            # 入队操作同步且很短；不用 run_coroutine_threadsafe 或游离任务。
            if queue.submit(message):
                logger.info("飞书消息入队 message_id={} chat_id={}",
                            message.message_id, message.chat_id)

        handler = build_event_handler(on_message)
        client = lark.ws.Client(
            settings.app_id, settings.app_secret.get_secret_value(),
            event_handler=handler, log_level=lark.LogLevel.WARNING,
        )
        logger.info("飞书渠道启动：文本消息、群会话共享、单消费者顺序执行")
        client.start()
    except WorkerFailed:
        logger.error("飞书业务线程异常退出，停止 SDK，不进行网络重连")
    except KeyboardInterrupt:
        logger.info("收到退出信号，停止接收并等待已入队任务完成")
    finally:
        stopping.set()
        queue.close()
        # 当前 SDK 没有依赖的公共 stop API；取消本入口专属循环里的任务。
        try:
            pending = list(asyncio.all_tasks(sdk_loop))
            for task in pending:
                task.cancel()
            if pending:
                sdk_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            sdk_loop.run_until_complete(sdk_loop.shutdown_asyncgens())
        finally:
            sdk_loop.close()
            asyncio.set_event_loop(None)
            if business_thread is not None and business_thread.ident is not None:
                # 不强杀执行中的 Agent，保留原有失败备份和锁释放语义。
                # 队列里的任务也会处理完，因此关闭可能需要等待。
                business_thread.join()
    if failures:
        raise SystemExit(f"飞书业务线程异常退出：{failures[0]}")


if __name__ == "__main__":
    main()
