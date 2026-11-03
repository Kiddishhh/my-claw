"""Web/CLI 同一服务端契约，测试注入替身，不创建真实沙箱。"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from filelock import Timeout
from api.app import create_app
from api.routers.sessions import get_session_service
from api.routers.tasks import get_task_runner


@pytest.fixture
def setup():
    resources = SimpleNamespace(checkpointer=object(), store=object())
    @asynccontextmanager
    async def factory():
        yield resources
    app = create_app(resource_factory=factory)
    service = SimpleNamespace(read_history=AsyncMock(return_value=[]), delete_session=AsyncMock(),
                              upload_file=AsyncMock(return_value={'path':'/workspace/inputs/file.csv','size':3,'workspace_saved':True}))
    runner = AsyncMock(return_value={'reply':'done','workspace_saved':True})
    app.dependency_overrides[get_session_service] = lambda: service
    app.dependency_overrides[get_task_runner] = lambda: runner
    with TestClient(app) as client:
        yield client, service, runner


def test_web_cli_sources_and_static(setup):
    client, service, runner = setup
    assert client.get('/').status_code == 200
    assert client.get('/assets/app.js').status_code == 200
    for channel in ('web', 'cli'):
        assert client.post(f'/v1/agent/channels/{channel}/tasks', json={'thread_id':'one','message':'hello'}).status_code == 200
        assert runner.await_args.kwargs['channel'] == channel
    assert client.post('/v1/agent/channels/feishu/tasks', json={'thread_id':'one','message':'hello'}).status_code == 422


def test_upload_delete_history_and_origin(setup, monkeypatch):
    client, service, runner = setup
    prefix = '/v1/agent/sessions/one'
    assert client.get(prefix + '/messages').json() == {'thread_id':'one','messages':[]}
    response = client.post(prefix + '/files?filename=data.csv', content=b'abc')
    assert response.status_code == 200
    service.upload_file.assert_awaited_once_with('one','data.csv',b'abc')
    from agent import sessions
    monkeypatch.setattr(sessions, 'MAX_UPLOAD_BYTES', 2)
    assert client.post(prefix + '/files?filename=x',content=b'abc').status_code == 413
    assert client.delete(prefix,headers={'Origin':'https://evil.example'}).status_code == 403
    service.delete_session.assert_not_awaited()
    assert client.delete(prefix).json()['deleted'] is True
    service.delete_session.side_effect = Timeout('busy')
    assert client.delete(prefix).status_code == 409
    assert client.delete('/v1/agent/sessions/bad%20id').status_code == 422
