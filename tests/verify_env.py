"""基础资源连通验收：复用正式连接池，不输出连接密码。

运行：uv run --all-extras python -m tests.verify_env
无需模型密钥；与正式服务一样执行 saver/store 的 setup。
"""
from urllib.parse import urlsplit

from infra.async_runtime import run_async
from infra.resources import open_resources
from infra.settings import ResourceSettings


def connection_target(value: str) -> str:
    """只显示主机/端口；不输出 userinfo、路径、查询参数或非法原文。"""
    try:
        target = urlsplit(value)
        if target.scheme not in {"postgresql", "postgres", "redis", "rediss"} or not target.hostname:
            return "连接目标已配置（详情隐藏）"
        port = target.port or (6379 if target.scheme.startswith("redis") else 5432)
        return f"{target.hostname}:{port}"
    except ValueError:
        return "连接目标已配置（详情隐藏）"


async def main() -> None:
    settings = ResourceSettings()
    print("PostgreSQL：", connection_target(settings.postgres_url.get_secret_value()))
    print("Redis：", connection_target(settings.redis_url.get_secret_value()))
    # 初始化失败/查询失败/正常结束，都由 open_resources 关闭已有连接池。
    async with open_resources(settings) as resources:
        async with resources.pg_pool.connection() as connection:
            await connection.execute("SELECT 1")
        await resources.redis.ping()
        print("PostgreSQL、Redis、checkpoint/store 初始化与连通检查通过。")


if __name__ == "__main__":
    try:
        run_async(main())
    except Exception as exc:
        print(f"资源验收失败：{type(exc).__name__}")
        raise SystemExit(1) from None
