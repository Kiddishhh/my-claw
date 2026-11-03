"""终端连续对话：先启动 serve_api，再运行 python -m channels.cli。

CLI 只是渠道，不自行维护数据库连接或创建第二套 Agent。
新建/切换通过 thread_id 续聊；删除调用与网页相同的服务端清理接口。
"""
import argparse
import asyncio
from pathlib import Path
from uuid import uuid4
import httpx
from infra.async_runtime import run_async


async def chat(base_url: str, thread_id: str | None = None) -> None:
    from api.schemas.tasks import TaskRequest
    if thread_id:
        thread_id = TaskRequest(thread_id=thread_id, message='validate').thread_id
    thread = thread_id or uuid4().hex
    print('MyClaw CLI：/new 新建；/use ID 切换；/history 历史；/upload 路径；/delete 删除；/exit 退出')
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(1900, connect=10)) as client:
        while True:
            print(f'\n会话：{thread}')
            try:
                text = (await asyncio.to_thread(input, '你 > ')).strip()
                if not text:
                    continue
                if text == '/exit':
                    break
                if text == '/new':
                    thread = uuid4().hex
                    continue
                if text.startswith('/use '):
                    thread = TaskRequest(thread_id=text[5:].strip(), message='validate').thread_id
                    continue
                if text == '/delete':
                    confirm = await asyncio.to_thread(input, '将永久删除本会话、项目文件及沙箱，输入 DELETE 确认：')
                    if confirm != 'DELETE':
                        continue
                    response = await client.delete(f'/v1/agent/sessions/{thread}')
                elif text == '/history':
                    response = await client.get(f'/v1/agent/sessions/{thread}/messages')
                elif text.startswith('/upload '):
                    from agent.sessions import MAX_UPLOAD_BYTES
                    path = Path(text[8:].strip().strip('"')).expanduser()
                    # CLI 运行在用户电脑，读取的正是用户电脑上的文件。
                    with path.open('rb') as source:
                        content = source.read(MAX_UPLOAD_BYTES + 1)
                    if len(content) > MAX_UPLOAD_BYTES:
                        print('文件不能超过 20 MiB')
                        continue
                    response = await client.post(f'/v1/agent/sessions/{thread}/files',
                                                 params={'filename': path.name}, content=content)
                else:
                    response = await client.post('/v1/agent/channels/cli/tasks',
                                                 json={'thread_id': thread, 'message': text})
                if response.is_error:
                    print(f'请求失败（HTTP {response.status_code}），可重试；未切换会话。')
                    continue
                data = response.json()
                if text == '/delete':
                    print('会话已删除')
                    thread = uuid4().hex
                elif text == '/history':
                    for message in data['messages']:
                        print(f"{message['role']} > {message['text']}")
                elif text.startswith('/upload '):
                    print('文件已上传并保存，后续消息请引用此沙箱路径：', data['path'])
                else:
                    print('Agent >', data['reply'] or '本次没有可展示的文字回复')
                    if not data['workspace_saved']:
                        print('注意：工作区未保存成功。')
            except EOFError:
                break
            except (httpx.HTTPError, OSError, ValueError) as exc:
                # 网络错误可能包含内部地址，终端只输出类型。
                print(f'操作失败：{type(exc).__name__}；请确认 API 已启动或输入有效。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--thread-id')
    args = parser.parse_args()
    try:
        run_async(chat(args.url, args.thread_id))
    except KeyboardInterrupt:
        print('\n已退出；服务端已接受的任务可能仍在处理，请稍后查看历史。')


if __name__ == '__main__':
    main()
