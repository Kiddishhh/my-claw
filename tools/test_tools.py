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
from langchain_core.tools import ToolException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_CHARS = 3000
_MAX_OUTPUT_BYTES = _MAX_OUTPUT_CHARS * 4


def _test_target(path: str) -> Path:
    """将输入解析为项目内现有文件或目录，不接受额外命令行参数。"""
    if not path.strip():
        raise ToolException("测试路径不能为空。")
    try:
        target = (PROJECT_ROOT / path).resolve(strict=True)
        valid = target.is_relative_to(PROJECT_ROOT.resolve()) and (target.is_file() or target.is_dir())
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
    """运行项目内指定目录或文件的 pytest，验证代码改动，默认测试 tests/。

    使用当前 Python 解释器，返回退出码和末尾最多 3000 字符的测试输出。
    超过 120 秒会终止直接测试子进程；失败、未收集到测试或超时均标记为工具错误。
    path 只接受项目内文件或目录，不接受 shell 命令、pytest 选项或 :: 节点选择器。
    """
    target = _test_target(path)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", str(target), "-q", "-p", "no:cacheprovider",
            cwd=PROJECT_ROOT,
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
