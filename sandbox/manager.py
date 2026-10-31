"""Daytona 的会话生命周期管理：官方 SDK + 后端适配器，Agent 留在本机。

三个核心函数分别收集文件、初始化环境、获取/创建后端；sandbox_for_thread
在整个任务期间持有跨进程文件锁。SQLite 只保存资源元数据，不保存任何密钥。
适用于同一台主机、同一状态目录；不是多台服务器的分布式协调方案。
"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import os
import re
import shlex
import sqlite3
import time
from typing import Callable, Iterator

from daytona import Daytona, DaytonaConfig, DaytonaNotFoundError, CreateSandboxFromSnapshotParams, ListSandboxesQuery, Sandbox
from dotenv import dotenv_values
from filelock import FileLock, Timeout
from langchain_daytona import DaytonaSandbox

from infra.logging import get_logger
from infra.settings import SandboxSettings
from sandbox.workspace import save_workspace, restore_workspace, prune_versions

logger = get_logger()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 明确选择项目源码目录。包含 subagents 是为满足既有导入，不代表启用委派。
SOURCE_DIRS = ("agent", "infra", "middleware", "tools", "subagents", "sandbox", "scripts", "tests", ".agents/skills")
EXTRA_FILES = ("AGENTS.md", "pyproject.toml", "uv.lock", ".python-version")
SAFE_ENV = {"APP_ENV": "test", "PYTHONUNBUFFERED": "1"}


def _session_key(user_id: str, thread_id: str) -> str:
    if not user_id.strip() or not thread_id.strip():
        raise ValueError("user_id 和 thread_id 不能为空")
    # 用 JSON 避免拼接歧义；哈希作为 ASCII 标签和锁文件名，不是鉴权凭据。
    return hashlib.sha256(json.dumps([user_id, thread_id]).encode()).hexdigest()


def _sql(root: Path, statement: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    directory = root / ".sandbox-state"
    directory.mkdir(exist_ok=True, parents=True)
    # sqlite 的连接上下文只提交/回滚事务，closing 才保证连接真正关闭。
    with closing(sqlite3.connect(directory / "sessions.sqlite3", timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        # 多个会话首次启动也可能并发迁移，先取得写事务，避免重复 ALTER。
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_key TEXT PRIMARY KEY, sandbox_id TEXT, workdir TEXT,
            status TEXT NOT NULL, last_used_at REAL NOT NULL)""")
        # 原地、幂等迁移，保留旧沙箱映射；不读取或修改用户项目文件。
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        for name, kind in (("snapshot_path", "TEXT"), ("saved_at", "REAL"),
                           ("backup_status", "TEXT"), ("backup_error", "TEXT"),
                           ("dependency_warning", "TEXT")):
            if name not in columns:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
        return db.execute(statement, parameters).fetchall()


def save_session_workspace(root: Path, key: str, backend: DaytonaSandbox, workdir: str) -> str:
    """持锁调用：文件落盘后再更新 SQLite 指针；失败保留上一个完整版本。"""
    try:
        version = save_workspace(backend, workdir, root, key)
        _sql(root, "UPDATE sessions SET snapshot_path=?,saved_at=?,backup_status='saved',backup_error=NULL WHERE session_key=?",
             (version, time.time(), key))
        try:
            prune_versions(root, key, version)
        except Exception as error:
            logger.warning("旧备份清理失败，最新版本已安全保存：{}", type(error).__name__)
        return version
    except Exception as error:
        _sql(root, "UPDATE sessions SET backup_status='failed',backup_error=? WHERE session_key=?",
             (type(error).__name__, key))
        raise


def _lock(root: Path, key: str, timeout: float = 30) -> FileLock:
    directory = root / ".sandbox-state"
    directory.mkdir(exist_ok=True, parents=True)
    return FileLock(str(directory / f"{key}.lock"), timeout=timeout)


def create_daytona_client(settings: SandboxSettings) -> Daytona:
    """只把 Daytona 管理密钥交给本机 SDK；不加载到远程环境。"""
    return Daytona(DaytonaConfig(
        api_key=settings.daytona_api_key.get_secret_value(),
        api_url=settings.daytona_api_url,
    ))


