"""显式开启的真实 PG/Redis 验收：无真实模型/云端沙箱调用。

启动 Docker 后设置 SYC_TEST_DATABASE=1 再运行本文件。只清理本测试的随机键，
不会清空现有表；首次 setup 会创建 LangGraph 所需的表。
"""
import os
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from agent.main import build_agent
from infra.async_runtime import run_async
from infra.resources import open_resources
from tests.test_development_registration import CapturingModel
from tests.test_remote_agent import TestSandbox


@pytest.mark.skipif(os.getenv("SYC_TEST_DATABASE") != "1", reason="需要显式开启真实数据库测试")
def test_persistence_survives_pool_and_graph_recreation(tmp_path):
    thread = "integration-" + uuid4().hex
    namespace = ("integration", thread)
    config = {"configurable": {"thread_id": thread}}

    async def run():
        try:
            # 关闭并重新创建池，比仅重建 graph 更接近服务重启后的恢复。
            for prompt in ["first", "second"]:
                async with open_resources() as resources:
                    assert resources.checkpointer.conn is resources.pg_pool
                    assert resources.store.conn is resources.pg_pool
                    agent = build_agent(
                        backend=TestSandbox(root_dir=tmp_path), workdir="/workspace",
                        model=CapturingModel(replies=[AIMessage(content="ok")]),
                        checkpointer=resources.checkpointer, store=resources.store,
                    )
                    state = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}, config)
                    if prompt == "first":
                        await resources.store.aput(namespace, "preference", {"language": "zh"})
                    else:
                        item = await resources.store.aget(namespace, "preference")
                        assert item.value == {"language": "zh"}
                        assert [m.content for m in state["messages"] if m.type == "human"] == ["first", "second"]
        finally:
            async with open_resources() as resources:
                await resources.checkpointer.adelete_thread(thread)
                await resources.store.adelete(namespace, "preference")
    run_async(run())


@pytest.mark.skipif(os.getenv("SYC_TEST_DATABASE") != "1", reason="需要显式开启真实数据库测试")
def test_session_delete_clears_own_checkpoint_and_store(tmp_path):
    """只写/删测试随机线程；不创建 Daytona 资源，不触碰现有用户会话。"""
    from agent.sessions import delete_session, read_history
    from sandbox.manager import session_key
    from langgraph.graph import StateGraph, MessagesState, START, END
    thread = 'delete-check-' + uuid4().hex
    key = session_key('local-user', thread)
    namespace = ('sessions', key, 'test')
    async def run():
        async with open_resources() as resources:
            try:
                graph = StateGraph(MessagesState)
                graph.add_node('answer', lambda state: {'messages': [AIMessage(content='ok')]})
                graph.add_edge(START, 'answer')
                graph.add_edge('answer', END)
                agent = graph.compile(checkpointer=resources.checkpointer)
                await agent.ainvoke({'messages':[{'role':'user','content':'test'}]}, {'configurable':{'thread_id':key}})
                await resources.store.aput(namespace,'value',{'test':True})
                assert len(await read_history(thread,checkpointer=resources.checkpointer)) == 2
                await delete_session(thread,checkpointer=resources.checkpointer,store=resources.store,project_root=tmp_path)
                assert await read_history(thread,checkpointer=resources.checkpointer) == []
                assert await resources.store.aget(namespace,'value') is None
                await delete_session(thread,checkpointer=resources.checkpointer,store=resources.store,project_root=tmp_path)
            finally:
                await resources.checkpointer.adelete_thread(key)
                await resources.store.adelete(namespace,'value')
    run_async(run())
