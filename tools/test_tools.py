"""DevMate 的本地 pytest 工具；本模块只定义工具，不向 Agent 注册。

使用当前 Python 解释器，在项目根目录执行 pytest；只接受项目内文件或目录。
默认超时 120 秒，最多返回末尾 3000 字符；不主动创建日志文件。
本地路径检查不能替代沙箱：pytest 会执行测试及其导入代码，仅适合受信任的本地项目。
"""

import asyncio
import os
from pathlib import Path
import sys
from typing import Any

from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_CHARS = 3000
_MAX_OUTPUT_BYTES = _MAX_OUTPUT_CHARS * 4


def _test_target(path: str, root: Path | None = None) -> Path:
    """将输入解析为项目内现有文件或目录，不接受额外命令行参数。"""
    root = root if root is not None else PROJECT_ROOT
    if not path.strip():
        raise ToolException("测试路径不能为空。")
    try:
        target = (root / path).resolve(strict=True)
        valid = target.is_relative_to(root.resolve()) and (target.is_file() or target.is_dir())
    except (OSError, ValueError, RuntimeError):
        raise ToolException("测试路径无效或不存在，请提供项目内的测试文件或目录。") from None
    if not valid:
        raise ToolException("只允许运行当前项目内的测试文件或目录。")
    return target


async def _read_result(process: Any) -> tuple[str, bool]:
    """持续读取输出，只保留有界尾部，避免大量测试输出长期占用内存。"""
    tail = bytearray()
    truncated = False
    while chunk := await process.stdout.read(4096):
        tail.extend(chunk)
        if len(tail) > _MAX_OUTPUT_BYTES:
            del tail[:-_MAX_OUTPUT_BYTES]
            truncated = True
    await process.wait()
    text = tail.decode("utf-8", errors="replace")
    return text[-_MAX_OUTPUT_CHARS:].rstrip(), truncated or len(text) > _MAX_OUTPUT_CHARS


async def _stop_process(process: Any) -> None:
    """超时或取消时终止并回收直接子进程；不承诺终止测试另行创建的进程树。"""
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


@tool
async def run_pytest(path: str = "tests/") -> str:
    """在本工具所属项目中执行 pytest，验证指定测试文件或目录。

    适用：用户要求运行测试，或代码修改后需要验证行为。优先选择覆盖本次改动的测试范围；
    仅阅读或解释代码时无需执行。不用于生成测试、运行任意 Python 脚本、安装依赖或执行 shell 命令。

    参数：path 为项目内已存在的测试文件/目录，默认 "tests/"；建议传项目根目录相对路径，
    例如 {"path": "tests/test_settings.py"}。不接受 pytest 参数、shell 命令或 :: 测试节点选择器。
    执行使用当前 Python 解释器，工作目录固定为本工具所属项目根目录，不跟随 Agent 的临时工作目录。
    若代码写在其他隔离目录，本工具不能据此证明那份代码通过测试。

    返回文本包含 pytest exit_code 和合并后的 stdout/stderr 尾部，最多 3000 字符，截断时注明。
    退出码 0 表示本次收集到的测试通过，不代表未覆盖功能或真实外部服务已验证。
    测试失败、未收集到测试、启动失败或超时均作为工具错误返回；根据报错修复后再运行。

    超时为 120 秒，超时/取消仅终止直接测试子进程，不保证清理测试另行启动的进程树。
    pytest 会执行测试及导入代码，其文件写入、网络请求等副作用由被测代码决定。
    无代码、配置或环境变化时，不必反复运行同一组已通过测试。
    """
    return await _run_pytest(path, PROJECT_ROOT)


async def _run_pytest(path: str, root: Path) -> str:
    """在指定工作区运行测试，保留统一的超时与输出限制。"""
    target = _test_target(path, root)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", str(target), "-q", "-p", "no:cacheprovider",
            cwd=root,
            env=os.environ | {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        raise ToolException("无法启动 pytest，请检查当前 Python 解释器及项目虚拟环境。") from None

    try:
        output, truncated = await asyncio.wait_for(_read_result(process), timeout=_TIMEOUT_SECONDS)
    except TimeoutError:
        await _stop_process(process)
        raise ToolException(f"pytest 运行超时（{_TIMEOUT_SECONDS} 秒），已终止直接测试子进程。") from None
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    except OSError:
        await _stop_process(process)
        raise ToolException("读取 pytest 输出失败，已回收直接测试子进程。") from None

    note = "；输出已截断，仅保留末尾 3000 字符" if truncated else ""
    summary = f"pytest exit_code={process.returncode}{note}\n{output or '（没有输出）'}"
    if process.returncode != 0:
        raise ToolException(summary)
    return summary


run_pytest.handle_tool_error = True
run_pytest.handle_validation_error = "参数无效，请提供字符串形式的项目内测试路径。"


def create_test_tools(workspace_root: Path) -> list[BaseTool]:
    """绑定当前 Agent 工作区，避免隔离验收误跑宿主项目测试。"""
    root = workspace_root.resolve()

    @tool("run_pytest")
    async def workspace_pytest(path: str = "tests/") -> str:
        """在当前 Agent 工作区使用当前虚拟环境运行 pytest。

        只用于修改后的必要验证或用户要求的测试。path 为工作区相对路径，如 tests/test_settings.py；
        不接受 shell 命令、pytest 选项或测试节点选择器。默认 tests/。
        返回退出码和最多 3000 字符输出尾部；失败、无测试、超时均作为工具错误返回。
        超时 120 秒，仅保证终止直接子进程。测试会执行本地代码，需信任工作区。
        """
        return await _run_pytest(path, root)

    workspace_pytest.handle_tool_error = True
    workspace_pytest.handle_validation_error = "参数无效，请提供项目内测试路径。"
    return [workspace_pytest]
