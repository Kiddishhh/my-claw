"""Local data analysis behavioral coverage; no provider requests."""
import json
import asyncio
import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from langchain_core.tools import ToolException

from tools.data_tools import create_data_tools


@pytest.fixture
def setup(tmp_path):
    """Create known sales data in an isolated workspace."""
    source = tmp_path / "data/inputs"
    source.mkdir(parents=True)
    (source / "sales.csv").write_text(
        "date,product,amount,status\n2026-01-02,A,0.10,paid\n"
        "2026-01-03,A,0.20,paid\n2026-02-01,B,12.50,paid\n"
        "2026-02-02,B,,cancelled\n", encoding="utf-8-sig")
    tools = {tool.name: tool for tool in create_data_tools(tmp_path)}
    for tool in tools.values():
        tool.handle_tool_error = False  # Surface the same validation errors to assertions.
    return tmp_path, tools


def test_exact_totals_chart_and_exports(setup):
    """Read, filter, group, plot and export using immutable artifacts."""
    root, tools = setup
    original = (root / "data/inputs/sales.csv").read_bytes()
    inspected = tools["inspect_table"].invoke({"file_path": "sales.csv"})
    assert inspected["row_count"] == 4 and inspected["missing"]["amount"] == 1
    result = tools["analyze_table"].invoke({
        "dataset_id": inspected["dataset_id"], "group_by": ["product"],
        "metric": "amount", "operation": "sum",
        "filters": [{"column": "status", "operator": "eq", "value": "paid"}],
    })
    assert result["preview"] == [{"product": "A", "value": "0.30"}, {"product": "B", "value": "12.50"}]
    chart = tools["plot_analysis"].invoke({"result_id": result["result_id"], "x": "product"})
    assert (root / chart["path"]).read_bytes().startswith(b"\x89PNG")
    export = tools["export_report"].invoke({"result_id": result["result_id"], "title": "Sales"})
    assert all((root / path).exists() for path in export["paths"])
    workbook = load_workbook(root / export["paths"][1])
    assert workbook.active.cell(2, 2).value == "0.30"
    workbook.close()
    assert (root / "data/inputs/sales.csv").read_bytes() == original


def test_monthly_grouping_and_missing_policy(setup):
    """Missing amounts cannot silently become zero."""
    root, tools = setup
    dataset = tools["inspect_table"].invoke({"file_path": "sales.csv"})["dataset_id"]
    with pytest.raises(ToolException, match="缺失"):
        tools["analyze_table"].invoke({"dataset_id": dataset, "metric": "amount"})
    result = tools["analyze_table"].invoke({"dataset_id": dataset, "metric": "amount", "missing": "drop", "date_column": "date", "date_format": "%Y-%m-%d", "frequency": "month"})
    assert result["preview"] == [{"date": "2026-01", "value": "0.30"}, {"date": "2026-02", "value": "12.50"}]
    assert result["rows_used"] == 3


def test_excel_sheet_selection_and_formulas(setup):
    """Sheet ambiguity and unevaluated formulas are explicit errors."""
    root, tools = setup
    workbook = Workbook()
    workbook.active.title = "sales"
    workbook.active.append(["product", "amount"])
    workbook.active.append(["A", 1.25])
    workbook.create_sheet("other")
    workbook.save(root / "data/inputs/book.xlsx")
    with pytest.raises(ToolException, match="sheet_name"):
        tools["inspect_table"].invoke({"file_path": "book.xlsx"})
    result = tools["inspect_table"].invoke({"file_path": "book.xlsx", "sheet_name": "sales"})
    assert result["preview"][0]["amount"] == "1.25"
    workbook.active.cell(2, 2, "=1+2")
    workbook.save(root / "data/inputs/book.xlsx")
    with pytest.raises(ToolException, match="公式"):
        tools["inspect_table"].invoke({"file_path": "book.xlsx", "sheet_name": "sales"})


@pytest.mark.parametrize("path", ["../secret.csv", "../../.env", "C:/private.csv", "https://example.com/a.csv"])
def test_input_boundaries(setup, path):
    """Only input-directory files can be analyzed."""
    _, tools = setup
    with pytest.raises(ToolException):
        tools["inspect_table"].invoke({"file_path": path})


