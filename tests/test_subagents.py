"""实际 DeepAgents 委派、角色权限和审查 JSON 的离线集成验收。"""
import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from agent.main import build_local_agent as build_agent
from subagents.profiles import build_subagents
from subagents.schemas import ReviewResult
from scripts._verification import ScriptedModel
from tools.test_tools import create_test_tools


def call(name, args, identity="call"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identity, "type": "tool_call"}])


def test_reviewer_returns_json_and_rejects_writes(tmp_path):
    (tmp_path / "AGENTS.md").write_text("RULE_SENTINEL", encoding="utf-8")
    verdict = {"approved": True, "summary": "No blocker", "issues": [], "security_findings": [], "verification": "Not executed"}
    reviewer = ScriptedModel(replies=[call("write_file", {"file_path": "/forbidden.txt", "content": "bad"}), call("ReviewResult", verdict, "review")])
    parent = ScriptedModel(replies=[call("task", {"subagent_type": "reviewer", "description": "Review workspace"}), AIMessage(content="done")])
    agent = build_agent(workspace_root=tmp_path, model=parent, enable_subagents=True, subagent_models={"reviewer": reviewer}, max_model_calls=2)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Review"}]}, {"configurable": {"thread_id": "review"}}))
    returned = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert json.loads(returned.content) == verdict
    assert not (tmp_path / "forbidden.txt").exists()
    assert any(isinstance(m, ToolMessage) and m.status == "error" for m in reviewer.seen_messages[-1])
    assert "RULE_SENTINEL" in str(reviewer.seen_messages[0][0].content)


def test_roles_expose_only_required_tools(tmp_path):
    profiles = {spec["name"]: spec for spec in build_subagents(tmp_path)}
    assert {t.name for t in profiles["data_analyst"]["tools"]} == {"inspect_table", "analyze_table", "plot_analysis", "export_report"}
    assert {t.name for t in profiles["tester"]["tools"]} == {"run_pytest"}
    for name in ("planner", "researcher", "reviewer", "data_analyst", "general-purpose"):
        fs = profiles[name]["middleware"][0]
        assert {t.name for t in fs.tools} <= {"ls", "read_file", "glob", "grep"}
    assert profiles["data_analyst"]["skills"] != profiles["coder"]["skills"]
    with pytest.raises(ValueError):
        build_subagents(tmp_path, models={"typo": ScriptedModel(replies=[])})


def test_test_tool_runs_bound_workspace_and_rejects_escape(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_bound.py").write_text("def test_bound():\n    assert True\n", encoding="utf-8")
    tool = create_test_tools(tmp_path)[0]
    assert "1 passed" in asyncio.run(tool.ainvoke({"path": "tests/"}))
    result = asyncio.run(tool.ainvoke({"name": "run_pytest", "args": {"path": str(Path(__file__).resolve())}, "id": "escape", "type": "tool_call"}))
    assert result.status == "error"


def test_review_cannot_approve_blocker():
    with pytest.raises(ValidationError):
        ReviewResult(approved=True, summary="bad", issues=[{"severity": "error", "file": "/a.py", "detail": "bug"}], security_findings=[], verification="none")


def test_parallel_delegation_keeps_usage_and_context_private(tmp_path):
    """Parallel child writes must neither conflict nor overwrite parent telemetry."""
    usage = {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
    parent = ScriptedModel(replies=[AIMessage(content="", usage_metadata=usage, tool_calls=[
        {"name": "task", "args": {"subagent_type": role, "description": "Summarize"}, "id": role, "type": "tool_call"}
        for role in ("planner", "researcher")
    ]), AIMessage(content="done", usage_metadata=usage)])
    children = {role: ScriptedModel(replies=[AIMessage(content="child", usage_metadata=usage)]) for role in ("planner", "researcher")}
    agent = build_agent(workspace_root=tmp_path, model=parent, enable_subagents=True, subagent_models=children, max_model_calls=2)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Delegate"}]}, {"configurable": {"thread_id": "parallel"}}))
    state = agent.get_state({"configurable": {"thread_id": "parallel"}}).values
    assert state["total_input_tokens"] == 20
    assert state["total_output_tokens"] == 2
    assert state["channel"] == "cli"
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage) and m.status == "success"]) == 2
