"""全局错误边界：客户端只收到稳定提示，日志保留异常类型和代码位置。"""
from traceback import extract_tb
from uuid import uuid4
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from api.schemas.errors import ErrorResponse
from domain.errors import BusinessError
from infra.logging import get_logger

logger = get_logger()


def register_exception_handlers(app: FastAPI) -> None:
    def respond(request: Request, status: int, code: str, message: str, headers=None):
        request_id = getattr(request.state, "request_id", None) or uuid4().hex
        request.state.request_id = request_id
        body = ErrorResponse(code=code, message=message, request_id=request_id)
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"),
                            headers={**(headers or {}), "X-Request-ID": request_id})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return respond(request, 422, "VALIDATION_ERROR", "请求参数不符合要求")

    @app.exception_handler(BusinessError)
    async def business_error(request: Request, exc: BusinessError):
        mapping = {
            "INVALID_INPUT": (400, "输入不符合业务要求"),
            "NOT_FOUND": (404, "资源不存在"),
            "CONFLICT": (409, "当前状态不允许此操作"),
            "UNAVAILABLE": (503, "服务暂不可用"),
            "TASK_TIMEOUT": (504, "任务执行超时，已尝试保存工作区"),
        }
        # 不直接回显未知 code，避免业务层误把内部异常文本当作代码传入。
        code = exc.code if exc.code in mapping else "BUSINESS_ERROR"
        status, message = mapping.get(code, (400, "操作未能完成"))
        return respond(request, status, code, message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        messages = {400: "请求不合法", 401: "身份凭据无效", 403: "无权访问", 404: "资源不存在",
                    405: "请求方法不支持", 409: "资源状态冲突", 429: "请求过于频繁", 503: "服务暂不可用"}
        return respond(request, exc.status_code, "HTTP_ERROR",
                       messages.get(exc.status_code, "请求处理失败"), exc.headers)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        response = respond(request, 500, "INTERNAL_ERROR", "服务器处理失败，请稍后重试")
        # 不记录 exc 文本/局部变量/请求正文：数据库错误可能含连接密码。
        frames = [(f.filename, f.lineno, f.name) for f in extract_tb(exc.__traceback__)]
        logger.error("request_id={} exception_type={} frames={}",
                     request.state.request_id, type(exc).__name__, frames)
        return response
