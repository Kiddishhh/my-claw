"""数据分析验收的固定样例和独立断言；不调用模型、不读取宿主业务数据。"""
import json
import shlex
from decimal import Decimal
from pathlib import Path

SALES_CSV = (
    "date,product,amount,status\n"
    "2026-01-02,A,0.10,paid\n"
    "2026-01-03,A,0.20,paid\n"
    "2026-02-01,B,12.50,paid\n"
    "2026-02-02,B,,cancelled\n"
)


def run_analysis(root: Path) -> dict:
    """受控验收计算脚本：实际调用表格库，不让模型手算或预填结果。"""
    from tools.data_tools import create_data_tools
    tools = {item.name: item for item in create_data_tools(root)}
    for item in tools.values():
        item.handle_tool_error = False
        item.handle_validation_error = False
    dataset = tools["inspect_table"].invoke({"file_path": "sales_verification.csv"})
    result = tools["analyze_table"].invoke({
        "dataset_id": dataset["dataset_id"], "metric": "amount", "group_by": ["product"],
        "filters": [{"column": "status", "operator": "eq", "value": "paid"}],
    })
    chart = tools["plot_analysis"].invoke({
        "result_id": result["result_id"], "x": "product", "title": "Paid sales",
        "x_label": "Product", "y_label": "Amount (CNY)",
    })
    report = tools["export_report"].invoke({"result_id": result["result_id"], "title": "Paid sales"})
    summary = {"result": result, "chart": chart, "report": report}
    output = root / "outputs/analysis/verification-summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def analysis_task(workdir: str) -> str:
    """给模型真实业务问题和已测计算入口，隔离测试目标与代码生成能力。"""
    code = f"import json; from pathlib import Path; from scripts._analysis_verification import run_analysis; print(json.dumps(run_analysis(Path({workdir!r}))))"
    command = f"cd {shlex.quote(workdir)} && python -m uv run --locked --extra daytona python -c {shlex.quote(code)}"
    return (
        f"请分析 {workdir}/data/inputs/sales_verification.csv，金额单位元，按商品统计已付款金额，"
        "生成柱状图和 CSV/XLSX/Markdown 报告，排除取消订单，保留原始输入。"
        "这是受控集成验收，计算脚本已预置并验证，不测试自主代码生成。"
        "主代理直接委派 data_analyst，不要事先阅读源码。请将以下完整命令交给 data_analyst，"
        "要求用 execute 实际执行一次，读取退出码和返回摘要后立即汇报，不重写脚本、不调查字体或扫描源码："
        f"\n{command}\n"
        f"随后将实际执行结果和 {workdir}/outputs/analysis/verification-summary.json 的路径交 reviewer。"
        "reviewer 只读摘要及其中的结果 JSON / Markdown 报告，核对金额与筛选口径后使用 ReviewResult 返回结论，"
        "不运行命令、不审查本次未改动的全库代码。主代理不代替子代理执行。"
    )


def validate_analysis_files(root: Path) -> dict:
    """从真实磁盘产物核对精确金额、筛选、输入完整性和报告文件。"""
    root = root.resolve()
    assert (root / "data/inputs/sales_verification.csv").read_bytes() == SALES_CSV.encode("utf-8"), "原始数据被修改"
    outputs = root / "outputs/analysis"
    matches = []
    for path in outputs.glob("*/result.json"):
        if path.is_symlink() or not path.resolve().is_relative_to(outputs.resolve()):
            raise AssertionError("分析结果越界")
        result = json.loads(path.read_text(encoding="utf-8"))
        rows = result.get("rows", [])
        if len(rows) == 2 and {row.get("product"): Decimal(row["value"]) for row in rows} == {"A": Decimal("0.30"), "B": Decimal("12.50")}:
            matches.append((path, result))
    assert matches, "未找到精确统计结果 A=0.30、B=12.50"
    path, result = matches[-1]
    assert result["rows_used"] == 3, "已付款记录数量错误"
    files = [path]
    for pattern in ("chart-*.png", "report-*.csv", "report-*.xlsx", "report-*.md"):
        found = sorted(path.parent.glob(pattern))
        assert found, f"缺少产物：{pattern}"
        files.append(found[-1])
    for item in files:
        assert not item.is_symlink() and item.resolve().is_relative_to(outputs.resolve())
        assert 0 < item.stat().st_size <= 5 * 1024 * 1024
    assert files[1].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    return {"totals": {"A": "0.30", "B": "12.50"}, "rows_used": 3,
            "artifacts": [str(item.relative_to(root)).replace("\\", "/") for item in files]}
