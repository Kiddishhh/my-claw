"""进程级异步资源：API 与 CLI 共用，不依赖 FastAPI。

必须在实际运行 Agent 的事件循环中进入此上下文，不跨 asyncio.run 复用。
AsyncExitStack 保证建表或 Redis 探测失败时，已经打开的池也得到关闭。
"""
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from infra.postgres import build_pg_pool
from infra.redis import build_redis
from infra.settings import ResourceSettings


@dataclass(frozen=True)
class AppResources:
    pg_pool: AsyncConnectionPool
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    redis: Redis


async def initialize_persistence(pool: AsyncConnectionPool):
    """首次自动建表，后续 setup 按库的迁移记录跳过已执行项。

当前入口按单进程启动；以后增加 worker 时需单独串行化迁移。
Store 只是长期数据容器，绑定它不会自动提取用户偏好。
"""
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    saver, store = AsyncPostgresSaver(pool), AsyncPostgresStore(pool)
    await saver.setup()
    await store.setup()
    return saver, store


@asynccontextmanager
async def open_resources(settings: ResourceSettings | None = None):
    settings = settings or ResourceSettings()
    async with AsyncExitStack() as stack:
        pool = build_pg_pool(settings)
        stack.push_async_callback(pool.close)
        await pool.open()
        await pool.wait(timeout=settings.resource_timeout)
        saver, store = await initialize_persistence(pool)
        redis = build_redis(settings)
        stack.push_async_callback(redis.aclose)
        await redis.ping()
        yield AppResources(pool, saver, store, redis)
