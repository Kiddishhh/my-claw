"""工作区持久化行为：下载失败不替换旧备份，恢复包括删除语义。"""
import json
import hashlib
from types import SimpleNamespace

import pytest

from sandbox.workspace import save_workspace, restore_workspace


class Backend:
    def __init__(self):
        self.files = {"src/main.js": b"hello", "public/logo.png": b"PNG"}
        self.fail = False
        self.uploaded = []

    def execute(self, command):
        if command.startswith("python -c"):
            items = [{"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                     for p, b in sorted(self.files.items())]
            return SimpleNamespace(exit_code=0, output=json.dumps(items), truncated=False)
        return SimpleNamespace(exit_code=0, output="")

    def download_files(self, paths):
        return [SimpleNamespace(path=p, content=None if self.fail else self.files[p.removeprefix('/work/')],
                                error="failed" if self.fail else None) for p in paths]

    def upload_files(self, files):
        self.uploaded.extend(files)
        return [SimpleNamespace(error=None) for _ in files]


def test_roundtrip_deletions_and_failed_save(tmp_path):
    backend = Backend()
    first = save_workspace(backend, "/work", tmp_path, "a" * 64)
    del backend.files["public/logo.png"]
    backend.files["src/main.js"] = b"new"
    second = save_workspace(backend, "/work", tmp_path, "a" * 64)
    assert first != second
    backend.fail = True
    with pytest.raises(RuntimeError):
        save_workspace(backend, "/work", tmp_path, "a" * 64)
    restore_workspace(backend, "/work", tmp_path, second)
    assert backend.uploaded == [("/work/src/main.js", b"new")]
    backend.uploaded.clear()
    restore_workspace(backend, "/work", tmp_path, first)
    assert len(backend.uploaded) == 2


def test_rejects_path_escape_and_corrupt_backup(tmp_path):
    backend = Backend()
    backend.files = {"../escape": b"bad"}
    with pytest.raises(ValueError):
        save_workspace(backend, "/work", tmp_path, "b" * 64)
    backend.files = {"index.html": b"ok"}
    saved = save_workspace(backend, "/work", tmp_path, "b" * 64)
    (tmp_path / saved / "project/index.html").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        restore_workspace(backend, "/work", tmp_path, saved)


def test_real_scan_keeps_sources_but_not_runtime_outputs(tmp_path):
    """实际执行扫描程序（无云端、无模型），避免只验证模拟清单。"""
    import shlex
    import subprocess
    import sys
    from sandbox.workspace import _scan
    class Scanner:
        def execute(self, command):
            args = shlex.split(command)
            args[0] = sys.executable
            result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=15)
            return SimpleNamespace(exit_code=result.returncode, output=result.stdout, truncated=False)
    (tmp_path / "index.html").write_text("hello", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE=value")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/dependency.js").write_text("ignore")
    (tmp_path / "credential-form.tsx").write_text("component")
    (tmp_path / "empty").mkdir()
    for rel in ("dist/index.html", "outputs/report.csv", "logs/terminal.txt", "coverage/report.html", "pytest_result.txt", "debug.log"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime")
    for rel in ("package.json", "package-lock.json", "public/logo.svg", "src/build/helper.js"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source")
    names = {x["path"] for x in _scan(Scanner(), str(tmp_path))}
    assert {"index.html", ".env", "credential-form.tsx", "empty", "package.json", "package-lock.json", "public", "public/logo.svg", "src", "src/build", "src/build/helper.js"} == names
    with pytest.raises(RuntimeError):
        _scan(Scanner(), str(tmp_path / "missing"))


def test_directory_link_and_executable_metadata_roundtrip(tmp_path, monkeypatch):
    from sandbox import workspace as w
    backend = Backend()
    backend.files = {"bin/build.sh": b"#!/bin/sh\necho ok"}
    data = backend.files["bin/build.sh"]
    items = [
        {"path": "bin", "kind": "directory", "mode": 0o755},
        {"path": "bin/build.sh", "kind": "file", "mode": 0o755,
         "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        {"path": "empty", "kind": "directory", "mode": 0o755},
        {"path": "run-build", "kind": "symlink", "mode": 0o777, "target": "bin/build.sh"},
    ]
    monkeypatch.setattr(w, "_scan", lambda *args: items)
    saved = save_workspace(backend, "/work", tmp_path, "c" * 64)
    assert (tmp_path / saved / "project/empty").is_dir()
    assert not (tmp_path / saved / "project/run-build").exists()
    commands = []
    monkeypatch.setattr(backend, "execute", lambda cmd: (commands.append(cmd) or SimpleNamespace(exit_code=0)))
    restore_workspace(backend, "/work", tmp_path, saved)
    assert "ln -s -- bin/build.sh /work/run-build" in commands
    assert "chmod 755 -- /work/bin/build.sh" in commands
    assert backend.uploaded == [("/work/bin/build.sh", data)]
    with pytest.raises(ValueError, match="链接越出"):
        w._validate_manifest([{"path": "escape", "kind": "symlink", "target": "../host", "mode": 0o777}])


def test_old_full_snapshot_restores_only_sources(tmp_path):
    backend = Backend()
    backend.files = {"src/main.js": b"source", "dist/index.html": b"built", "logs/terminal.txt": b"log"}
    # 模拟先前已保存的全量版本，不能重新恢复历史运行产物。
    manifest = []
    version = tmp_path / "legacy"
    for name, content in backend.files.items():
        path = version / "project" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest.append({"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    (version / "manifest.json").write_text(json.dumps(manifest))
    restore_workspace(backend, "/work", tmp_path, "legacy")
    assert backend.uploaded == [("/work/src/main.js", b"source")]
