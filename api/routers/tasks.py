"""本机单用户入口：在当前请求内等待任务完成，不自动重试。"""
from typing import Annotated
from fastapi import APIRouter, Depends
from filelock import Timeout as LockTimeout
from agent.dispatcher import run_task
from agent.result import extract_reply_text
from api.dependencies import ResourcesDep
from api.schemas.tasks import TaskRequest, TaskResponse
from domain.errors import BusinessError

router = APIRouter(prefix="/v1/agent", tags=["agent"])


def get_task_runner():
    """依赖注入运行器，测试可替换而不调用模型或沙箱。"""
    return run_task


@router.post("/tasks", response_model=TaskResponse)
async def execute_task(body: TaskRequest, resources: ResourcesDep,
                       runner: Annotated[object, Depends(get_task_runner)]) -> TaskResponse:
    # 本机单用户身份固定在服务器；以后认证接入时改成身份依赖。
    try:
        result = await runner(body.thread_id, body.message, user_id="local-user", channel="api",
                              checkpointer=resources.checkpointer, store=resources.store)
    except LockTimeout as exc:
        raise BusinessError("CONFLICT") from exc
    except TimeoutError as exc:
        raise BusinessError("TASK_TIMEOUT") from exc
    reply = extract_reply_text(result["reply"])
    saved = result["workspace_saved"]
    return TaskResponse(thread_id=body.thread_id,
                        status="completed" if saved else "save_failed",
                        reply=reply, workspace_saved=saved)
