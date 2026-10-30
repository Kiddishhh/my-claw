"""验收断言必须识别正确产物，也必须拒绝被篡改的数据和金额。"""
import json
from pathlib import Path
import pytest
from scripts._analysis_verification import SALES_CSV, analysis_task, run_analysis, validate_analysis_files
from tools.data_tools import create_data_tools


def test_analysis_verifier_checks_artifacts_and_rejects_tampering(tmp_path):
    source = tmp_path / "data/inputs/sales_verification.csv"
    source.parent.mkdir(parents=True)
    source.write_bytes(SALES_CSV.encode("utf-8"))
    result = run_analysis(tmp_path)["result"]
    summary = validate_analysis_files(tmp_path)
    assert summary["totals"] == {"A": "0.30", "B": "12.50"}
    assert len(summary["artifacts"]) == 5
    source.write_text("changed", encoding="utf-8")
    with pytest.raises(AssertionError, match="原始数据"):
        validate_analysis_files(tmp_path)
    source.write_bytes(SALES_CSV.encode("utf-8"))
    saved = tmp_path / result["path"]
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["rows"][0]["value"] = "999"
    saved.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AssertionError, match="精确统计"):
        validate_analysis_files(tmp_path)
    assert "reviewer" in analysis_task("/workspace")


def test_table_tools_run_without_model_credentials(tmp_path):
    """复制纯计算模块到无 .env 的目录，模拟沙箱，而不是依赖宿主配置。"""
    import os
    import shutil
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    for name in ("infra", "tools"):
        shutil.copytree(root / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    source = tmp_path / "data/inputs/sales_verification.csv"
    source.parent.mkdir(parents=True)
    source.write_bytes(SALES_CSV.encode("utf-8"))
    code = (
        "from pathlib import Path; from tools.data_tools import create_data_tools; "
        "tools={t.name:t for t in create_data_tools(Path.cwd())}; "
        "tools['inspect_table'].handle_validation_error=False; "
        "result=tools['inspect_table'].invoke({'file_path':'sales_verification.csv'}); "
        "assert result['row_count']==4; print('NO_MODEL_KEY_REQUIRED')"
    )
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("SYC_", "LANGSMITH_", "LANGCHAIN_", "OPENAI_", "DAYTONA_"))}
    env.update(PYTHONPATH="", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "NO_MODEL_KEY_REQUIRED" in result.stdout
