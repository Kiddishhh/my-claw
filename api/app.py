"""FastAPI 组装入口，导入模块不会建立数据库连接或调用模型。"""
from fastapi import FastAPI
from api.exception_handlers import register_exception_handlers
from api.lifespan import make_lifespan
from api.middleware.request_context import RequestContextMiddleware
from api.routers.health import router
from api.routers.tasks import router as tasks_router
from infra.resources import open_resources
from api.routers.sessions import router as sessions_router
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse


def create_app(*, resource_factory=open_resources) -> FastAPI:
    app = FastAPI(title="my-claw API", lifespan=make_lifespan(resource_factory))
    register_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    app.include_router(tasks_router)
    app.include_router(sessions_router)
    static = Path(__file__).resolve().parents[1] / 'channels/web/static'
    # 只挂载明确前缀，不用 '/' 截获后续注册的接口。
    app.mount('/assets', StaticFiles(directory=static), name='web-assets')
    @app.get('/', include_in_schema=False)
    async def web_index():
        return FileResponse(static / 'index.html')
    return app


app = create_app()