def test_duplicate_headers_and_invalid_numbers(setup):
    """Do not guess headers or coerce malformed numbers into nulls."""
    root, tools = setup
    file = root / "data/inputs/bad.csv"
    file.write_text("x,x\n1,2\n", encoding="utf-8")
    with pytest.raises(ToolException, match="表头"):
        tools["inspect_table"].invoke({"file_path": "bad.csv"})
    file.write_text("amount\nnot-a-number\n", encoding="utf-8")
    dataset = tools["inspect_table"].invoke({"file_path": "bad.csv"})["dataset_id"]
    with pytest.raises(ToolException, match="数值"):
        tools["analyze_table"].invoke({"dataset_id": dataset, "metric": "amount"})


def test_join_rejects_duplicate_lookup_keys(setup):
    """A join must not silently multiply monetary totals."""
    root, tools = setup
    (root / "data/inputs/lookup.csv").write_text("product,category\nA,one\nA,two\n", encoding="utf-8")
    left = tools["inspect_table"].invoke({"file_path": "sales.csv"})["dataset_id"]
    right = tools["inspect_table"].invoke({"file_path": "lookup.csv"})["dataset_id"]
    with pytest.raises(ToolException, match="唯一"):
        tools["analyze_table"].invoke({"dataset_id": left, "join_dataset_id": right, "join_on": "product", "operation": "count"})


def test_valid_join_snapshot_and_top_n(setup):
    """A changed source does not mutate the registered snapshot; join uses explicit keys."""
    root, tools = setup
    (root / "data/inputs/lookup.csv").write_text("product,category\nA,first\nB,second\n", encoding="utf-8")
    left = tools["inspect_table"].invoke({"file_path": "sales.csv"})["dataset_id"]
    right = tools["inspect_table"].invoke({"file_path": "lookup.csv"})["dataset_id"]
    (root / "data/inputs/sales.csv").write_text("changed\n", encoding="utf-8")
    result = tools["analyze_table"].invoke({"dataset_id": left, "join_dataset_id": right,
        "join_on": "product", "group_by": ["category"], "metric": "amount",
        "missing": "drop", "descending": True, "top_n": 1})
    assert result["preview"] == [{"category": "second", "value": "12.50"}]
    assert result["group_count"] == 2


@pytest.mark.parametrize("operation,expected", [("mean", "0.15"), ("min", "0.10"), ("max", "0.20")])
def test_aggregations(setup, operation, expected):
    """Decimal arithmetic covers each supported numeric aggregation."""
    _, tools = setup
    identity = tools["inspect_table"].invoke({"file_path": "sales.csv"})["dataset_id"]
    result = tools["analyze_table"].invoke({"dataset_id": identity, "metric": "amount", "operation": operation,
        "filters": [{"column": "product", "operator": "eq", "value": "A"}]})
    assert result["preview"] == [{"value": expected}]


def test_empty_invalid_dates_and_precision(setup):
    """Empty filters are explicit and sums retain low-order decimals."""
    root, tools = setup
    path = root / "data/inputs/edge.csv"
    path.write_text("date,amount\nNaT,100000000000000000000\n2026-01-01,0.00000000000000000001\n", encoding="utf-8")
    dataset = tools["inspect_table"].invoke({"file_path": "edge.csv"})["dataset_id"]
    with pytest.raises(ToolException, match="日期"):
        tools["analyze_table"].invoke({"dataset_id": dataset, "operation": "count", "date_column": "date", "date_format": "%Y-%m-%d"})
    summed = tools["analyze_table"].invoke({"dataset_id": dataset, "metric": "amount"})
    assert summed["preview"][0]["value"] == "100000000000000000000.00000000000000000001"
    result = tools["analyze_table"].invoke({"dataset_id": dataset, "operation": "count", "filters": [{"column": "date", "operator": "eq", "value": "missing"}]})
    assert result["row_count"] == 0 and result["rows_used"] == 0
    with pytest.raises(ToolException):
        tools["plot_analysis"].invoke({"result_id": result["result_id"], "x": "value"})


