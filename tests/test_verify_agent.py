"""End-to-end tests for the verification CLI without provider requests."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests import verify_agent


def test_offline_probe_checks_real_wiring_and_records_calls(tmp_path: Path, capsys) -> None:
    result = asyncio.run(verify_agent.run_verification(log_root=tmp_path))
    assert result["passed"]
    assert result["model_calls"] == 4
    assert result["stop_reason"] == "completed"
    assert not result["budget_hit"] and not result["call_limit_hit"]
    assert all(check["passed"] for check in result["checks"] if check["hard"])
    folder = Path(result["record_dir"])
    saved = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert saved["passed"]
    artifact = json.loads((folder / "workspace" / "verification.json").read_text(encoding="utf-8"))
    assert artifact["memory_code"] != artifact["skill_code"]
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(event["event"] == "model_start" for event in events) == 4
    assert any(event["event"] == "tool_end" and event["status"] == "success" for event in events)
    assert any(event["event"] == "graph_update" for event in events)
    output = capsys.readouterr().out
    assert all(text in output for text in ["Memory", "Skills", "工具调用轨迹", "断言结果", "熔断"])


def test_budget_stop_is_not_reported_as_success(tmp_path: Path) -> None:
    result = asyncio.run(verify_agent.run_verification(log_root=tmp_path, max_input_tokens=1))
    assert result["budget_hit"] and not result["passed"]
    assert result["stop_reason"] == "input_budget"
    assert result["model_calls"] == 1


def test_call_limit_distinguishes_synthetic_message_from_real_call(tmp_path: Path) -> None:
    result = asyncio.run(verify_agent.run_verification(log_root=tmp_path, max_model_calls=1))
    assert result["call_limit_hit"] and not result["passed"]
    assert result["stop_reason"] == "model_call_limit"
    assert result["model_calls"] == 1


def test_wrong_file_content_cannot_pass_from_tool_trace_alone(tmp_path: Path) -> None:
    class WrongContentModel(verify_agent.OfflineProbeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            response = super()._generate(messages, stop, run_manager, **kwargs)
            for call in response.generations[0].message.tool_calls:
                if call["name"] == "write_file":
                    call["args"]["content"] = '{}'
            return response

    result = asyncio.run(verify_agent.run_verification(log_root=tmp_path, model=WrongContentModel()))
    assert not result["passed"]
    assert not next(item for item in result["checks"] if item["name"] == "磁盘文件验证标记匹配")["passed"]


def test_failure_keeps_record_and_sets_non_success_reason(tmp_path: Path) -> None:
    class BrokenModel(verify_agent.OfflineProbeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("offline failure")

    result = asyncio.run(verify_agent.run_verification(log_root=tmp_path, model=BrokenModel()))
    assert not result["passed"] and result["stop_reason"] == "error"
    assert result["error_type"] == "RuntimeError"
    assert (Path(result["record_dir"]) / "report.json").exists()


def test_timeout_is_recorded(tmp_path: Path) -> None:
    class SlowModel(verify_agent.OfflineProbeModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.sleep(60)

    result = asyncio.run(verify_agent.run_verification(log_root=tmp_path, model=SlowModel(), timeout=0.1))
    assert result["stop_reason"] == "timeout" and not result["passed"]


def test_file_entrypoint_runs_from_external_directory(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ | {"PYTHONPATH": "", "PYTHONUTF8": "1", "LANGSMITH_TRACING": "false",
                        "SYC_API_KEY": "offline-placeholder"}
    completed = subprocess.run(
        [sys.executable, str(root / "tests/verify_agent.py"), "--offline", "--log-dir", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    assert "offline" in completed.stdout and "model_start" in completed.stdout


@pytest.mark.parametrize("kwargs", [{"max_model_calls": 0}, {"max_input_tokens": -1}, {"timeout": 0}])
def test_invalid_limits_rejected_before_creating_output(tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        asyncio.run(verify_agent.run_verification(log_root=tmp_path, **kwargs))
    assert list(tmp_path.iterdir()) == []
