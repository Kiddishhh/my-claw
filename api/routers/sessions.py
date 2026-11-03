"""本机单用户渠道。复用依赖注入和全局异常处理，不接收任意身份/磁盘路径。"""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from filelock import Timeout as LockTimeout

from agent import sessions
from agent.result import extract_reply_text
from api.dependencies import ResourcesDep
from api.routers.tasks import get_task_runner
from api.schemas.tasks import TaskRequest, TaskResponse
from api.schemas.sessions import ThreadId, HistoryResponse, DeleteResponse, UploadResponse
from domain.errors import BusinessError


def local_request(request: Request) -> None:
    """无登录版本只供本机使用；阻止其他网站在浏览器中跨站调用写接口。

    CLI 不发送 Origin，仍可使用。本检查不是多人鉴权，不能据此公开部署服务。
    """
    if request.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}:
        raise HTTPException(403)
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403)


router = APIRouter(prefix='/v1/agent', tags=['sessions'], dependencies=[Depends(local_request)])


def get_session_service():
    return sessions


ServiceDep = Annotated[object, Depends(get_session_service)]


@router.post('/channels/{channel}/tasks', response_model=TaskResponse)
async def channel_task(channel: Literal['web', 'cli'], body: TaskRequest, resources: ResourcesDep,
                       runner: Annotated[object, Depends(get_task_runner)]):
    try:
        result = await runner(body.thread_id, body.message, user_id='local-user', channel=channel,
                              checkpointer=resources.checkpointer, store=resources.store)
    except LockTimeout as exc:
        raise BusinessError('CONFLICT') from exc
    except TimeoutError as exc:
        raise BusinessError('TASK_TIMEOUT') from exc
    return TaskResponse(thread_id=body.thread_id, reply=extract_reply_text(result['reply']),
                        workspace_saved=result['workspace_saved'],
                        status='completed' if result['workspace_saved'] else 'save_failed')


@router.get('/sessions/{thread_id}/messages', response_model=HistoryResponse)
async def history(thread_id: ThreadId, resources: ResourcesDep, service: ServiceDep):
    messages = await service.read_history(thread_id, checkpointer=resources.checkpointer)
    return HistoryResponse(thread_id=thread_id, messages=messages)


@router.delete('/sessions/{thread_id}', response_model=DeleteResponse)
async def delete(thread_id: ThreadId, resources: ResourcesDep, service: ServiceDep):
    try:
        await service.delete_session(thread_id, checkpointer=resources.checkpointer, store=resources.store)
    except LockTimeout as exc:
        raise BusinessError('CONFLICT') from exc
    return DeleteResponse(thread_id=thread_id, deleted=True)


@router.post('/sessions/{thread_id}/files', response_model=UploadResponse)
async def upload(thread_id: ThreadId, request: Request, service: ServiceDep,
                 filename: Annotated[str, Query(min_length=1, max_length=255)]):
    """原始二进制流上传；逐块限额，避免 multipart 在检查前写出巨型临时文件。

    浏览器直接发送 File 对象；文件名为查询参数，真正路径由服务器生成。
    一次最多 20 MiB，不解压、不执行、也不把文件内容放进模型提示词。
    """
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > sessions.MAX_UPLOAD_BYTES:
            raise HTTPException(413, '文件超过大小上限')
        data.extend(chunk)
    try:
        return UploadResponse(**await service.upload_file(thread_id, filename, bytes(data)))
    except LockTimeout as exc:
        raise BusinessError('CONFLICT') from exc
