"""实例级 task 串行化：排队执行整个子代理，不删除模型的委派意图。"""
import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from infra.logging import get_logger

logger = get_logger()


class TaskSerializationMiddleware(AgentMiddleware):
    """同一个 Agent 实例同时最多执行一个 task。

    当前入口每次会话执行独立构建 Agent；外部会话文件锁仍负责跨请求互斥。
    不把本实例跨事件循环共享，不把此锁误认为按 thread_id 分组或分布式锁。
    只保护 task，不保护主代理普通写入；退出调用也不代表远程后台进程已停止。
    """

    def __init__(self) -> None:
        self._task_lock = asyncio.Lock()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """普通工具直接放行；委派持锁运行，异常及取消保持向外传播。"""
        if request.tool_call["name"] != "task":
            return await handler(request)
        identity = request.tool_call["id"]
        role = request.tool_call.get("args", {}).get("subagent_type", "unknown")
        queued = perf_counter()
        logger.info("委派排队：role={} call={}", role, identity)
        # 锁覆盖 await handler 的整个生命周期，await 让出事件循环但不释放锁。
        async with self._task_lock:
            started = perf_counter()
            logger.info("委派开始：role={} call={} 等待={:.0f} ms", role, identity, (started - queued) * 1000)
            try:
                return await handler(request)
            finally:
                # 这里只声明调用退出，不把异常或取消误报为成功。
                logger.info("委派结束：role={} call={} 耗时={:.0f} ms", role, identity, (perf_counter() - started) * 1000)