def test_excel_formula_export_and_count_dedup(setup):
    """Formula-like group text stays text in exports, duplicates require explicit opt-in."""
    root, tools = setup
    (root / "data/inputs/formula.csv").write_text("category\n=1+1\n=1+1\n", encoding="utf-8")
    identity = tools["inspect_table"].invoke({"file_path": "formula.csv"})["dataset_id"]
    result = tools["analyze_table"].invoke({"dataset_id": identity, "operation": "count", "group_by": ["category"], "deduplicate": True})
    assert result["preview"] == [{"category": "=1+1", "value": "1"}]
    assert result["duplicate_dropped"] == 1
    output = tools["export_report"].invoke({"result_id": result["result_id"]})
    assert "'=1+1" in (root / output["paths"][0]).read_text(encoding="utf-8-sig")
    book = load_workbook(root / output["paths"][1])
    assert book.active.cell(2, 1).data_type == "s"
    book.close()


def test_tools_registered_in_agent_and_skills_discovered(setup):
    """An offline model calls the registered tool inside the real Agent graph."""
    from agent.main import build_local_agent as build_agent
    from scripts._verification import ScriptedModel
    from langchain_core.messages import AIMessage, ToolMessage
    root, _ = setup
    project = Path(__file__).resolve().parents[1]
    (root / "AGENTS.md").write_text("Use data tools for local tables.", encoding="utf-8")
    for name in ("local-data-analysis", "analysis-reporting"):
        shutil.copytree(project / "subagents/skills/data-analysis" / name, root / "subagents/skills/data-analysis" / name)
    model = ScriptedModel(replies=[AIMessage(content="", tool_calls=[{
        "name": "inspect_table", "args": {"file_path": "sales.csv"}, "id": "inspect", "type": "tool_call"}]), AIMessage(content="done")])
    parent = ScriptedModel(replies=[AIMessage(content="", tool_calls=[{
        "name": "task", "args": {"subagent_type": "data_analyst", "description": "Inspect sales.csv"}, "id": "delegate", "type": "tool_call"}]), AIMessage(content="done")])
    agent = build_agent(workspace_root=root, model=parent, enable_subagents=True, subagent_models={"data_analyst": model}, max_model_calls=2)
    config = {"configurable": {"thread_id": "data-test"}}
    async def run():
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "Inspect local sales"}]}, config)
        state = (await agent.aget_state(config)).values
        return result, state
    result, state = asyncio.run(run())
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert messages[0].status == "success"
    child_results = [m for m in model.seen_messages[-1] if isinstance(m, ToolMessage)]
    assert json.loads(child_results[0].content)["row_count"] == 4
    assert "local-data-analysis" in str(model.seen_messages[0][0].content)
    assert "analysis-reporting" in str(model.seen_messages[0][0].content)
    assert not state.get("skills_metadata")


def test_tool_errors_are_reported_as_error_messages(setup):
    """Production tool wrapper preserves failure semantics for the model."""
    root, _ = setup
    tool = create_data_tools(root)[0]
    message = tool.invoke({"name": "inspect_table", "args": {"file_path": "../secret.csv"}, "id": "bad", "type": "tool_call"})
    assert message.status == "error"


@pytest.mark.parametrize("name,content", [("corrupt.xlsx", b"not a zip"), ("oversized_cell.csv", b"column\n" + b"x" * 200000)], ids=["corrupt-xlsx", "oversized-csv-cell"])
def test_corrupt_files_remain_tool_errors(setup, name, content):
    """Parser exceptions must not abort the Agent graph."""
    root, _ = setup
    (root / "data/inputs" / name).write_bytes(content)
    message = create_data_tools(root)[0].invoke({"name": "inspect_table", "args": {"file_path": name}, "id": "corrupt", "type": "tool_call"})
    assert message.status == "error"
    assert "文件损坏" in message.content


@pytest.mark.parametrize("value", ["x" * 40000, "text\x01invalid"], ids=["long-cell", "invalid-control"])
def test_export_rejects_excel_data_loss(setup, value):
    """Export rejects unrepresentable Excel values rather than silently truncating."""
    root, tools = setup
    (root / "data/inputs/long.csv").write_text("label\n" + value + "\n", encoding="utf-8")
    identity = tools["inspect_table"].invoke({"file_path": "long.csv"})["dataset_id"]
    result = tools["analyze_table"].invoke({"dataset_id": identity, "group_by": ["label"], "operation": "count"})
    with pytest.raises(ToolException, match="Excel 不支持"):
        tools["export_report"].invoke({"result_id": result["result_id"]})
    assert not list((root / "outputs/analysis" / result["result_id"]).glob("report-*"))
