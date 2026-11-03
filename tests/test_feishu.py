"""飞书渠道离线行为测试：不连接飞书、不调用模型或创建沙箱。"""
import asyncio
from contextlib import asynccontextmanager
from queue import Full
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from infra.async_runtime import run_async
from channels.feishu.adapter import parse_message, split_reply
from channels.feishu.handler import MessageQueue, handle_message, consume
from channels.feishu.settings import FeishuSettings


def event(chat="chat-a", actor="user-a", message="m1", kind="text"):
    return NS(header=NS(app_id="app", tenant_key="tenant"), event=NS(
        sender=NS(sender_type="user", sender_id=NS(open_id=actor)),
        message=NS(chat_id=chat, chat_type="group", message_id=message,
                   message_type=kind, content='{"text":"@_user_1 创建页面"}',
                   mentions=[NS(key="@_user_1", name="助手")]),
    ))


def test_parse_and_shared_session():
    a = parse_message(event(), "app")
    b = parse_message(event(actor="user-b"), "app")
    c = parse_message(event(chat="chat-b"), "app")
    assert a.text == "@助手 创建页面"
    assert a.owner_id == b.owner_id
    assert a.owner_id != c.owner_id
    assert a.chat_id == b.chat_id
    assert parse_message(event(), "other-app") is None
    assert parse_message(event(kind="image"), "app") is None
    malformed = event()
    malformed.event.sender.sender_id.open_id = None
    assert parse_message(malformed, "app") is None


def test_queue_full_does_not_mark_message_seen_and_close_rejects():
    queue = MessageQueue(capacity=1, dedup_capacity=2)
    a = parse_message(event(), "app")
    b = parse_message(event(message="m2"), "app")
    assert queue.submit(a)
    assert not queue.submit(a)
    with pytest.raises(Full):
        queue.submit(b)
    assert queue.get_nowait() == a
    assert queue.submit(b)
    queue.close()
    with pytest.raises(RuntimeError):
        queue.submit(a)


def test_reply_chunks_preserve_unicode_content():
    text = "你好🙂" * 3000
    chunks = split_reply(text)
    assert "".join(chunks) == text
    assert all(len(part.encode("utf-8")) <= 3000 for part in chunks)


def test_handler_reuses_dispatcher_and_does_not_rerun_on_send_failure():
    async def check():
        runner = AsyncMock(return_value={"reply": "完成", "workspace_saved": False})
        adapter = NS(send=AsyncMock(side_effect=RuntimeError("send failed")))
        resources = NS(checkpointer=object(), store=object())
        msg = parse_message(event(), "app")
        await handle_message(msg, resources, adapter, runner=runner)
        assert runner.await_count == 1
        assert runner.call_args.kwargs["thread_id"] == msg.chat_id
        assert runner.call_args.kwargs["user_id"] == msg.owner_id
        assert runner.call_args.kwargs["channel"] == "feishu"
        assert runner.call_args.kwargs["checkpointer"] is resources.checkpointer
        assert "未成功保存" in adapter.send.call_args.args[1]
    run_async(check())


def test_consumer_serial_execution_and_resource_cleanup():
    async def check():
        queue = MessageQueue(capacity=3, dedup_capacity=10)
        queue.submit(parse_message(event(message="m1"), "app"))
        queue.submit(parse_message(event(message="m2"), "app"))
        order = []
        resources = NS(checkpointer=object(), store=object())

        @asynccontextmanager
        async def resource_factory():
            order.append("open")
            try:
                yield resources
            finally:
                order.append("close")

        async def handler(msg, res, adapter):
            assert res is resources
            order.append(msg.message_id + "-start")
            await asyncio.sleep(0)
            order.append(msg.message_id + "-end")
            if msg.message_id == "m2":
                queue.close()

        await consume(queue, object(), lambda: order.append("ready"),
                      resource_factory=resource_factory, handler=handler)
        assert order == ["open", "ready", "m1-start", "m1-end",
                         "m2-start", "m2-end", "close"]
    run_async(check())


def test_settings_do_not_require_bot_id_or_verification_token():
    settings = FeishuSettings(_env_file=None, app_id="app", app_secret="secret")
    assert settings.queue_size > 0
    assert "'secret'" not in repr(settings)