def _collect_project_files(
    root: Path, dest_root: str, subdirs: tuple[str, ...] = SOURCE_DIRS,
    extra_files: tuple[str, ...] = EXTRA_FILES,
) -> list[tuple[str, bytes]]:
    """上传前一次性检查所有候选文件；返回远程绝对路径和原始字节。

    不扫描/上传整个磁盘。允许 .agents/skills，但拒绝环境文件、凭据文件、
    缓存和链接。检测已配置密钥及常见私钥格式，不承诺识别任意编码的秘密。
    """
    root = root.resolve()
    remote = PurePosixPath(dest_root)
    if not remote.is_absolute() or ".." in remote.parts:
        raise ValueError("远程目录必须是规范绝对路径")
    env = dict(dotenv_values(PROJECT_ROOT / ".env")) | dict(dotenv_values(root / ".env")) | dict(os.environ)
    known = [str(value).encode() for name, value in env.items() if value and len(str(value)) >= 8
             and re.search(r"KEY|TOKEN|SECRET|PASSWORD|POSTGRES_URL|REDIS_URL", name, re.I)]
    candidates: set[Path] = set()
    for name in (*subdirs, *extra_files):
        rel = PurePosixPath(name.replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or ":" in str(rel):
            raise ValueError("上传清单只接受项目相对路径")
        path = root.joinpath(*rel.parts)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("上传清单包含链接或越界路径")
        if path.is_dir():
            candidates.update(path.rglob("*"))
        elif path.is_file():
            candidates.add(path)
        elif name in extra_files:
            raise ValueError(f"缺少初始化必需文件：{name}")
    result: list[tuple[str, bytes]] = []
    total = 0
    for path in sorted(candidates):
        rel = path.relative_to(root)
        # 任意一级目录链接都拒绝，避免 Windows junction 等间接越界。
        if any(part.is_symlink() for part in (path, *path.parents) if part != root) or not path.resolve().is_relative_to(root):
            raise ValueError("上传内容包含链接或越界路径")
        if not path.is_file():
            continue
        if any(p in {".git", ".venv", "__pycache__", ".sandbox-state", "logs"}
               or p.startswith(".env") or re.search(r"credential|secret|id_rsa|id_ed25519", p, re.I)
               for p in rel.parts):
            continue
        if path.suffix.lower() not in {".py", ".md", ".toml", ".lock", ".txt"} and path.name != ".python-version":
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("候选文件超过 5 MiB 上限")
        content = path.read_bytes()
        if any(value in content for value in known) or re.search(
            rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}|\blsv2_[A-Za-z0-9_]{20,}", content,
        ):
            raise ValueError("候选文件含疑似密钥，已阻止整批上传")
        total += len(content)
        if total > 30 * 1024 * 1024:
            raise ValueError("上传文件总量超过 30 MiB")
        result.append((str(remote / rel.as_posix()), content))
    if not result:
        raise ValueError("没有可上传的项目文件")
    return result


def _execute(backend: DaytonaSandbox, command: str) -> None:
    result = backend.execute(command)
    if result.exit_code != 0:
        # 安装日志可能包含索引凭据；这里仅记录退出码，不回显原始命令输出。
        raise RuntimeError(f"远程初始化命令失败，exit_code={result.exit_code}")


def _sync_dependencies(backend: DaytonaSandbox, workdir: str) -> str | None:
    """首次准备/恢复时尝试安装；项目安装失败是可修复诊断，不阻断 Agent。

只返回退出码，不将可能包含凭据的安装输出写入数据库或提示词。
连接异常仍向外抛出，避免将不可访问的沙箱标记为可用。
"""
    quoted = shlex.quote(workdir)
    result = backend.execute(f"cd {quoted} && if [ -f package-lock.json ]; then npm ci; "
             "elif [ -f pnpm-lock.yaml ]; then corepack pnpm install --frozen-lockfile; "
             "elif [ -f yarn.lock ]; then corepack yarn install --immutable; "
             "elif [ -f package.json ]; then npm install; "
             "elif [ -f uv.lock ]; then python -m uv sync --locked; fi")
    if result.exit_code != 0:
        return f"上次准备工作区时依赖安装失败，exit_code={result.exit_code}。文件可访问；请检查项目依赖与锁文件，按需修复后验证构建。当前依赖状态需重新核实。"
    return None


