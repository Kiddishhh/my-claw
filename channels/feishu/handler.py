"""轻量业务层：内存排队/去重 → 现有 run_task → 飞书回复。

不新增数据库表。不直接 build_agent，保留原有组件、沙箱恢复和落盘。
队列及去重在重启后清空，已有 checkpoint 和项目文件仍由引擎持久化。
"""
import asyncio
from collections import OrderedDict
from queue import Empty, Queue
from threading import Lock

from agent.result import extract_reply_text

from infra.logging import get_logger

from .adapter import FeishuMessage

logger = get_logger()


class MessageQueue:
    """线程安全的有界队列：SDK 线程写入，业务线程顺序读取。"""
    def __init__(self, capacity: int = 50, dedup_capacity: int = 10_000):
        if capacity < 1 or dedup_capacity <= capacity:
            raise ValueError("去重容量必须大于正数队列容量")
        self.queue: Queue[FeishuMessage] = Queue(maxsize=capacity)
        self.seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self.dedup_capacity = dedup_capacity
        self.lock = Lock()
        self.closed = False

    def submit(self, message: FeishuMessage) -> bool:
        """返回 False 表示重复；满队列抛 Full，且不提前标记已接收。"""
        key = (message.app_id, message.tenant_key, message.message_id)
        with self.lock:
            if self.closed:
                raise RuntimeError("feishu_queue_closed")
            if key in self.seen:
                return False
            self.queue.put_nowait(message)
            self.seen[key] = None
            if len(self.seen) > self.dedup_capacity:
                self.seen.popitem(last=False)
            return True

    def get_nowait(self) -> FeishuMessage:
        return self.queue.get_nowait()

    def close(self) -> None:
        """停止接受新消息；消费者处理完已接受的任务后退出。"""
        with self.lock:
            self.closed = True

    @property
    def finished(self) -> bool:
        with self.lock:
            return self.closed and self.queue.empty()


async def handle_message(message, resources, adapter, *, runner=None) -> None:
    """一次任务的唯一执行点；发送失败绝不回到 run_task 重做。"""
    if runner is None:
        from agent.dispatcher import run_task
        runner = run_task
    try:
        if len(message.text) > 50_000:
            reply = "消息过长，请缩短任务描述后重新发送。"
        else:
            result = await runner(
                thread_id=message.chat_id, user_id=message.owner_id, channel="feishu",
                message=f"[飞书发送者：{message.actor_id}]\n{message.text}",
                checkpointer=resources.checkpointer, store=resources.store,
            )
            reply = extract_reply_text(result.get("reply")).strip() or "本次未返回可展示的文字回复。"
            if not result.get("workspace_saved", False):
                reply += "\n\n提示：本次工作区未成功保存，沙箱清理后可能无法恢复本次修改。"
    except Exception as exc:
        logger.error("飞书任务失败 message_id={} type={}",
                     message.message_id, type(exc).__name__)
        reply = "本次任务处理失败，可能已完成部分操作。请让我先检查当前项目再继续。"
    try:
        await adapter.send(message, reply)
        logger.info("飞书任务已回复 message_id={} chat_id={}",
                    message.message_id, message.chat_id)
    except Exception as exc:
        logger.error("飞书回复失败，未重跑 Agent message_id={} type={} code={} log_id={}",
                     message.message_id, type(exc).__name__,
                     getattr(exc, "code", None), getattr(exc, "log_id", None))


async def consume(queue, adapter, ready, *, resource_factory=None,
                  handler=handle_message) -> None:
    """一个消费者全局串行，资源池在本循环创建并在退出时关闭。

    工厂和 handler 可注入，离线测试无需真实数据库、模型或沙箱。
    轮询间隔 0.1 秒，仅用于等待内存消息，不是健康检查。
    """
    if resource_factory is None:
        from infra.resources import open_resources
        resource_factory = open_resources
    async with resource_factory() as resources:
        ready()
        while not queue.finished:
            try:
                message = queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.1)
                continue
            await handler(message, resources, adapter)
