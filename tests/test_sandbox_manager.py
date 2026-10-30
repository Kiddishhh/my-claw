"""模拟 provider 验证生命周期，不创建付费资源；另用 CLI 做真实验收。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from daytona import DaytonaNotFoundError

from infra.settings import SandboxSettings
from sandbox import manager as m


@pytest.fixture
def environment(tmp_path):
    for name in m.EXTRA_FILES:
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/main.py").write_text("print('hello')", encoding="utf-8")
    client = Mock()
    boxes = {}
    client.list.return_value = []

    def create(params, **kwargs):
        box = Mock(id=f"sandbox-{len(boxes)}", state="started")
        box.get_work_dir.return_value = "/home/test"
        boxes[box.id] = box
        return box

    client.create.side_effect = create
    client.get.side_effect = lambda identity: boxes[identity]
    backend = Mock()
    backend.execute.return_value = SimpleNamespace(exit_code=0, output="ok")
    backend.upload_files.side_effect = lambda files: [SimpleNamespace(error=None) for _ in files]
    factory = Mock(return_value=backend)
    settings = SandboxSettings(daytona_api_key="unit-test-key", _env_file=None, syc_sandbox_idle_seconds=10)
    options = dict(project_root=tmp_path, settings=settings, client=client, backend_factory=factory)
    return tmp_path, client, backend, options


def test_reuse_restart_and_user_isolation(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "issue", **options) as first:
        identity = first[1].id
    # 只依赖 SQLite，不依赖进程内缓存；第二轮不重复上传。
    with m.sandbox_for_thread("alice", "issue", **dict(options)) as second:
        assert second[1].id == identity
    with m.sandbox_for_thread("bob", "issue", **options) as third:
        assert third[1].id != identity
    assert client.create.call_count == backend.upload_files.call_count == 2
    assert len(m._sql(root, "SELECT * FROM sessions")) == 2
    assert client.create.call_args.args[0].env_vars == m.SAFE_ENV


def test_stopped_resume_syncs_dependencies(environment):
    _, client, backend, options = environment
    with m.sandbox_for_thread("alice", "issue", **options) as first:
        box = first[1]
    box.state = "stopped"
    before = backend.execute.call_count
    with m.sandbox_for_thread("alice", "issue", **options):
        pass
    box.start.assert_called_once_with(timeout=120)
    assert backend.execute.call_count > before
    assert client.create.call_count == 1


def test_cleanup_skips_active_and_retries(environment):
    root, client, _, options = environment
    cleanup_options = {k: v for k, v in options.items() if k != "backend_factory"}
    with m.sandbox_for_thread("alice", "issue", **options):
        m._sql(root, "UPDATE sessions SET last_used_at=0")
        assert m.cleanup_idle_sandboxes(**cleanup_options)["busy"] == 1
        client.delete.assert_not_called()
    m._sql(root, "UPDATE sessions SET last_used_at=0")
    client.delete.side_effect = RuntimeError("network")
    assert m.cleanup_idle_sandboxes(**cleanup_options)["failed"] == 1
    assert m._sql(root, "SELECT * FROM sessions")
    client.delete.side_effect = None
    assert m.cleanup_idle_sandboxes(**cleanup_options)["deleted"] == 1
    assert not m._sql(root, "SELECT * FROM sessions")


@pytest.mark.parametrize("error", [RuntimeError("network"), DaytonaNotFoundError("missing")])
def test_get_failure_never_creates_replacement(environment, error):
    _, client, _, options = environment
    with m.sandbox_for_thread("alice", "issue", **options):
        pass
    client.get.side_effect = error
    with pytest.raises(Exception):
        with m.sandbox_for_thread("alice", "issue", **options):
            pass
    assert client.create.call_count == 1


def test_upload_failure_cleanup_failure_is_recorded(environment):
    root, client, backend, options = environment
    backend.upload_files.side_effect = lambda files: [SimpleNamespace(error="denied") for _ in files]
    client.delete.side_effect = RuntimeError("network")
    with pytest.raises(RuntimeError, match="上传"):
        with m.sandbox_for_thread("alice", "issue", **options):
            pass
    assert m._sql(root, "SELECT * FROM sessions")[0]["status"] == "cleanup_pending"
    with pytest.raises(RuntimeError, match="初始化未完成"):
        with m.sandbox_for_thread("alice", "issue", **options):
            pass
    assert client.create.call_count == 1


def test_ambiguous_create_resolved_by_label_cleanup(environment):
    root, client, _, options = environment
    client.create.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        with m.sandbox_for_thread("alice", "issue", **options):
            pass
    row = m._sql(root, "SELECT * FROM sessions")[0]
    assert row["status"] == "cleanup_pending" and row["sandbox_id"] is None
    orphan = Mock(id="orphan")
    client.list.return_value = [orphan]
    assert m.cleanup_idle_sandboxes(**{k: v for k, v in options.items() if k != "backend_factory"})["deleted"] == 1
    client.delete.assert_called_once_with(orphan, wait=True, timeout=120)


def test_task_error_stops_sandbox(environment):
    root, _, _, options = environment
    with pytest.raises(ValueError, match="task failed"):
        with m.sandbox_for_thread("alice", "issue", **options) as resources:
            resources[1].stop.side_effect = RuntimeError("stop failed")
            raise ValueError("task failed")
    resources[1].stop.assert_called_once_with(timeout=120)
    assert m._sql(root, "SELECT * FROM sessions")[0]["status"] == "cleanup_pending"


def test_upload_manifest_allows_skills_but_excludes_credentials(environment):
    root, _, _, _ = environment
    skill = root / ".agents/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("safe skill", encoding="utf-8")
    (root / "agent/.env").write_text("SECRET=hidden", encoding="utf-8")
    (root / "agent/credentials.txt").write_text("hidden", encoding="utf-8")
    names = [name for name, _ in m._collect_project_files(root, "/remote")]
    assert "/remote/.agents/skills/example/SKILL.md" in names
    assert not any("credentials" in name or ".env" in name for name in names)


def test_known_secret_blocks_before_creation(environment, monkeypatch):
    root, client, _, options = environment
    monkeypatch.setenv("SYC_API_KEY", "known-private-key-value")
    (root / "agent/main.py").write_text("KEY='known-private-key-value'", encoding="utf-8")
    with pytest.raises(ValueError, match="密钥"):
        with m.sandbox_for_thread("alice", "issue", **options):
            pass
    client.create.assert_not_called()


def test_manifest_rejects_traversal(environment):
    root, _, _, _ = environment
    with pytest.raises(ValueError, match="相对路径"):
        m._collect_project_files(root, "/remote", subdirs=("../other",))