def seed_project_into_sandbox(backend: DaytonaSandbox, sandbox: Sandbox, root: Path) -> str:
    """显式平台自测首次上传；调用方另行尝试安装，不把安装失败当成文件不可用。"""
    base = PurePosixPath(sandbox.get_work_dir())
    if not base.is_absolute() or ".." in base.parts:
        raise ValueError("沙箱未返回有效工作目录")
    workdir = str(base / "my-claw")
    files = _collect_project_files(root, workdir)
    parents = {str(PurePosixPath(path).parent) for path, _ in files}
    _execute(backend, "mkdir -p " + " ".join(shlex.quote(p) for p in sorted(parents)))
    results = backend.upload_files(files)
    if len(results) != len(files) or any(item.error for item in results):
        raise RuntimeError("沙箱文件上传未全部成功")
    logger.info("沙箱初始化完成：上传 {} 个文件", len(files))
    return workdir


def _labels(key: str) -> dict[str, str]:
    return {"my_claw_session": key}


def _get_or_create_locked(
    thread_id: str, root: Path, user_id: str, settings: SandboxSettings,
    client: Daytona, backend_factory: Callable[..., DaytonaSandbox],
) -> tuple[DaytonaSandbox, Sandbox, Daytona, str]:
    key = _session_key(user_id, thread_id)
    rows = _sql(root, "SELECT * FROM sessions WHERE session_key=?", (key,))
    snapshot = rows[0]["snapshot_path"] if rows else None
    if rows and rows[0]["status"] == "archived" and not snapshot:
        raise RuntimeError("原沙箱已回收但没有有效备份，需要人工处理，不能创建空项目冒充恢复")
    if rows and rows[0]["status"] != "archived":
        row = rows[0]
        if row["status"] != "ready":
            raise RuntimeError("此会话初始化未完成或等待回收，请先运行回收程序；不会自动创建重复沙箱")
        try:
            sandbox = client.get(row["sandbox_id"])
        except DaytonaNotFoundError:
            if not snapshot:
                raise RuntimeError("原沙箱已删除且没有有效磁盘备份，不能自动恢复") from None
            _sql(root, "UPDATE sessions SET sandbox_id=NULL,status='archived' WHERE session_key=?", (key,))
        else:
            state = getattr(sandbox.state, "value", sandbox.state)
            if state == "stopped":
                sandbox.start(timeout=120)
            elif state != "started":
                raise RuntimeError(f"沙箱暂不可复用：{state}")
            backend = backend_factory(sandbox=sandbox, timeout=settings.syc_sandbox_command_timeout)
            # 不把依赖正确作为允许进入 Agent 的前提；也不每轮重装 node_modules。
            logger.info("复用会话沙箱：{}", sandbox.id)
            return backend, sandbox, client, row["workdir"]

    # 创建之前验证全部文件，避免密钥检查失败后仍产生云端资源。
    if settings.syc_sandbox_seed_platform and not snapshot:
        _collect_project_files(root, "/preflight")
    # 先写 initializing：即使 create 请求超时但云端已创建，下次也不会盲目再建。
    _sql(root, "INSERT INTO sessions(session_key,status,last_used_at) VALUES(?,'initializing',?) "
         "ON CONFLICT(session_key) DO UPDATE SET status='initializing',workdir=NULL,last_used_at=excluded.last_used_at", (key, time.time()))
    sandbox = None
    try:
        # SDK 0.227 使用 ListSandboxesQuery，而不是旧示例的 list(labels=...)。
        # 映射意外丢失而标签还在时不认领未知状态，交由回收/人工处理。
        existing = list(client.list(ListSandboxesQuery(labels=_labels(key)), request_timeout=30))
        if existing:
            raise RuntimeError("存在未登记的同会话沙箱，请先运行回收程序检查")
        sandbox = client.create(CreateSandboxFromSnapshotParams(
            labels=_labels(key), env_vars=dict(SAFE_ENV),
            auto_stop_interval=settings.syc_sandbox_auto_stop_minutes,
            auto_delete_interval=settings.syc_sandbox_auto_delete_minutes,
        ), timeout=120)
        _sql(root, "UPDATE sessions SET sandbox_id=? WHERE session_key=?", (sandbox.id, key))
        backend = backend_factory(sandbox=sandbox, timeout=settings.syc_sandbox_command_timeout)
        if settings.syc_sandbox_seed_platform and not snapshot:
            # 仅保留显式开发验收兼容入口；默认不向沙箱上传平台仓库。
            workdir = seed_project_into_sandbox(backend, sandbox, root)
        else:
            base = PurePosixPath(sandbox.get_work_dir())
            if not base.is_absolute() or ".." in base.parts:
                raise ValueError("无效沙箱工作目录")
            workdir = str(base / "workspace")
            _execute(backend, f"mkdir -p {shlex.quote(workdir)}")
            if snapshot:
                restore_workspace(backend, workdir, root, snapshot)
            # 使用具备 Node/Python 的 Daytona 镜像；缺失解释器时明确失败。
            _execute(backend, "node --version && npm --version && python --version")
        warning = _sync_dependencies(backend, workdir)
        _sql(root, "UPDATE sessions SET dependency_warning=? WHERE session_key=?", (warning, key))
        if warning:
            logger.warning("项目依赖未准备好，交由 Agent 修复；沙箱保持可访问")
        _sql(root, "UPDATE sessions SET workdir=?,status='ready',last_used_at=? WHERE session_key=?", (workdir, time.time(), key))
        return backend, sandbox, client, workdir
    except Exception:
        _sql(root, "UPDATE sessions SET status='cleanup_pending' WHERE session_key=?", (key,))
        if sandbox is not None:
            try:
                client.delete(sandbox, wait=True, timeout=120)
                if snapshot:
                    _sql(root, "UPDATE sessions SET sandbox_id=NULL,status='archived' WHERE session_key=?", (key,))
                else:
                    _sql(root, "DELETE FROM sessions WHERE session_key=?", (key,))
            except Exception as cleanup_error:
                logger.error("初始化失败且清理待重试：sandbox={} error={}", sandbox.id, type(cleanup_error).__name__)
        raise


