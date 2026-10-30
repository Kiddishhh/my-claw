"""默认主代理工具注册、委派禁用和隔离测试通路。"""
import asyncio

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field

from agent.main import build_local_agent as build_agent
from scripts._verification import ScriptedModel
from tests.verify_development_tools import run_verification


class CapturingModel(ScriptedModel):
    """记录模型实际收到的工具列表。"""

    names: set[str] = Field(default_factory=set)

    def bind_tools(self, tools, **kwargs):
        self.names.update(t.name if hasattr(t, "name") else t.get("name", "") for t in tools)
        return self


def test_default_registration_and_task_rejected(tmp_path):
    model = CapturingModel(replies=[AIMessage(content="", tool_calls=[{
        "name": "task", "args": {"subagent_type": "coder", "description": "write"}, "id": "blocked", "type": "tool_call"}]), AIMessage(content="done")])
    agent = build_agent(workspace_root=tmp_path, model=model, max_model_calls=2)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "probe"}]}, {"configurable": {"thread_id": "registration"}}))
    assert {"web_search", "fetch_url", "run_pytest", "read_file", "write_file", "edit_file", "ls", "glob", "grep"} <= model.names
    assert not model.names & {"task", "ReviewResult", "inspect_table", "analyze_table", "plot_analysis", "export_report"}
    assert any(isinstance(m, ToolMessage) and m.name == "task" and m.status == "error" for m in result["messages"])


def test_development_verification_offline(tmp_path):
    report = asyncio.run(run_verification(offline=True, log_root=tmp_path))
    assert report["passed"], report
    assert not {"local-data-analysis", "analysis-reporting"} & set(report["skills"])
