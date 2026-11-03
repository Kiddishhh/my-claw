"""飞书格式适配：解析入站文本、生成稳定会话归属、发送结果。

这里只接收文本。群消息范围由飞书授权决定：当前按群 @ 权限接入；
以后如果申请群全量消息权限，需要另行添加触发条件。
"""
import asyncio
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .settings import FeishuSettings


class FeishuAPIError(RuntimeError):
    """保留可定位的错误码/请求 ID，不回显可能含敏感信息的原始响应。"""
    def __init__(self, code: int | None, log_id: str | None):
        self.code, self.log_id = code, log_id
        super().__init__(f"feishu_api_failed:code={code},log_id={log_id}")


class FeishuMessage(BaseModel):
    """渠道内部消息；发送者只用于上下文，不参与成员鉴权。"""
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    app_id: str = Field(min_length=1)
    tenant_key: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @property
    def owner_id(self) -> str:
        """同群共享 owner，不同渠道/应用/租户/群隔离。

        当前引擎同时用 user_id 和 thread_id 定位 checkpoint/沙箱，
        因此不能把每位发言者的 open_id 直接作为引擎 user_id。
        """
        raw = json.dumps(["feishu", self.app_id, self.tenant_key, self.chat_id])
        return "feishu-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_message(data: Any, app_id: str) -> FeishuMessage | None:
    """解析 SDK 事件；忽略非文本、机器人及字段缺失的消息。"""
    header = getattr(data, "header", None)
    event = getattr(data, "event", None)
    if getattr(header, "app_id", None) != app_id or event is None:
        return None
    msg = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    if (getattr(msg, "message_type", None) != "text"
            or getattr(msg, "chat_type", None) not in {"p2p", "group"}
            or getattr(sender, "sender_type", None) != "user"):
        return None
    try:
        content = json.loads(msg.content)
    except (TypeError, ValueError, AttributeError):
        return None
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    # 保留被 @ 成员的含义，只将平台占位符转换为名称。
    for mention in getattr(msg, "mentions", None) or []:
        key = getattr(mention, "key", None)
        if key:
            text = text.replace(key, f"@{getattr(mention, 'name', None) or '成员'}")
    try:
        return FeishuMessage(
            app_id=app_id,
            tenant_key=getattr(header, "tenant_key", None)
            or getattr(sender, "tenant_key", None),
            message_id=getattr(msg, "message_id", None),
            chat_id=getattr(msg, "chat_id", None),
            actor_id=getattr(getattr(sender, "sender_id", None), "open_id", None),
            text=text.strip(),
        )
    except ValidationError:
        # 不用 unknown 填身份，避免错误地将不同来源合成一个会话。
        return None


def split_reply(text: str) -> list[str]:
    """保守地按 3000 UTF-8 字节拆分，保持中文和表情字符完整。"""
    parts, chars, size = [], [], 0
    for char in text or "任务已处理完成。":
        count = len(char.encode("utf-8"))
        if size + count > 3000:
            parts.append("".join(chars))
            chars, size = [], 0
        chars.append(char)
        size += count
    if chars:
        parts.append("".join(chars))
    return parts


class FeishuAdapter:
    def __init__(self, settings: FeishuSettings):
        # 只有运行入口初始化 SDK loop 后才构造适配器。
        import lark_oapi as lark
        self.client = (lark.Client.builder().app_id(settings.app_id)
                       .app_secret(settings.app_secret.get_secret_value())
                       .timeout(15).build())

    async def send(self, message: FeishuMessage, text: str) -> None:
        """回复原消息，便于群内识别结果对应的提问。

        同步 SDK 放入线程，避免网络和 token 刷新阻塞 Agent 的循环。
        本版不自动重试发送，失败交给 handler 记录，不重新运行 Agent。
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        for part in split_reply(text):
            body = (ReplyMessageRequestBody.builder().msg_type("text")
                    .content(json.dumps({"text": part}, ensure_ascii=False)).build())
            req = (ReplyMessageRequest.builder().message_id(message.message_id)
                   .request_body(body).build())
            resp = await asyncio.to_thread(self.client.im.v1.message.reply, req)
            if not resp.success():
                raise FeishuAPIError(resp.code, resp.get_log_id())

    async def send_to_chat(self, chat_id: str, text: str) -> str:
        """主动发一条文本到指定会话，用于发送链路验收。

        与 send(message, text) 回复已有消息不同，此方法只需要 chat_id。
        成功返回飞书 message_id；不自动重试，避免重复投递自检消息。
        """
        if not re.fullmatch(r"oc_[A-Za-z0-9]+", chat_id):
            raise ValueError("请传入真实的 oc_ 开头 chat_id")
        if not text.strip() or len(text.encode("utf-8")) > 3000:
            raise ValueError("自检正文必须非空且不超过 3000 字节")
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = (CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).build())
        req = (CreateMessageRequest.builder().receive_id_type("chat_id")
               .request_body(body).build())
        resp = await asyncio.to_thread(self.client.im.v1.message.create, req)
        if not resp.success():
            raise FeishuAPIError(resp.code, resp.get_log_id())
        message_id = getattr(getattr(resp, "data", None), "message_id", None)
        if not message_id:
            raise RuntimeError("飞书返回成功但缺少 message_id，不能确认发送结果")
        return message_id
