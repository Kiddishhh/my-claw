"""渠道共用会话服务：历史、用户上传和主动删除；不依赖 FastAPI。

所有写入复用 sandbox manager 的会话锁。同步 SDK/磁盘在专用线程，
异步 Saver/Store 保留在创建连接池的事件循环，取消时等待在途 I/O 完成。
"""
import re
from pathlib import Path, PurePosixPath
from uuid import uuid4

from agent.result import extract_reply_text
from sandbox.async_session import async_sandbox_session, SessionBackend
from sandbox.manager import (
    PROJECT_ROOT, session_key, sandbox_for_thread, save_session_workspace,
    session_deletion, delete_session_sandbox, finish_session_deletion,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def upload_name(filename: str) -> str:
    """不信任客户端文件名；去路径和控制字符，加随机前缀防覆盖及设备名。"""
    basename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    safe = re.sub(r'[^\w. -]', '_', basename, flags=re.UNICODE).strip(' .') or 'file'
    suffix = PurePosixPath(safe).suffix
    if len(suffix.encode('utf-8')) > 30:
        suffix = ''
    stem = safe[:-len(suffix)] if suffix else safe
    # Linux 限制按字节而不是字符。为 UUID、扩展名和 .upload 留出空间。
    safe = stem.encode('utf-8')[:170].decode('utf-8', errors='ignore').rstrip(' .') + suffix
    # 运行日志后缀在备份层被排除；用户上传仍须持久化，显式改名而非悄悄丢失。
    if safe.endswith(('.log', '.pyc', '.pyo', '.tsbuildinfo')):
        safe += '.upload'
    return f'{uuid4().hex}_{safe}'


async def upload_file(thread_id: str, filename: str, content: bytes, *,
                      user_id: str = 'local-user', project_root: Path | None = None,
                      settings=None, client=None) -> dict:
    """上传到 inputs/，返回真实沙箱绝对路径；不在宿主机执行文件。"""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError('文件超过 20 MiB')
    root = (project_root or PROJECT_ROOT).resolve()
    context = sandbox_for_thread(user_id, thread_id, root, settings=settings, client=client)
    async with async_sandbox_session(context) as (resources, worker):
        raw_backend, _, _, workdir = resources
        backend = SessionBackend(raw_backend, worker)
        path = str(PurePosixPath(workdir) / 'inputs' / upload_name(filename))
        # upload_files 在不同后端不保证递归创建父目录；用明确绝对路径准备目录。
        import shlex
        result = await backend.aexecute('mkdir -p ' + shlex.quote(str(PurePosixPath(path).parent)))
        if result.exit_code != 0:
            raise RuntimeError('上传目录创建失败')
        results = await backend.aupload_files([(path, content)])
        if len(results) != 1 or results[0].error:
            raise RuntimeError('沙箱上传失败')
        # 保存失败不返回成功；远程文件可能已经存在，用户可保留路径后重试。
        await worker.call(save_session_workspace, root, session_key(user_id, thread_id), backend, workdir)
        return {'path': path, 'size': len(content), 'workspace_saved': True}


async def read_history(thread_id: str, *, checkpointer, user_id: str = 'local-user') -> list[dict]:
    """只展示用户消息和最终文字；不公开工具输出、推理或内部状态。"""
    record = await checkpointer.aget_tuple({'configurable': {'thread_id': session_key(user_id, thread_id)}})
    if record is None:
        return []
    result = []
    for message in record.checkpoint.get('channel_values', {}).get('messages', []):
        role = getattr(message, 'type', None)
        if role not in {'human', 'ai'} or getattr(message, 'tool_calls', None):
            continue
        text = extract_reply_text(message.content)
        if text:
            result.append({'role': 'user' if role == 'human' else 'assistant', 'text': text})
    return result


async def delete_session(thread_id: str, *, checkpointer, store, user_id: str = 'local-user',
                         project_root: Path | None = None, settings=None, client=None) -> None:
    """删除指定会话；失败抛出并保留 deleting 状态，重试继续清理。

    Store 不会自动按 thread_id 划分。仅删除约定的 ('sessions', key) 命名空间，
    不删除用户级共享记忆；现有 Agent 尚未写入此命名空间。
    Redis 当前只是连接资源，没有会话缓存，绝不 flush 整个数据库。
    """
    root = (project_root or PROJECT_ROOT).resolve()
    async with async_sandbox_session(session_deletion(root, user_id, thread_id)) as (key, worker):
        await worker.call(delete_session_sandbox, root, key, settings=settings, client=client)
        await checkpointer.adelete_thread(key)
        while items := await store.asearch(('sessions', key), limit=100):
            for item in items:
                await store.adelete(item.namespace, item.key)
        await worker.call(finish_session_deletion, root, key)
