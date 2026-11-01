"""异步 Redis 客户端拥有自己的池；aclose 同时关闭池。"""
from redis.asyncio import ConnectionPool, Redis
from infra.settings import ResourceSettings


def build_redis(settings: ResourceSettings) -> Redis:
    pool = ConnectionPool.from_url(
        settings.redis_url.get_secret_value(), decode_responses=True,
        max_connections=settings.redis_max_connections,
        socket_connect_timeout=settings.resource_timeout,
        socket_timeout=settings.resource_timeout,
    )
    return Redis.from_pool(pool)
