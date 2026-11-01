"""接口行为验证：使用注入运行器，不消耗真实模型 token。"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from api.app import create_app
from api.routers.tasks import get_task_runner


@pytest.mark.parametrize('saved', [True, False])
def test_task_contract_and_injection(saved):
    resources = SimpleNamespace(checkpointer=object(), store=object())
    @asynccontextmanager
    async def factory():
        yield resources
    app = create_app(resource_factory=factory)
    runner = AsyncMock(return_value={'reply': '完成', 'workspace_saved': saved, 'workspace_path': '/private'})
    app.dependency_overrides[get_task_runner] = lambda: runner
    with TestClient(app) as client:
        result = client.post('/v1/agent/tasks', json={'thread_id': 'demo-1', 'message': ' 开发页面 '})
        assert result.status_code == 200
        assert result.json() == {'thread_id': 'demo-1', 'reply': '完成', 'workspace_saved': saved,
                                 'status': 'completed' if saved else 'save_failed'}
        runner.assert_awaited_once_with('demo-1', '开发页面', user_id='local-user',
            checkpointer=resources.checkpointer, store=resources.store)
        for body in [{'thread_id': '../x', 'message': 'work'}, {'thread_id': 'demo', 'message': ' '},
                     {'thread_id': 'demo', 'message': 'work', 'user_id': 'other'}]:
            assert client.post('/v1/agent/tasks', json=body).status_code == 422
        runner.side_effect = TimeoutError()
        assert client.post('/v1/agent/tasks', json={'thread_id': 'demo', 'message': 'work'}).status_code == 504
