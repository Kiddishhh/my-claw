"""会话管理回归：只用临时磁盘和假远程对象，不删除真实数据。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from infra.async_runtime import run_async


def test_delete_removes_only_target_and_retries(tmp_path):
    from agent.sessions import delete_session
    from sandbox.manager import _sql, session_key
    key = session_key('local-user', 'one')
    other = session_key('local-user', 'two')
    for k in (key, other):
        _sql(tmp_path, "INSERT INTO sessions(session_key,status,last_used_at) VALUES(?,'archived',0)", (k,))
        folder = tmp_path / '.sandbox-state' / 'workspaces' / k
        folder.mkdir(parents=True)
        (folder / 'source.txt').write_text('keep')
    saver = SimpleNamespace(adelete_thread=AsyncMock(side_effect=[RuntimeError('offline'), None, None]))
    store = SimpleNamespace(asearch=AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError):
        run_async(delete_session('one', checkpointer=saver, store=store, project_root=tmp_path))
    assert _sql(tmp_path, 'SELECT status FROM sessions WHERE session_key=?', (key,))[0]['status'].startswith('deleting')
    run_async(delete_session('one', checkpointer=saver, store=store, project_root=tmp_path))
    run_async(delete_session('one', checkpointer=saver, store=store, project_root=tmp_path))
    assert not (tmp_path / '.sandbox-state' / 'workspaces' / key).exists()
    assert (tmp_path / '.sandbox-state' / 'workspaces' / other / 'source.txt').exists()
    assert not _sql(tmp_path, 'SELECT * FROM sessions WHERE session_key=?', (key,))
    assert saver.adelete_thread.await_args.args == (key,)


def test_delete_busy_does_not_touch_checkpoint(tmp_path):
    from agent.sessions import delete_session
    from sandbox.manager import _lock, session_key
    from filelock import Timeout
    saver = SimpleNamespace(adelete_thread=AsyncMock())
    with _lock(tmp_path, session_key('local-user', 'busy')):
        with pytest.raises(Timeout):
            run_async(delete_session('busy', checkpointer=saver, store=Mock(), project_root=tmp_path))
    saver.adelete_thread.assert_not_awaited()


def test_upload_name_is_portable_and_unique():
    from agent.sessions import upload_name
    for value in ['../../.env', r'C:\secret\x.csv', 'NUL', 'a.log', '销售数据.xlsx', '中文'*150+'.csv']:
        name = upload_name(value)
        assert '/' not in name and '\\' not in name and ':' not in name
        assert name != upload_name(value)
        assert len(name.encode('utf-8')) <= 255


def test_history_hides_tools_and_reasoning():
    from agent.sessions import read_history
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={'channel_values': {'messages': [
        HumanMessage(content='hi'), ToolMessage(content='secret', tool_call_id='x'),
        AIMessage(content=[{'type':'reasoning','text':'private'}, {'type':'text','text':'hello'}]),
    ]}})))
    assert run_async(read_history('one', checkpointer=saver)) == [{'role':'user','text':'hi'}, {'role':'assistant','text':'hello'}]


def test_upload_reuses_session_and_saves_before_return(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agent import sessions as service
    events = []
    backend = Mock()
    backend.execute.return_value = SimpleNamespace(exit_code=0)
    backend.upload_files.return_value = [SimpleNamespace(error=None)]
    @contextmanager
    def context(*args, **kwargs):
        events.append('locked')
        yield backend, None, None, '/workspace'
        events.append('released')
    monkeypatch.setattr(service, 'sandbox_for_thread', context)
    monkeypatch.setattr(service, 'save_session_workspace', lambda *args: events.append('saved'))
    result = run_async(service.upload_file('one','../../sales.csv',b'a,b', project_root=tmp_path))
    assert result['path'].startswith('/workspace/inputs/') and result['path'].endswith('sales.csv')
    assert backend.upload_files.call_args.args[0] == [(result['path'],b'a,b')]
    assert events == ['locked','saved','released']
    monkeypatch.setattr(service, 'save_session_workspace', Mock(side_effect=RuntimeError('disk full')))
    with pytest.raises(RuntimeError):
        run_async(service.upload_file('one','x',b'x',project_root=tmp_path))


def test_remote_failure_retains_local_data_and_blocks_restore(tmp_path):
    from agent.sessions import delete_session
    from sandbox.manager import _sql, session_key, _get_or_create_locked
    key = session_key('local-user','one')
    _sql(tmp_path, "INSERT INTO sessions(session_key,sandbox_id,status,last_used_at) VALUES(?,'remote','ready',0)",(key,))
    client = Mock()
    client.delete.side_effect = RuntimeError('network')
    saver = SimpleNamespace(adelete_thread=AsyncMock())
    with pytest.raises(RuntimeError):
        run_async(delete_session('one',checkpointer=saver,store=Mock(),project_root=tmp_path,client=client))
    saver.adelete_thread.assert_not_awaited()
    assert _sql(tmp_path,'SELECT status FROM sessions WHERE session_key=?',(key,))[0]['status'] == 'deleting'
    with pytest.raises(RuntimeError):
        _get_or_create_locked('one',tmp_path,'local-user',Mock(),client,Mock())


def test_finish_rejects_link_to_other_folder(tmp_path):
    from sandbox.manager import finish_session_deletion, session_key
    import os
    outside = tmp_path / 'other'
    outside.mkdir()
    (outside/'keep').write_text('safe')
    key = session_key('local-user','one')
    target = tmp_path / '.sandbox-state' / 'workspaces' / key
    target.parent.mkdir(parents=True)
    try:
        os.symlink(outside,target,target_is_directory=True)
    except OSError:
        pytest.skip('当前 Windows 身份不能创建 symlink')
    with pytest.raises(ValueError):
        finish_session_deletion(tmp_path,key)
    assert (outside/'keep').read_text() == 'safe'


def test_pending_cloud_creation_is_not_forgotten(tmp_path):
    import time
    from sandbox.manager import _sql, session_key, delete_session_sandbox
    key = session_key('local-user','pending')
    _sql(tmp_path, "INSERT INTO sessions(session_key,status,last_used_at) VALUES(?,'deleting',?)",(key,time.time()))
    client = Mock()
    client.list.return_value = []
    with pytest.raises(RuntimeError, match='尚未确定'):
        delete_session_sandbox(tmp_path,key,settings=SimpleNamespace(syc_sandbox_idle_seconds=3600),client=client)
    assert _sql(tmp_path,'SELECT status FROM sessions WHERE session_key=?',(key,))[0]['status'] == 'deleting'
