"""统一入口必须在持锁范围内保存，保存失败不能伪装成持久化成功。"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent import dispatcher as d
from infra.settings import SandboxSettings
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore


@pytest.fixture(autouse=True)
def explicit_test_persistence(monkeypatch):
    """测试显式提供内存实现；生产入口不隐式降级。"""
    original = d.run_task
    async def run(*args, **kwargs):
        kwargs.setdefault('checkpointer', InMemorySaver())
        kwargs.setdefault('store', InMemoryStore())
        return await original(*args, **kwargs)
    monkeypatch.setattr(d, 'run_task', run)


def test_agent_runs_on_callers_loop_and_session_stays_on_one_thread(tmp_path, monkeypatch):
    from threading import get_ident
    threads = []
    observed = {}
    @contextmanager
    def session(*args, **kwargs):
        threads.append(get_ident())
        try:
            yield Mock(), SimpleNamespace(id='sandbox'), Mock(), '/workspace'
        finally:
            threads.append(get_ident())
    async def invoke(*args, **kwargs):
        observed['loop'] = asyncio.get_running_loop()
        observed['thread_key'] = kwargs['config']['configurable']['thread_id']
        return {'messages': [SimpleNamespace(content='done')]}
    builder = Mock(return_value=SimpleNamespace(ainvoke=invoke))
    monkeypatch.setattr(d, 'sandbox_for_thread', session)
    monkeypatch.setattr(d, 'save_session_workspace', lambda *args: threads.append(get_ident()))
    monkeypatch.setattr(d, 'build_agent', builder)
    async def run():
        saver, store = InMemorySaver(), InMemoryStore()
        await d.run_task('same-thread', 'build', user_id='alice', project_root=tmp_path,
                         checkpointer=saver, store=store,
                         settings=SandboxSettings(daytona_api_key='test', _env_file=None))
        assert observed['loop'] is asyncio.get_running_loop()
        assert builder.call_args.kwargs['checkpointer'] is saver
        assert builder.call_args.kwargs['store'] is store
        assert observed['thread_key'] == d._session_key('alice', 'same-thread')
        assert len(set(threads)) == 1 and threads[0] != get_ident()
    asyncio.run(run())


def test_cancel_during_acquisition_waits_and_releases_on_same_thread():
    from sandbox.async_session import async_sandbox_session
    from threading import Event, get_ident
    started, release = Event(), Event()
    order = []
    @contextmanager
    def session():
        order.append(('enter', get_ident()))
        started.set()
        assert release.wait(5)
        try:
            yield None
        finally:
            order.append(('exit', get_ident()))
    async def run():
        async def invoke():
            async with async_sandbox_session(session()):
                raise AssertionError('cancelled acquisition must not invoke')
        task = asyncio.create_task(invoke())
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [name for name, _ in order] == ['enter', 'exit']
        assert order[0][1] == order[1][1]
    asyncio.run(run())


@pytest.mark.parametrize('timeout', [False, True])
def test_cancel_waits_for_actual_backend_command(tmp_path, monkeypatch, timeout):
    from threading import Event
    from tests.test_remote_agent import TestSandbox
    from deepagents.backends.protocol import ExecuteResponse
    started, release = Event(), Event()
    order = []
    class BlockingBackend(TestSandbox):
        def execute(self, command, *, timeout=None):
            started.set()
            assert release.wait(5)
            order.append('command_finished')
            return ExecuteResponse(output='ok', exit_code=0, truncated=False)
    @contextmanager
    def session(*args, **kwargs):
        try:
            yield BlockingBackend(root_dir=tmp_path), SimpleNamespace(id='sandbox'), None, '/workspace'
        finally:
            order.append('unlock')
    def build(**kwargs):
        async def invoke(*args, **options):
            await kwargs['backend'].aexecute('write files')
            return {'messages': [SimpleNamespace(content='ok')]}
        return SimpleNamespace(ainvoke=invoke)
    monkeypatch.setattr(d, 'sandbox_for_thread', session)
    monkeypatch.setattr(d, 'build_agent', build)
    monkeypatch.setattr(d, 'save_session_workspace', lambda *args: order.append('save'))
    monkeypatch.setattr(d, '_sql', lambda *args: [])
    async def run():
        settings = SandboxSettings(daytona_api_key='test', _env_file=None)
        settings.syc_sandbox_task_timeout = .05 if timeout else 10
        task = asyncio.create_task(d.run_task('t', 'work', settings=settings, project_root=tmp_path))
        assert await asyncio.to_thread(started.wait, 5)
        if not timeout:
            task.cancel()
        await asyncio.sleep(.1)
        try:
            assert not task.done() and order == []
        finally:
            release.set()
            with pytest.raises(TimeoutError if timeout else asyncio.CancelledError):
                await task
        assert order == ['command_finished', 'save', 'unlock']
    asyncio.run(run())


@pytest.mark.parametrize("backup_fails", [False, True])
def test_dispatcher_saves_before_unlock(tmp_path, monkeypatch, backup_fails):
    held = []
    @contextmanager
    def session(*args, **kwargs):
        held.append(True)
        try:
            yield Mock(), SimpleNamespace(id="sandbox"), Mock(), "/workspace"
        finally:
            held.pop()
    def save(*args):
        assert held
        if backup_fails:
            raise OSError("disk full")
        return ".sandbox-state/workspaces/version"
    monkeypatch.setattr(d, "sandbox_for_thread", session)
    monkeypatch.setattr(d, "save_session_workspace", save)
    monkeypatch.setattr(d, "build_agent", lambda **kwargs: SimpleNamespace(
        ainvoke=AsyncMock(return_value={"messages": [SimpleNamespace(content="done")]})))
    result = asyncio.run(d.run_task("thread", "build web", project_root=tmp_path,
                                   settings=SandboxSettings(daytona_api_key="test", _env_file=None)))
    assert result["workspace_saved"] is (not backup_fails)
    assert not held


def test_execution_error_attempts_backup_and_preserves_original(tmp_path, monkeypatch):
    @contextmanager
    def session(*args, **kwargs):
        yield Mock(), SimpleNamespace(id="sandbox"), Mock(), "/workspace"
    save = Mock(side_effect=OSError("disk full"))
    monkeypatch.setattr(d, "sandbox_for_thread", session)
    monkeypatch.setattr(d, "save_session_workspace", save)
    monkeypatch.setattr(d, "build_agent", lambda **kwargs: SimpleNamespace(ainvoke=AsyncMock(side_effect=ValueError("model failed"))))
    with pytest.raises(ValueError, match="model failed"):
        asyncio.run(d.run_task("thread", "build", project_root=tmp_path,
                              settings=SandboxSettings(daytona_api_key="test", _env_file=None)))
    save.assert_called_once()


def test_cancel_waits_for_backup_and_unlock(tmp_path, monkeypatch):
    from threading import Event
    running = Event()
    order = []
    @contextmanager
    def session(*args, **kwargs):
        order.append("lock")
        try:
            yield Mock(), SimpleNamespace(id="sandbox"), Mock(), "/workspace"
        finally:
            order.append("unlock")
    async def invoke(*args, **kwargs):
        running.set()
        await asyncio.sleep(20)
    monkeypatch.setattr(d, "sandbox_for_thread", session)
    monkeypatch.setattr(d, "save_session_workspace", lambda *args: order.append("save"))
    monkeypatch.setattr(d, "build_agent", lambda **kwargs: SimpleNamespace(ainvoke=invoke))
    async def run():
        task = asyncio.create_task(d.run_task("thread", "build", project_root=tmp_path,
                                             settings=SandboxSettings(daytona_api_key="test", _env_file=None)))
        assert await asyncio.to_thread(running.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert order == ["lock", "save", "unlock"]
    asyncio.run(run())


def test_dependency_warning_reaches_agent(tmp_path, monkeypatch):
    @contextmanager
    def session(*args, **kwargs):
        yield Mock(), SimpleNamespace(id="sandbox"), Mock(), "/workspace"
    invoke = AsyncMock(return_value={"messages": [SimpleNamespace(content="fixed")]})
    monkeypatch.setattr(d, "sandbox_for_thread", session)
    monkeypatch.setattr(d, "save_session_workspace", lambda *args: "version")
    monkeypatch.setattr(d, "_sql", lambda *args: [{"dependency_warning": "依赖安装失败 exit_code=1"}])
    monkeypatch.setattr(d, "build_agent", lambda **kwargs: SimpleNamespace(ainvoke=invoke))
    result = asyncio.run(d.run_task("thread", "修复依赖", project_root=tmp_path,
                                   settings=SandboxSettings(daytona_api_key="test", _env_file=None)))
    assert result["workspace_saved"]
    prompt = invoke.call_args.args[0]["messages"][0]["content"]
    assert "修复依赖" in prompt and "exit_code=1" in prompt


def test_verification_options_reach_graph_without_changing_default_response(tmp_path, monkeypatch):
    @contextmanager
    def session(*args, **kwargs):
        yield Mock(), SimpleNamespace(id="sandbox"), Mock(), "/workspace"
    state = {"messages": [SimpleNamespace(content="done")]}
    invoke = AsyncMock(return_value=state)
    builder = Mock(return_value=SimpleNamespace(ainvoke=invoke))
    monkeypatch.setattr(d, "sandbox_for_thread", session)
    monkeypatch.setattr(d, "save_session_workspace", lambda *args: "version")
    monkeypatch.setattr(d, "build_agent", builder)
    callback = Mock()
    result = asyncio.run(d.run_task("thread", "test", project_root=tmp_path,
        settings=SandboxSettings(daytona_api_key="test", _env_file=None),
        callbacks=[callback], include_state=True, max_model_calls=6, max_subagent_model_calls=8))
    assert result["agent_state"] is state
    assert invoke.call_args.kwargs["config"]["callbacks"] == [callback]
    assert builder.call_args.kwargs["max_model_calls"] == 6
    assert builder.call_args.kwargs["max_subagent_model_calls"] == 8
