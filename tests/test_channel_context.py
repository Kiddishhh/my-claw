"""渠道/角色分离，以及飞书生命周期异常边界；全部离线。"""
import asyncio
from threading import Event
from unittest.mock import AsyncMock, patch

import pytest

from infra.async_runtime import run_async


def test_reply_extraction_does_not_claim_success_for_empty_content():
    from agent.result import extract_reply_text
    assert extract_reply_text(None) == ""
    assert extract_reply_text([{"type": "reasoning", "text": "private"}]) == ""
    assert extract_reply_text([{"type": "text", "text": "answer"}]) == "answer"
    assert extract_reply_text("answer") == "answer"


def test_public_session_key_preserves_historical_encoding():
    import hashlib
    import json
    from sandbox.manager import session_key
    # 原来的 user_id/thread_id 编码约定保持不变，不能混入 channel 或角色。
    expected = hashlib.sha256(json.dumps(["alice", "existing-thread"]).encode()).hexdigest()
    assert session_key("alice", "existing-thread") == expected


@pytest.mark.parametrize("channel", ["feishu", "api", "cli"])
def test_main_and_children_keep_channel_separate_from_role(tmp_path, monkeypatch, channel):
    from agent import main
    from tests.test_remote_agent import TestSandbox
    from tests.test_development_registration import CapturingModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    from middleware.context import RequestContextMiddleware
    from subagents.profiles import build_subagents

    monkeypatch.setattr(main, "create_deep_agent", lambda **kwargs: kwargs)
    captured = main.build_agent(
        backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", channel=channel,
        model=CapturingModel(replies=[]), checkpointer=InMemorySaver(), store=InMemoryStore(),
    )
    contexts = [m for m in captured["middleware"] if isinstance(m, RequestContextMiddleware)]
    assert [(m.channel, m.agent_role) for m in contexts] == [(channel, "main")]
    for specs in [captured["subagents"], build_subagents(tmp_path, channel=channel)]:
        for spec in specs:
            context = next(m for m in spec["middleware"] if isinstance(m, RequestContextMiddleware))
            state = run_async(context.abefore_agent({}, None))
            assert state["channel"] == channel
            assert state["agent_role"] == spec["name"]


def test_extra_feishu_event_is_acknowledged_without_agent():
    import json
    from channels.feishu.events import build_event_handler
    handler = build_event_handler(lambda event: pytest.fail("must not submit an agent task"))
    handler._do_without_validation(json.dumps({
        "schema": "2.0", "header": {"event_type": "im.chat.access_event.bot_p2p_chat_entered_v1"},
        "event": {},
    }).encode())


@pytest.mark.parametrize("stage", ["connecting", "connected", "reconnecting"])
def test_worker_failure_bypasses_sdk_network_reconnect(stage):
    from channels.feishu.__main__ import WorkerFailed, abort_worker
    import lark_oapi.ws.client as sdk
    loop = asyncio.new_event_loop()
    client = object.__new__(sdk.Client)
    reconnect_calls = []

    async def connect():
        if stage == "connecting":
            loop.call_soon(abort_worker, Event())
            await asyncio.sleep(10)
        if stage == "reconnecting":
            raise OSError("offline network failure")
        loop.call_soon(abort_worker, Event())

    async def reconnect():
        reconnect_calls.append(True)
        loop.call_soon(abort_worker, Event())
        await asyncio.sleep(10)

    client._connect = connect
    client._disconnect = AsyncMock()
    client._reconnect = reconnect
    client._ping_loop = lambda: asyncio.sleep(10)
    client._auto_reconnect = True
    client._fmt_log = lambda *args: "offline lifecycle test"
    # 在目标阶段真正进入后触发故障，不依赖机器负载或定时器精度。
    try:
        with patch.object(sdk, "loop", loop), pytest.raises(WorkerFailed):
            client.start()
        assert len(reconnect_calls) == (1 if stage == "reconnecting" else 0)
    finally:
        pending = list(asyncio.all_tasks(loop))
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def test_resource_target_display_does_not_expose_credentials():
    from tests.verify_env import connection_target
    for url in ["postgresql://user:secret@localhost:5432/db?password=also-secret",
                "redis://user:secret@localhost:6379/0", "password=secret host=localhost"]:
        text = connection_target(url)
        assert "secret" not in text and "user" not in text
