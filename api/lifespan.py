"""应用启动时获取资源，关闭时退出上下文；请求不临时建池。"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from infra.resources import open_resources


def make_lifespan(resource_factory=open_resources):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with resource_factory() as resources:
            app.state.resources = resources
            try:
                yield
            finally:
                # 清除引用后由 open_resources 关闭所有连接。
                app.state.resources = None
    return lifespan