def test_entrypoint_drains_accepted_messages_on_shutdown(monkeypatch):
    """真实线程/循环生命周期，替换 SDK 网络和业务资源。"""
    import sys
    import channels.feishu.__main__ as entry

    order = []
    callback = []

    class Builder:
        def register_p2_im_message_receive_v1(self, fn):
            callback.append(fn)
            return self

        def register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self, fn):
            return self

        def build(self):
            return self

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            callback[0](event())
            callback[0](event())  # 重推只执行一次。
            raise KeyboardInterrupt

    @asynccontextmanager
    async def resources():
        order.append("open")
        try:
            yield object()
        finally:
            order.append("close")

    async def handle(msg, resources, adapter):
        order.append(msg.message_id)

    async def consumer(queue, adapter, ready):
        await consume(queue, adapter, ready, resource_factory=resources, handler=handle)

    monkeypatch.setitem(sys.modules, "lark_oapi", NS(
        EventDispatcherHandler=NS(builder=lambda *args: Builder()),
        ws=NS(Client=Client), LogLevel=NS(WARNING=1),
    ))
    monkeypatch.setattr(entry, "FeishuSettings", lambda: FeishuSettings(
        _env_file=None, app_id="app", app_secret="secret"))
    monkeypatch.setattr(entry, "FeishuAdapter", lambda settings: object())
    monkeypatch.setattr(entry, "consume", consumer)
    entry.main()
    assert order == ["open", "m1", "close"]


def test_actual_sdk_reply_request_without_network(monkeypatch):
    """使用已安装 SDK 的真实 builder，网络发送替换为本地假函数。"""
    from channels.feishu.adapter import FeishuAdapter

    async def check():
        adapter = FeishuAdapter(FeishuSettings(
            _env_file=None, app_id="app", app_secret="secret"))
        requests = []

        def reply(req):
            requests.append(req)
            return NS(success=lambda: True)

        monkeypatch.setattr(adapter.client.im.v1.message, "reply", reply)
        await adapter.send(parse_message(event(), "app"), "完成")
        assert len(requests) == 1
        assert requests[0].message_id == "m1"
        assert requests[0].request_body.msg_type == "text"

    run_async(check())


def test_actual_sdk_send_to_chat_reports_message_id_and_errors(monkeypatch):
    from channels.feishu.adapter import FeishuAdapter, FeishuAPIError

    async def check():
        adapter = FeishuAdapter(FeishuSettings(
            _env_file=None, app_id="app", app_secret="secret"))
        requests = []

        def create(req):
            requests.append(req)
            return NS(success=lambda: True, data=NS(message_id="om_sent"))

        monkeypatch.setattr(adapter.client.im.v1.message, "create", create)
        assert await adapter.send_to_chat("oc_real123", "自检") == "om_sent"
        assert requests[0].receive_id_type == "chat_id"
        assert requests[0].request_body.receive_id == "oc_real123"
        with pytest.raises(ValueError):
            await adapter.send_to_chat("chat_id", "自检")
        monkeypatch.setattr(adapter.client.im.v1.message, "create", lambda req: NS(
            success=lambda: False, code=230002, get_log_id=lambda: "log-id"))
        with pytest.raises(FeishuAPIError) as exc:
            await adapter.send_to_chat("oc_real123", "自检")
        assert exc.value.code == 230002
        assert exc.value.log_id == "log-id"
    run_async(check())


def test_send_script_rejects_placeholder_before_loading_credentials(monkeypatch):
    from scripts import verify_feishu_send as script
    monkeypatch.setattr(script, "FeishuSettings", lambda: pytest.fail("must not load secrets"))
    with pytest.raises(SystemExit) as exc:
        script.main(["chat_id"])
    assert exc.value.code == 2


def test_listen_once_prints_chat_id_without_agent(monkeypatch, capsys):
    import sys
    from scripts import verify_feishu_send as script
    callbacks = []

    class Builder:
        def register_p2_im_message_receive_v1(self, fn):
            callbacks.append(fn)
            return self

        def register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self, fn):
            return self

        def build(self):
            return self

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["auto_reconnect"] is False

        def start(self):
            loop = asyncio.get_event_loop()
            loop.call_soon(callbacks[0], event(chat="oc_test123"))
            loop.run_until_complete(asyncio.sleep(60))

    monkeypatch.setitem(sys.modules, "lark_oapi", NS(
        EventDispatcherHandler=NS(builder=lambda *args: Builder()),
        ws=NS(Client=Client), LogLevel=NS(WARNING=1),
    ))
    settings = FeishuSettings(_env_file=None, app_id="app", app_secret="secret")
    assert script.listen_once(settings, 5) == 0
    assert "chat_id=oc_test123" in capsys.readouterr().out
