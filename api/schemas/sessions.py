"""本地渠道契约：身份由服务器固定，客户端只能指定会话 ID。"""
from typing import Annotated, Literal
from pydantic import StringConstraints
from api.schemas.common import APIModel

ThreadId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]


class ChatMessage(APIModel):
    role: Literal['user', 'assistant']
    text: str


class HistoryResponse(APIModel):
    thread_id: str
    messages: list[ChatMessage]


class DeleteResponse(APIModel):
    thread_id: str
    deleted: bool


class UploadResponse(APIModel):
    path: str
    size: int
    workspace_saved: bool
