"""Windows psycopg 使用 Selector loop；只在进程入口选择，不修改请求中的循环。"""
import asyncio
import sys


def run_async(coroutine):
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coroutine)
