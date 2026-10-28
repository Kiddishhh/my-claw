"""run_pytest 的离线测试；直接运行本文件可在终端查看真实 pytest 演示。"""

import asyncio
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


@pytest.fixture
def pytest_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """工具只运行临时测试，不执行真实模型请求，也不写应用日志。"""
    assert importlib.util.find_spec("tools.test_tools") is not None, "需要添加 tools/test_tools.py"
    module = importlib.import_module("tools.test_tools")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    return module


def test_run_pytest_executes_real_test_in_isolated_directory(pytest_tool: Any) -> None:
    """通过工具实际启动当前解释器的 pytest，并打印返回摘要。"""
    target = pytest_tool.PROJECT_ROOT / "tests" / "test_demo.py"
    target.write_text("def test_sum():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    print("\n[模拟任务] 修改代码后，运行 tests/test_demo.py 验证加法结果。")
    result = asyncio.run(pytest_tool.run_pytest.ainvoke({"path": "tests/test_demo.py"}))
    print(f"[run_pytest 返回]\n{result}")
    assert "exit_code=0" in result and "1 passed" in result
    assert not (pytest_tool.PROJECT_ROOT / "logs").exists()
    print("[校验通过] 当前解释器成功执行 pytest，未生成应用日志。")


def test_failed_test_returns_error_tool_message(pytest_tool: Any) -> None:
    target = pytest_tool.PROJECT_ROOT / "tests" / "test_failure.py"
    target.write_text("def test_failure():\n    assert 1 == 2\n", encoding="utf-8")
    result = asyncio.run(pytest_tool.run_pytest.ainvoke({
        "name": "run_pytest", "args": {"path": "tests/test_failure.py"},
        "id": "failed-test", "type": "tool_call",
    }))
    print(f"\n[预期失败结果] status={result.status}\n{result.content}")
    assert result.status == "error" and "exit_code=1" in result.content and "1 failed" in result.content


@pytest.mark.parametrize("path", ["", " ", "../outside", "missing.py", "--help", "tests/../.."])
def test_invalid_path_never_starts_process(pytest_tool: Any, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("无效路径不得启动进程")

    monkeypatch.setattr(pytest_tool.asyncio, "create_subprocess_exec", forbidden)
    result = asyncio.run(pytest_tool.run_pytest.ainvoke({
        "name": "run_pytest", "args": {"path": path}, "id": "invalid", "type": "tool_call",
    }))
    assert result.status == "error"


def test_process_arguments_and_bounded_output(pytest_tool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def create(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        reader = asyncio.StreamReader()
        reader.feed_data(("很长的测试输出" * 10000 + "\nRESULT-END").encode("utf-8"))
        reader.feed_eof()

        class Process:
            stdout = reader
            returncode = 0

            async def wait(self) -> int:
                return 0

        return Process()

    monkeypatch.setattr(pytest_tool.asyncio, "create_subprocess_exec", create)
    result = asyncio.run(pytest_tool.run_pytest.ainvoke({}))
    args, kwargs = calls[0]
    assert args[:3] == (sys.executable, "-m", "pytest")
    assert Path(args[3]) == pytest_tool.PROJECT_ROOT / "tests"
    assert kwargs["cwd"] == pytest_tool.PROJECT_ROOT
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert result.endswith("RESULT-END") and "截断" in result
    assert len(result) < 3200


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_stop_child(pytest_tool: Any, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    processes = []

    async def scenario() -> None:
        started = asyncio.Event()

        async def create(*args: Any, **kwargs: Any) -> Any:
            class Process:
                stdout = asyncio.StreamReader()
                returncode = None
                killed = False
                drained = False

                def kill(self) -> None:
                    self.killed = True
                    self.returncode = -9
                    self.stdout.feed_eof()

                async def communicate(self) -> tuple[bytes, None]:
                    self.drained = True
                    return b"", None

            process = Process()
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(pytest_tool.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(pytest_tool, "_TIMEOUT_SECONDS", 10 if cancel else 0.01)
        task = asyncio.create_task(pytest_tool.run_pytest.ainvoke({}))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert "超时" in await task

    asyncio.run(scenario())
    assert processes[0].killed and processes[0].drained


def test_process_start_failure_is_reported(pytest_tool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def create(*args: Any, **kwargs: Any) -> None:
        raise OSError("private-system-detail")

    monkeypatch.setattr(pytest_tool.asyncio, "create_subprocess_exec", create)
    result = asyncio.run(pytest_tool.run_pytest.ainvoke({}))
    assert "启动" in result and "private-system-detail" not in result


@pytest.mark.parametrize("mode", ["file", "module"])
def test_import_from_external_directory_without_pythonpath(tmp_path: Path, mode: str) -> None:
    root = Path(__file__).resolve().parents[1]
    code = (
        "import runpy,sys; runpy.run_path(sys.argv[1], run_name='import_check')"
        if mode == "file" else "from tools.test_tools import run_pytest"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(root / "tools/test_tools.py")], cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": "", "LANGSMITH_TRACING": "false"},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "logs").exists()


def main() -> int:
    """直接运行时只演示一次真实 pytest 调用，结果仅打印到终端。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    return int(pytest.main([
        f"{Path(__file__).resolve()}::test_run_pytest_executes_real_test_in_isolated_directory",
        "-s", "-v", "-p", "no:cacheprovider",
    ]))


if __name__ == "__main__":
    raise SystemExit(main())
