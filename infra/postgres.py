"""仅构造连接池；打开、建表、关闭由应用生命周期统一负责。"""
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from infra.settings import ResourceSettings


def build_pg_pool(settings: ResourceSettings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=settings.postgres_url.get_secret_value(), open=False,
        min_size=settings.pg_min_size, max_size=settings.pg_max_size,
        timeout=settings.resource_timeout,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    )
