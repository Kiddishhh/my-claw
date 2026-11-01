"""同步沙箱资源的异步桥接：文件锁始终在同一专用线程进入和退出。

只把同步 SDK/磁盘操作送到线程；Agent 和异步数据库留在调用方事件循环。
取消等待无法停止 Python 工作线程，因此先等在途操作结束，才允许释放锁。
"""
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from deepagents.backends.protocol import SandboxBackendProtocol


async def drain(future):
    """屏蔽重复取消直至在途操作结束；返回结果和是否收到取消。"""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(future), cancelled
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            cancelled = True


class SessionWorker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sandbox-session")

    def submit(self, fn, *args, **kwargs):
        return asyncio.get_running_loop().run_in_executor(self.executor, partial(fn, *args, **kwargs))

    async def call(self, fn, *args, **kwargs):
        value, cancelled = await drain(self.submit(fn, *args, **kwargs))
        if cancelled:
            raise asyncio.CancelledError()
        return value


class SessionBackend(SandboxBackendProtocol):
    """保留同步后端接口，把全部异步 I/O 纳入持锁线程。

框架默认 aexecute/awrite 使用 to_thread，取消协程不会终止同步命令。
这里统一使用 worker.call，收到取消后仍等待实际 I/O 完成再抛出取消。
底层 SDK 自己的命令超时仍有效；该保证不覆盖用户主动启动的后台进程。
"""
    def __init__(self, backend, worker: SessionWorker):
        self.backend, self.worker = backend, worker

    @property
    def id(self):
        return self.backend.id

    def execute(self, command, *, timeout=None):
        return self.backend.execute(command, timeout=timeout)

    async def aexecute(self, command, *, timeout=None):
        return await self.worker.call(self.execute, command, timeout=timeout)

    def ls(self, path):
        return self.backend.ls(path)

    async def als(self, path):
        return await self.worker.call(self.ls, path)

    def read(self, file_path, offset=0, limit=2000):
        return self.backend.read(file_path, offset, limit)

    async def aread(self, file_path, offset=0, limit=2000):
        return await self.worker.call(self.read, file_path, offset, limit)

    def grep(self, pattern, path=None, glob=None, **kwargs):
        return self.backend.grep(pattern, path, glob, **kwargs)

    async def agrep(self, pattern, path=None, glob=None, **kwargs):
        return await self.worker.call(self.grep, pattern, path, glob, **kwargs)

    def glob(self, pattern, path=None):
        return self.backend.glob(pattern, path)

    async def aglob(self, pattern, path=None):
        return await self.worker.call(self.glob, pattern, path)

    def write(self, file_path, content):
        return self.backend.write(file_path, content)

    async def awrite(self, file_path, content):
        return await self.worker.call(self.write, file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return self.backend.edit(file_path, old_string, new_string, replace_all)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return await self.worker.call(self.edit, file_path, old_string, new_string, replace_all)

    def delete(self, file_path):
        return self.backend.delete(file_path)

    async def adelete(self, file_path):
        return await self.worker.call(self.delete, file_path)

    def upload_files(self, files):
        return self.backend.upload_files(files)

    async def aupload_files(self, files):
        return await self.worker.call(self.upload_files, files)

    def download_files(self, paths):
        return self.backend.download_files(paths)

    async def adownload_files(self, paths):
        return await self.worker.call(self.download_files, paths)


@asynccontextmanager
async def async_sandbox_session(context):
    worker = SessionWorker()
    entered = False
    try:
        resources, cancelled = await drain(worker.submit(context.__enter__))
        entered = True
        if cancelled:
            raise asyncio.CancelledError()
        yield resources, worker
    finally:
        # 正常、异常和取消均在持锁线程执行 __exit__，保持 FileLock 所有权。
        exc_info = sys.exc_info()
        try:
            if entered:
                _, cancelled = await drain(worker.submit(context.__exit__, *exc_info))
                if cancelled and exc_info[0] is None:
                    raise asyncio.CancelledError()
        finally:
            # 所有提交工作已经完成，shutdown 不再等待网络或磁盘。
            worker.executor.shutdown(wait=True)
