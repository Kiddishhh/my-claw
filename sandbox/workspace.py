"""按会话保存不可变文件版本；SQLite 仅在成功后切换指针。

备份不是 shell 进程快照。调用方必须持有会话锁并停止任务写入；后台服务仍可能
修改文件，所以下载前后比较清单，发现变化即失败，绝不替换上一个有效版本。
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import posixpath
import shlex
import shutil
import uuid


# 只排除明确的依赖、缓存及运行输出；不按源码扩展名白名单筛选，避免漏掉素材/配置。
# 构建输出目录仅匹配项目根目录，src/build 等业务源码目录仍然保留。
_DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_OUTPUT_ROOTS = {"dist", "build", ".next", "out", "output", "outputs", "logs", "coverage", "test-results", "playwright-report", ".sandbox-state"}
_RESULT_FILES = {"pytest_result.txt", "junit.xml", "test-results.xml", ".coverage"}
_OUTPUT_SUFFIXES = (".log", ".pyc", ".pyo", ".tsbuildinfo")


def _keep_source(name: str) -> bool:
    """保存和历史版本恢复采用同一约定；自定义运行报告应写入 outputs/。"""
    parts = name.split("/")
    return (not any(p in _DEPENDENCY_DIRS for p in parts)
            and parts[0] not in _OUTPUT_ROOTS
            and name not in _RESULT_FILES
            and not parts[-1].endswith(_OUTPUT_SUFFIXES))


def _relative(name: str) -> PurePosixPath:
    """同时拒绝 POSIX 穿越和 Windows 特殊文件名，防止远程路径逃逸到宿主。"""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(p in {".", ".."} for p in name.split("/")):
        raise ValueError("非法工作区相对路径")
    for part in name.split("/"):
        if not part or re.search(r'[\\:*?"<>|\x00-\x1f]', part) or part.endswith((".", " ")):
            raise ValueError("工作区路径不可移植")
        if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
            raise ValueError("拒绝设备文件路径")
    return path


def _inside(root: Path, name: str) -> Path:
    rel = _relative(name)
    target = root.joinpath(*rel.parts)
    for candidate in (target, *target.parents):
        if candidate == root.parent:
            break
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("工作区不接受链接")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("工作区路径越界")
    return target


def _scan(backend, workdir: str) -> list[dict]:
    # 仅把扫描程序和远程路径交给沙箱；不上传平台源码或环境变量。
    script = '''import os,sys,json,hashlib,stat
from pathlib import Path
supplied=Path(sys.argv[1])
if supplied.is_symlink() or not supplied.is_dir(): raise ValueError('missing or linked workspace')
root=supplied.resolve()
deps,outputs,results,suffixes=json.loads(sys.argv[2])
def keep(name):
    parts=name.split('/')
    return not any(p in deps for p in parts) and parts[0] not in outputs and name not in results and not parts[-1].endswith(tuple(suffixes))
items=[]
def fail(error): raise error
for base,dirs,files in os.walk(root,followlinks=False,onerror=fail):
    # 原地剪枝，避免扫描或下载依赖及构建产物。
    dirs[:]=[name for name in dirs if keep(Path(base,name).relative_to(root).as_posix())]
    for name in sorted(dirs+files):
        p=Path(base,name)
        if not keep(p.relative_to(root).as_posix()): continue
        info=p.lstat()
        item=dict(path=p.relative_to(root).as_posix(),mode=stat.S_IMODE(info.st_mode)&0o777)
        if stat.S_ISLNK(info.st_mode):
            target=p.resolve()
            if not target.is_relative_to(root): raise ValueError('link escapes workspace')
            item.update(kind='symlink',target=os.path.relpath(target,p.parent).replace(os.sep,'/'))
        elif stat.S_ISDIR(info.st_mode):
            item.update(kind='directory')
        elif stat.S_ISREG(info.st_mode):
            size=info.st_size
            digest=hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda:f.read(1048576),b''): digest.update(chunk)
            item.update(kind='file',size=size,sha256=digest.hexdigest())
        else: raise ValueError('unsupported special file')
        items.append(item)
print(json.dumps(sorted(items,key=lambda x:x['path'])))
'''
    policy = json.dumps([sorted(_DEPENDENCY_DIRS), sorted(_OUTPUT_ROOTS), sorted(_RESULT_FILES), _OUTPUT_SUFFIXES])
    command = "python -c " + shlex.quote(script) + " " + " ".join(shlex.quote(x) for x in (workdir, policy))
    result = backend.execute(command)
    if result.exit_code != 0 or getattr(result, "truncated", False):
        raise RuntimeError("工作区扫描失败或清单被截断，保留旧备份")
    items = json.loads(result.output)
    _validate_manifest(items)
    return [item for item in items if _keep_source(item["path"])]


def _validate_manifest(items: list[dict]) -> None:
    if not isinstance(items, list):
        raise ValueError("非法文件清单")
    seen = set()
    for item in items:
        rel = _relative(item["path"])
        key = str(rel).casefold()
        if key in seen:
            raise ValueError("重复文件路径")
        seen.add(key)
        kind = item.get("kind", "file")  # 兼容已有普通文件备份。
        mode = item.get("mode", 0o644)
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise ValueError("非法文件权限")
        if kind == "symlink":
            target = item["target"]
            resolved = posixpath.normpath(posixpath.join(str(rel.parent), target))
            if not target or "\\" in target or PurePosixPath(target).is_absolute() or resolved == ".." or resolved.startswith("../"):
                raise ValueError("链接越出项目目录")
            if resolved != ".":
                _relative(resolved)
        elif kind == "file":
            if type(item["size"]) is not int or item["size"] < 0:
                raise ValueError("非法文件大小")
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("非法校验值")
        elif kind != "directory":
            raise ValueError("非法文件类型")
    # 不允许文件/链接同时作为其他清单项的父目录。
    kinds = {x["path"].casefold(): x.get("kind", "file") for x in items}
    for item in items:
        for parent in PurePosixPath(item["path"]).parents:
            if str(parent) != "." and kinds.get(str(parent).casefold(), "directory") != "directory":
                raise ValueError("清单目录结构冲突")


def save_workspace(backend, workdir: str, root: Path, session_key: str) -> str:
    """下载、校验后发布不可变版本，返回相对 root 的目录；失败不触碰旧版本。"""
    if not re.fullmatch(r"[0-9a-f]{64}", session_key):
        raise ValueError("非法会话存储标识")
    items = _scan(backend, workdir)
    version = uuid.uuid4().hex
    base = f".sandbox-state/workspaces/{session_key}"
    stage = _inside(root, f"{base}/staging/{version}")
    final_rel = f"{base}/versions/{version}"
    final = _inside(root, final_rel)
    stage.mkdir(parents=True)
    try:
        (stage / "project").mkdir()
        for item in items:
            kind = item.get("kind", "file")
            if kind == "directory":
                _inside(stage / "project", item["path"]).mkdir(parents=True, exist_ok=True)
                continue
            if kind == "symlink":
                # Windows 不需要创建或执行宿主链接；完整链接元数据保存在清单中。
                continue
            remote = str(PurePosixPath(workdir) / item["path"])
            results = backend.download_files([remote])
            if len(results) != 1 or results[0].error or results[0].content is None:
                raise RuntimeError("项目文件下载失败，旧版本未替换")
            content = results[0].content
            if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise RuntimeError("项目文件下载校验失败")
            target = _inside(stage / "project", item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if items != _scan(backend, workdir):
            raise RuntimeError("保存期间项目发生变化，请停止后台写入后重试")
        (stage / "manifest.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        final.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(final)
        return final_rel
    except BaseException:
        # stage 是本次生成的 UUID 目录，已校验在会话存储根内。
        shutil.rmtree(stage)
        raise


def restore_workspace(backend, workdir: str, root: Path, version_path: str) -> None:
    """校验完整版本后上传到新建的空沙箱；不用于覆盖已有工作区。"""
    version = _inside(root, version_path)
    manifest = _inside(version, "manifest.json")
    items = json.loads(manifest.read_text(encoding="utf-8"))
    _validate_manifest(items)
    # 旧版全量备份仍可用，但不把依赖、日志和构建产物带回新沙箱。
    items = [item for item in items if _keep_source(item["path"])]
    files = []
    for item in items:
        if item.get("kind", "file") != "file":
            continue
        path = _inside(version / "project", item["path"])
        if path.stat().st_size != item["size"]:
            raise ValueError("本地备份大小校验失败")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("本地备份校验失败")
        files.append((str(PurePosixPath(workdir) / item["path"]), content))
    parents = {workdir, *(str(PurePosixPath(p).parent) for p, _ in files)}
    parents.update(str(PurePosixPath(workdir) / x["path"]) for x in items if x.get("kind") == "directory")
    parents.update(str((PurePosixPath(workdir) / x["path"]).parent) for x in items)
    result = backend.execute("mkdir -p " + " ".join(shlex.quote(p) for p in sorted(parents)))
    if result.exit_code != 0:
        raise RuntimeError("恢复目录创建失败")
    for file in files:
        results = backend.upload_files([file])
        if len(results) != 1 or results[0].error:
            raise RuntimeError("项目恢复上传失败")
    # 先上传普通文件，再恢复链接与执行位；不把 Linux 执行权限施加到服务器文件。
    for item in sorted(items, key=lambda x: len(PurePosixPath(x["path"]).parts), reverse=True):
        path = shlex.quote(str(PurePosixPath(workdir) / item["path"]))
        if item.get("kind") == "symlink":
            command = f"ln -s -- {shlex.quote(item['target'])} {path}"
        elif "mode" in item:
            command = f"chmod {item['mode']:o} -- {path}"
        else:
            continue
        if backend.execute(command).exit_code != 0:
            raise RuntimeError("工作区链接或权限恢复失败")


def prune_versions(root: Path, session_key: str, current: str) -> None:
    """SQLite 已提交后保留当前版本和一个前版；不清理其他会话或 staging。"""
    directory = _inside(root, f".sandbox-state/workspaces/{session_key}/versions")
    current_dir = _inside(root, current)
    previous = sorted((p for p in directory.iterdir() if p != current_dir),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in previous[1:]:
        # UUID 名称、实际路径及链接逐项核实，避免根据数据库中的任意路径递归删除。
        if not re.fullmatch(r"[0-9a-f]{32}", candidate.name):
            continue
        checked = _inside(root, candidate.relative_to(root).as_posix())
        shutil.rmtree(checked)
