"""确定性测试异步 task 互斥，不调用模型或云服务。"""
import asyncio
from types import SimpleNamespace

import pytest
from langchain.messages import ToolMessage
from middleware.task_serialization import TaskSerializationMiddleware


def request(name="task", identity="one"):
    return SimpleNamespace(tool_call={"name": name, "id": identity, "args": {"subagent_type": "data_analyst"}})


def test_task_waits_but_normal_tools_continue():
    async def run():
        middleware = TaskSerializationMiddleware()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def handler(req):
            key = req.tool_call["id"]
            order.append(key)
            if key == "first":
                entered.set()
                await release.wait()
            return ToolMessage(content="ok", tool_call_id=key)
        first = asyncio.create_task(middleware.awrap_tool_call(request(identity="first"), handler))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(middleware.awrap_tool_call(request(identity="second"), handler))
        await middleware.awrap_tool_call(request("read_file", "normal"), handler)
        await asyncio.sleep(0)
        assert order == ["first", "normal"] and not second.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert order == ["first", "normal", "second"]
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_exception_or_cancellation_releases_lock(cancel):
    async def run():
        middleware = TaskSerializationMiddleware()
        entered = asyncio.Event()
        async def broken(req):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise ValueError("test error")
        task = asyncio.create_task(middleware.awrap_tool_call(request(), broken))
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await task
        async def healthy(req):
            return ToolMessage(content="recovered", tool_call_id="two")
        result = await asyncio.wait_for(middleware.awrap_tool_call(request(identity="two"), healthy), 1)
        assert result.content == "recovered"
    asyncio.run(run())


def test_instances_do_not_block_each_other_and_waiter_can_cancel():
    async def run():
        first, other = TaskSerializationMiddleware(), TaskSerializationMiddleware()
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(req):
            entered.set()
            await release.wait()
            return ToolMessage(content="done", tool_call_id="one")
        active = asyncio.create_task(first.awrap_tool_call(request(), held))
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(first.awrap_tool_call(request(), held))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        async def immediate(req):
            return ToolMessage(content="independent", tool_call_id="other")
        result = await asyncio.wait_for(other.awrap_tool_call(request(), immediate), 1)
        assert result.content == "independent" and not active.done()
        release.set()
        await active
    asyncio.run(run())
