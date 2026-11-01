"""API 基础层无需真实模型或沙箱；资源故障必须正确关闭并返回 503。"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from api.app import create_app


def test_health_errors_and_request_ids():
    pool = SimpleNamespace(connection=None)
    @asynccontextmanager
    async def resources():
        yield SimpleNamespace(pg_pool=pool, redis=SimpleNamespace(ping=AsyncMock()))
    app = create_app(resource_factory=resources)
    @app.get('/broken')
    async def broken():
        raise RuntimeError('private-secret')
    with TestClient(app, raise_server_exceptions=False) as client:
        live = client.get('/healthz')
        assert live.status_code == 200 and live.json() == {'status': 'ok'}
        assert live.headers['X-Request-ID']
        assert client.get('/readyz').status_code == 503
        for path, status in [('/missing', 404), ('/broken', 500)]:
            response = client.get(path)
            assert response.status_code == status
            assert response.json()['request_id'] == response.headers['X-Request-ID']
            assert 'private-secret' not in response.text


def test_startup_failure_closes_opened_resources(monkeypatch):
    from infra import resources as module
    pool = SimpleNamespace(open=AsyncMock(), wait=AsyncMock(), close=AsyncMock())
    redis = SimpleNamespace(ping=AsyncMock(side_effect=RuntimeError('unavailable')), aclose=AsyncMock())
    monkeypatch.setattr(module, 'build_pg_pool', lambda settings: pool)
    monkeypatch.setattr(module, 'build_redis', lambda settings: redis)
    monkeypatch.setattr(module, 'initialize_persistence', AsyncMock(return_value=(object(), object())))
    async def run():
        import pytest
        with pytest.raises(RuntimeError):
            async with module.open_resources():
                raise AssertionError('must not yield')
    asyncio.run(run())
    pool.close.assert_awaited_once()
    redis.aclose.assert_awaited_once()


def test_ready_validation_and_shutdown():
    from api.schemas.common import APIModel
    class Input(APIModel):
        count: int
    conn = SimpleNamespace(execute=AsyncMock())
    @asynccontextmanager
    async def connection():
        yield conn
    closed = []
    @asynccontextmanager
    async def resources():
        try:
            yield SimpleNamespace(pg_pool=SimpleNamespace(connection=connection),
                                  redis=SimpleNamespace(ping=AsyncMock()))
        finally:
            closed.append(True)
    app = create_app(resource_factory=resources)
    @app.post('/validate', response_model=Input)
    async def validate(body: Input):
        return body
    with TestClient(app) as client:
        assert client.get('/readyz').json() == {'status': 'ready'}
        for body in [{'count': '1'}, {'count': 1, 'extra': True}]:
            result = client.post('/validate', json=body)
            assert result.status_code == 422
            assert result.json()['code'] == 'VALIDATION_ERROR'
        assert client.post('/validate', json={'count': 1}).json() == {'count': 1}
    assert closed == [True] and app.state.resources is None


def test_shared_pool_setup_and_failure_cleanup(monkeypatch):
    from infra import resources as module
    conn = SimpleNamespace(execute=AsyncMock())
    @asynccontextmanager
    async def connection():
        yield conn
    pool = SimpleNamespace(connection=connection)
    saver, store = SimpleNamespace(setup=AsyncMock()), SimpleNamespace(setup=AsyncMock())
    received = []
    monkeypatch.setattr(module, 'AsyncPostgresSaver', lambda p: received.append(p) or saver)
    monkeypatch.setattr(module, 'AsyncPostgresStore', lambda p: received.append(p) or store)
    assert asyncio.run(module.initialize_persistence(pool)) == (saver, store)
    assert received == [pool, pool]
    saver.setup.assert_awaited_once()
    store.setup.assert_awaited_once()


def test_business_error_is_mapped_only_at_http_boundary():
    from domain.errors import BusinessError
    @asynccontextmanager
    async def resources():
        yield SimpleNamespace()
    app = create_app(resource_factory=resources)
    @app.get('/conflict')
    async def conflict():
        raise BusinessError('CONFLICT')
    with TestClient(app) as client:
        response = client.get('/conflict')
        assert response.status_code == 409
        assert response.json()['code'] == 'CONFLICT'
        assert response.json()['request_id'] == response.headers['X-Request-ID']
