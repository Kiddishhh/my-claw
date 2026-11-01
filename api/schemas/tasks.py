"""本机任务接口契约；不允许客户端指定服务器路径、用户或内部运行状态。"""
from typing import Annotated, Literal
from pydantic import StringConstraints
from api.schemas.common import APIModel


class TaskRequest(APIModel):
    thread_id: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50000)]


class TaskResponse(APIModel):
    thread_id: str
    status: Literal["completed", "save_failed"]
    reply: str
    workspace_saved: bool