def get_or_create_sandbox_backend(
    thread_id: str, project_root: Path | None = None, *, user_id: str = "local-user",
    settings: SandboxSettings | None = None, client: Daytona | None = None,
    backend_factory: Callable[..., DaytonaSandbox] = DaytonaSandbox,
) -> tuple[DaytonaSandbox, Sandbox, Daytona, str]:
    """兼容原示例的四元组入口；只保护准备阶段，运行任务优先用 sandbox_for_thread。"""
    root = (project_root or PROJECT_ROOT).resolve()
    settings = settings or SandboxSettings()
    client = client or create_daytona_client(settings)
    key = _session_key(user_id, thread_id)
    with _lock(root, key):
        result = _get_or_create_locked(thread_id, root, user_id, settings, client, backend_factory)
        _sql(root, "UPDATE sessions SET last_used_at=? WHERE session_key=?", (time.time(), key))
        return result


@contextmanager
def sandbox_for_thread(
    user_id: str, thread_id: str, project_root: Path | None = None, *,
    settings: SandboxSettings | None = None, client: Daytona | None = None,
    backend_factory: Callable[..., DaytonaSandbox] = DaytonaSandbox,
) -> Iterator[tuple[DaytonaSandbox, Sandbox, Daytona, str]]:
    """会话锁覆盖准备和整个 Agent 运行；异常先停止沙箱，再释放锁。

    不把 sandbox.delete 放进正常 finally：会话下轮需要保留已有文件。
    进程被强杀时 finally 不运行，文件锁由 OS 释放，平台回收作为兜底。
    """
    root = (project_root or PROJECT_ROOT).resolve()
    settings = settings or SandboxSettings()
    client = client or create_daytona_client(settings)
    key = _session_key(user_id, thread_id)
    with _lock(root, key):
        backend, sandbox, client, workdir = _get_or_create_locked(thread_id, root, user_id, settings, client, backend_factory)
        try:
            yield backend, sandbox, client, workdir
        except BaseException:
            try:
                sandbox.stop(timeout=120)
            except Exception as error:
                # 停止失败不可安全复用：下一轮交给回收程序，避免两个任务重叠。
                _sql(root, "UPDATE sessions SET status='cleanup_pending' WHERE session_key=?", (key,))
                logger.error("沙箱停止失败，已标记回收：{}", type(error).__name__)
            raise
        finally:
            _sql(root, "UPDATE sessions SET last_used_at=? WHERE session_key=?", (time.time(), key))


