"""服务器只读指导文件路由；execute 始终转交默认的远程 backend。

只发布 Markdown 指导材料，不暴露平台项目根。内容寻址目录保证运行期间
版本稳定；管理员修改源文件后，新建 Agent 会发布新版本。
"""
import hashlib
import os
from pathlib import Path
import tempfile

from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend

GUIDANCE_PERMISSIONS = [FilesystemPermission(operations=["write"], paths=["/guidance/**"], mode="deny")]
MEMORY = ["/guidance/AGENTS.md"]
DEV_SKILLS = ["/guidance/development/"]
DATA_SKILLS = ["/guidance/data-analysis/"]


def guidance_backend(sandbox_backend, root: Path) -> CompositeBackend:
    """从可信服务器目录生成规则快照，保持框架原生技能渐进式加载。"""
    sources = {"AGENTS.md": (root / "AGENTS.md").read_bytes()}
    for directory, prefix in ((root / ".agents/skills", "development"),
                              (root / "subagents/skills/data-analysis", "data-analysis")):
        for path in sorted(directory.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("指导文件不接受越界链接")
            sources[f"{prefix}/{path.relative_to(directory).as_posix()}"] = path.read_bytes()
    digest = hashlib.sha256()
    for name, content in sorted(sources.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    target = root / ".sandbox-state/guidance" / digest.hexdigest()
    for name, content in sources.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # 原子发布单个文件，另一个构建不会读到写入一半的规则。
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
                pending.write(content)
                pending_path = Path(pending.name)
            os.replace(pending_path, path)
    return CompositeBackend(default=sandbox_backend, routes={
        "/guidance/": FilesystemBackend(root_dir=target, virtual_mode=True),
    })
