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
    backend.execute.return_value = SimpleNamespace(exit_code=0, output="[]", truncated=False)
    backend.upload_files.side_effect = lambda files: [SimpleNamespace(error=None) for _ in files]
    factory = Mock(return_value=backend)
    settings = SandboxSettings(daytona_api_key="unit-test-key", _env_file=None, syc_sandbox_idle_seconds=10, syc_sandbox_seed_platform=True)
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


def test_stopped_resume_does_not_force_dependency_install(environment):
    _, client, backend, options = environment
    with m.sandbox_for_thread("alice", "issue", **options) as first:
        box = first[1]
    box.state = "stopped"
    before = backend.execute.call_count
    with m.sandbox_for_thread("alice", "issue", **options):
        pass
    box.start.assert_called_once_with(timeout=120)
    assert backend.execute.call_count == before
    assert client.create.call_count == 1


def test_cleanup_skips_active_and_retries(environment):
    root, client, _, options = environment
    cleanup_options = options
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
    assert m._sql(root, "SELECT * FROM sessions")[0]["status"] == "archived"


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


def test_default_empty_workspace_does_not_upload_platform(environment):
    root, client, backend, options = environment
    options["settings"] = options["settings"].model_copy(update={"syc_sandbox_seed_platform": False})
    with m.sandbox_for_thread("alice", "web", **options):
        pass
    backend.upload_files.assert_not_called()
    assert client.create.call_count == 1


def test_saved_workspace_survives_cleanup_and_recreates(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "web", **options) as first:
        first_id = first[1].id
        version = m.save_session_workspace(root, m._session_key("alice", "web"), backend, first[3])
    m._sql(root, "UPDATE sessions SET last_used_at=0")
    assert m.cleanup_idle_sandboxes(**options)["deleted"] == 1
    row = m._sql(root, "SELECT * FROM sessions")[0]
    assert row["snapshot_path"] and row["sandbox_id"] is None
    assert (root / version / "manifest.json").is_file()
    with m.sandbox_for_thread("alice", "web", **options) as second:
        assert second[1].id != first_id
    assert client.create.call_count == 2


def test_backup_failure_blocks_cleanup(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "web", **options):
        pass
    m._sql(root, "UPDATE sessions SET last_used_at=0")
    backend.execute.return_value = SimpleNamespace(exit_code=1, output="failed")
    assert m.cleanup_idle_sandboxes(**options)["failed"] == 1
    client.delete.assert_not_called()
    assert m._sql(root, "SELECT * FROM sessions")[0]["backup_status"] == "failed"


def test_confirmed_missing_restores_but_network_error_does_not(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "web", **options) as first:
        m.save_session_workspace(root, m._session_key("alice", "web"), backend, first[3])
    client.get.side_effect = RuntimeError("network")
    with pytest.raises(RuntimeError):
        m.get_or_create_sandbox_backend("web", **options, user_id="alice")
    assert client.create.call_count == 1
    client.get.side_effect = DaytonaNotFoundError("gone")
    m.get_or_create_sandbox_backend("web", **options, user_id="alice")
    assert client.create.call_count == 2


def test_missing_without_backup_never_becomes_empty_project_after_cleanup(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "web", **options):
        pass
    m._sql(root, "UPDATE sessions SET last_used_at=0")
    client.get.side_effect = DaytonaNotFoundError("gone")
    m.cleanup_idle_sandboxes(**options)
    with pytest.raises(RuntimeError, match="没有有效备份"):
        m.get_or_create_sandbox_backend("web", **options, user_id="alice")
    assert client.create.call_count == 1


def test_failed_restore_cleanup_preserves_last_good_snapshot(environment, monkeypatch):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "web", **options) as first:
        saved = m.save_session_workspace(root, m._session_key("alice", "web"), backend, first[3])
    m._sql(root, "UPDATE sessions SET sandbox_id=NULL,status='archived'")
    monkeypatch.setattr(m, "restore_workspace", Mock(side_effect=RuntimeError("upload interrupted")))
    client.delete.side_effect = RuntimeError("network")
    with pytest.raises(RuntimeError, match="upload interrupted"):
        m.get_or_create_sandbox_backend("web", **options, user_id="alice")
    row = m._sql(root, "SELECT * FROM sessions")[0]
    assert row["workdir"] is None and row["snapshot_path"] == saved
    client.delete.side_effect = None
    assert m.cleanup_idle_sandboxes(**options)["deleted"] == 1
    assert m._sql(root, "SELECT * FROM sessions")[0]["snapshot_path"] == saved


def test_retains_two_versions_after_successful_commit(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "web", **options) as resources:
        paths = [m.save_session_workspace(root, m._session_key("alice", "web"), backend, resources[3]) for _ in range(3)]
    assert not (root / paths[0]).exists()
    assert all((root / p).exists() for p in paths[1:])


def test_reuse_allows_agent_to_repair_broken_dependencies(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "repair", **options):
        pass
    backend.execute.return_value = SimpleNamespace(exit_code=1, output="lockfile mismatch")
    with m.sandbox_for_thread("alice", "repair", **options):
        pass  # 不应在 Agent 运行前被安装失败阻断。


def test_restored_dependency_error_keeps_workspace_accessible(environment):
    root, client, backend, options = environment
    with m.sandbox_for_thread("alice", "restore", **options) as resources:
        m.save_session_workspace(root, m._session_key("alice", "restore"), backend, resources[3])
    m._sql(root, "UPDATE sessions SET sandbox_id=NULL,status='archived'")
    backend.execute.side_effect = lambda cmd: SimpleNamespace(
        exit_code=1 if "npm ci" in cmd else 0, output="lock mismatch", truncated=False)
    with m.sandbox_for_thread("alice", "restore", **options) as resources:
        assert resources[3]
        row = m._sql(root, "SELECT * FROM sessions")[0]
        assert row["status"] == "ready"
        assert "exit_code=1" in row["dependency_warning"]
    client.delete.assert_not_called()