def cleanup_idle_sandboxes(
    project_root: Path | None = None, *, settings: SandboxSettings | None = None,
    client: Daytona | None = None, backend_factory: Callable[..., DaytonaSandbox] = DaytonaSandbox,
) -> dict[str, int]:
    """周期任务调用：跳过持锁会话，失败保留记录；云端 404 视为已删除。"""
    root = (project_root or PROJECT_ROOT).resolve()
    settings = settings or SandboxSettings()
    client = client or create_daytona_client(settings)
    counts = {"deleted": 0, "busy": 0, "failed": 0}
    cutoff = time.time() - settings.syc_sandbox_idle_seconds
    rows = _sql(root, "SELECT * FROM sessions WHERE status!='archived' AND (last_used_at<? OR status!='ready')", (cutoff,))
    for row in rows:
        key = row["session_key"]
        try:
            with _lock(root, key, timeout=0):
                current = _sql(root, "SELECT * FROM sessions WHERE session_key=?", (key,))
                if not current:
                    continue
                item = current[0]
                if item["status"] == "archived":
                    continue
                if item["status"] == "ready" and item["last_used_at"] >= cutoff:
                    continue
                if item["sandbox_id"]:
                    try:
                        matches = [client.get(item["sandbox_id"])]
                    except DaytonaNotFoundError:
                        matches = []
                else:
                    # create 中断时尚未拿到 ID，按我们写入的标签找回资源。
                    matches = list(client.list(ListSandboxesQuery(labels=_labels(key)), request_timeout=30))
                    if not matches and item["last_used_at"] >= cutoff:
                        # 创建请求可能仍在服务端执行，保留记录，下一轮继续观察。
                        continue
                for sandbox in matches:
                    if item["workdir"]:
                        # 回收前必须有最新有效备份；停止的沙箱先启动才能下载。
                        state = getattr(sandbox.state, "value", sandbox.state)
                        if state == "stopped":
                            sandbox.start(timeout=120)
                        elif state != "started":
                            raise RuntimeError("沙箱状态不支持安全备份，暂不删除")
                        backend = backend_factory(sandbox=sandbox, timeout=settings.syc_sandbox_command_timeout)
                        save_session_workspace(root, key, backend, item["workdir"])
                    try:
                        client.delete(sandbox, wait=True, timeout=120)
                    except DaytonaNotFoundError:
                        pass
                # 初始化失败、尚无工作目录的资源可移除；已使用会话永远保留恢复关联。
                if item["workdir"] or item["snapshot_path"]:
                    _sql(root, "UPDATE sessions SET sandbox_id=NULL,status='archived' WHERE session_key=?", (key,))
                else:
                    _sql(root, "DELETE FROM sessions WHERE session_key=?", (key,))
                counts["deleted"] += 1
        except Timeout:
            counts["busy"] += 1
        except Exception as error:
            counts["failed"] += 1
            logger.error("回收失败，将在下轮重试：{}", type(error).__name__)
    return counts
