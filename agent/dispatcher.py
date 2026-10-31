"""一次 Agent 任务的应用入口：沙箱复用/恢复 → 运行 → 文件落盘。

不是模型 API 包装器，不持久化聊天记录。同步 SDK 和文件锁放到专用线程中，
避免阻塞 Web 服务事件循环；外层取消后等待工作线程安全退出再释放调用方。
"""
import asyncio
from pathlib import Path
from threading import Event
from typing import Any
from langchain_core.callbacks import BaseCallbackHandler

from agent.main import PROJECT_ROOT, build_agent
from infra.agent_limits import DEFAULT_MODEL_CALLS, DEFAULT_SUBAGENT_MODEL_CALLS, DEFAULT_RECURSION_LIMIT
from infra.logging import get_logger
from infra.settings import SandboxSettings
from sandbox.manager import (sandbox_for_thread, save_session_workspace, _session_key, _sql)

logger = get_logger()


async def run_task(
    thread_id: str, message: str, *, user_id: str = "local-user",
    project_root: Path | None = None, settings: SandboxSettings | None = None,
    client: Any = None, model: Any = None,
    callbacks: list[BaseCallbackHandler] | None = None,
    max_model_calls: int = DEFAULT_MODEL_CALLS, max_subagent_model_calls: int = DEFAULT_SUBAGENT_MODEL_CALLS,
    include_state: bool = False,
) -> dict[str, Any]:
    """执行一次任务并返回文件保存状态；project_root 是可信服务器存储根，非用户参数。

每次构建新的 MemorySaver，仅恢复项目文件，不假装恢复历史消息。
执行失败时尽力备份并保留原异常；成功但备份失败时返回明确失败状态，不删除沙箱。
callbacks 用于内部验收/观测；include_state 仅供内部脚本取回图状态，默认不返回，
其中含消息对象，不应直接作为 Web JSON 响应或公开给其他用户。
"""
    if not message.strip():
        raise ValueError("任务内容不能为空")
    root = (project_root or PROJECT_ROOT).resolve()
    settings = settings or SandboxSettings()
    cancelled = Event()

    def worker() -> dict[str, Any]:
        with sandbox_for_thread(user_id, thread_id, root, settings=settings, client=client) as resources:
            backend, sandbox, _, workdir = resources
            key = _session_key(user_id, thread_id)
            agent = build_agent(backend=backend, workdir=workdir, user_id=user_id, model=model,
                                max_model_calls=max_model_calls,
                                max_subagent_model_calls=max_subagent_model_calls)
            rows = _sql(root, "SELECT dependency_warning FROM sessions WHERE session_key=?", (key,))
            warning = rows[0]["dependency_warning"] if rows else None
            task_message = message
            if warning:
                task_message += f"\n\n[应用环境诊断]\n{warning}"

            async def invoke() -> dict:
                task = asyncio.create_task(agent.ainvoke(
                    {"messages": [{"role": "user", "content": task_message}]},
                    config={"configurable": {"thread_id": thread_id}, "recursion_limit": DEFAULT_RECURSION_LIMIT,
                            "callbacks": list(callbacks or []),
                            "metadata": {"sandbox_id": sandbox.id}},
                ))
                try:
                    deadline = asyncio.get_running_loop().time() + settings.syc_sandbox_task_timeout
                    while not task.done():
                        if cancelled.is_set():
                            raise asyncio.CancelledError()
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("Agent 任务超时")
                        await asyncio.wait({task}, timeout=0.2)
                    return await task
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

            try:
                result = asyncio.run(invoke())
            except BaseException:
                try:
                    save_session_workspace(root, key, backend, workdir)
                except Exception as backup_error:
                    logger.error("任务失败后的备份也失败，保留旧版本：{}", type(backup_error).__name__)
                raise
            try:
                version = save_session_workspace(root, key, backend, workdir)
                saved, error = True, None
            except Exception as backup_error:
                version, saved, error = None, False, type(backup_error).__name__
                logger.error("任务执行完成但文件未保存，沙箱保留：{}", error)
            response = {"thread_id": thread_id, "sandbox_id": sandbox.id,
                    "reply": result["messages"][-1].content,
                    "workspace_saved": saved, "workspace_path": version,
                    "backup_error": error}
            if include_state:
                response["agent_state"] = result
            return response

    future = asyncio.create_task(asyncio.to_thread(worker))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancelled.set()
        # 不让客户端断开导致后台还在写时，下一次请求抢入同一会话。
        try:
            await asyncio.shield(future)
        except BaseException:
            pass
        raise
