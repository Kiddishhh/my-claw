"""纯 ASGI 中间件；请求变量为局部变量，不在实例上共享。"""
from time import perf_counter
from uuid import uuid4
from starlette.datastructures import MutableHeaders
from infra.logging import get_logger

logger = get_logger()


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex  # 服务端生成，不信任任意客户端头作为日志标识。
        scope.setdefault("state", {})["request_id"] = request_id
        started, status = perf_counter(), 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # 记录路由模板，不记录 URL 查询串（可能包含用户数据）。
            route = getattr(scope.get("route"), "path", "<unmatched>")
            logger.info("HTTP request_id={} method={} route={} status={} elapsed_ms={:.1f}",
                        request_id, scope["method"], route, status, (perf_counter()-started)*1000)
