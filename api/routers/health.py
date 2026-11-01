import asyncio
from fastapi import APIRouter, Response
from api.dependencies import ResourcesDep
from api.schemas.health import HealthResponse
from infra.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger()


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """进程能响应 HTTP；不代表模型和每个业务接口正常。"""
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse,
            responses={503: {"model": HealthResponse}})
async def readyz(response: Response, resources: ResourcesDep) -> HealthResponse:
    """仅在被请求时检查 PG 和 Redis；总时限三秒，无周期任务。"""
    try:
        async with asyncio.timeout(3):
            async with resources.pg_pool.connection() as conn:
                await conn.execute("SELECT 1")
            await resources.redis.ping()
    except Exception as exc:
        logger.warning("readiness failed exception_type={}", type(exc).__name__)
        response.status_code = 503
        return HealthResponse(status="not_ready")
    return HealthResponse(status="ready")
