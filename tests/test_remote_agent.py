"""不创建云端环境，通过真实图验证远程模式工具和路径配置。"""
import asyncio

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from langchain_core.messages import AIMessage, ToolMessage

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from agent.main import build_agent as _build_agent
from tests.test_development_registration import CapturingModel


class TestSandbox(FilesystemBackend, SandboxBackendProtocol):
    """仅测试使用的协议适配器；execute 返回可断言标记，不执行本机命令。"""

    __test__ = False

    @property
    def id(self) -> str:
        return "test-sandbox"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return ExecuteResponse(output="REMOTE_EXECUTED", exit_code=0, truncated=False)


def build_agent(**kwargs):
    """验收脚本显式使用内存存储，不声称跨进程持久化。"""
    return _build_agent(checkpointer=InMemorySaver(), store=InMemoryStore(), **kwargs)


def test_recompiled_graph_restores_messages_and_binds_store(tmp_path):
    saver, store = InMemorySaver(), InMemoryStore()
    async def run():
        config = {'configurable': {'thread_id': 'persistent-test'}}
        for prompt in ['first', 'second']:
            model = CapturingModel(replies=[AIMessage(content='ok')])
            graph = _build_agent(backend=TestSandbox(root_dir=tmp_path), workdir='/workspace',
                                 model=model, checkpointer=saver, store=store)
            assert graph.store is store and graph.checkpointer is saver
            state = await graph.ainvoke({'messages': [{'role': 'user', 'content': prompt}]}, config)
        users = [m.content for m in state['messages'] if m.type == 'human']
        assert users == ['first', 'second']
    asyncio.run(run())


def test_remote_agent_registers_delegation_without_local_pytest(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("REMOTE_RULE_SENTINEL", encoding="utf-8")
    model = CapturingModel(replies=[AIMessage(content="", tool_calls=[{
        "name": "execute", "args": {"command": "python -m pytest"}, "id": "exec", "type": "tool_call"}]), AIMessage(content="done")])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=model)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "run"}]}, {"configurable": {"thread_id": "remote"}}))
    assert {"execute", "web_search", "fetch_url", "read_file", "task"} <= model.names
    assert not {"run_pytest", "inspect_table"} & model.names
    assert "REMOTE_RULE_SENTINEL" not in str(model.seen_messages[0][0].content)
    assert "通用开发 Agent 工作约定" in str(model.seen_messages[0][0].content)
    assert "先阅读 AGENTS.md" not in str(model.seen_messages[0][0].content)
    assert any(isinstance(m, ToolMessage) and "REMOTE_EXECUTED" in str(m.content) for m in result["messages"])


def test_remote_builder_rejects_local_backend(tmp_path):
    with pytest.raises(TypeError, match="沙箱"):
        build_agent(backend=FilesystemBackend(root_dir=tmp_path), workdir="/workspace")


def call(name, args, identity="call"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identity, "type": "tool_call"}])


def test_remote_development_delegation_shares_files_and_role_components(tmp_path):
    """真实图顺序委派；模型为离线替身，文件工具真实读写隔离目录。"""
    import json
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("REMOTE_RULE_SENTINEL", encoding="utf-8")
    skill = root / ".agents/skills/development"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: remote-development-skill\ndescription: Development rules.\n---\nRead before coding.", encoding="utf-8")
    verdict = {"approved": True, "summary": "Reviewed", "issues": [], "security_findings": [], "verification": "REMOTE_EXECUTED"}
    children = {
        "coder": CapturingModel(replies=[call("write_file", {"file_path": "/workspace/created.py", "content": "VALUE = 42"}), AIMessage(content="created.py ready")]),
        "tester": CapturingModel(replies=[call("read_file", {"file_path": "/workspace/created.py"}), call("execute", {"command": "cd /workspace && python -m pytest"}, "exec"), AIMessage(content="REMOTE_EXECUTED")]),
        "reviewer": CapturingModel(replies=[call("execute", {"command": "touch forbidden"}), call("write_file", {"file_path": "/workspace/forbidden", "content": "bad"}, "write"), call("ReviewResult", verdict, "verdict")]),
    }
    parent = CapturingModel(replies=[call("task", {"subagent_type": name, "description": "Use /workspace and report evidence"}, name) for name in children] + [AIMessage(content="done")])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=parent, subagent_models=children)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Implement, test, review"}]}, {"configurable": {"thread_id": "pipeline"}}))
    assert (root / "created.py").read_text() == "VALUE = 42"
    assert not (root / "forbidden").exists()
    returned = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(returned) == 3 and all(m.status == "success" for m in returned)
    assert json.loads(returned[-1].content) == verdict
    assert "VALUE = 42" in str(children["tester"].seen_messages[-1])
    for model in children.values():
        assert "通用开发 Agent 工作约定" in str(model.seen_messages[0])
        assert "my-claw-code-quality" in str(model.seen_messages[0])
        assert not {"run_pytest", "task", "inspect_table"} & model.names
    assert {"write_file", "execute"} <= children["tester"].names
    assert not {"write_file", "edit_file", "execute"} & children["reviewer"].names
    assert sum(isinstance(m, ToolMessage) and m.status == "error" for m in children["reviewer"].seen_messages[-1]) == 2


def test_remote_roles_share_backend_and_have_independent_limits(tmp_path):
    from subagents.profiles import build_remote_subagents
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from agent.guidance import guidance_backend
    from agent.main import PROJECT_ROOT
    backend = guidance_backend(TestSandbox(root_dir=tmp_path), PROJECT_ROOT)
    roles = {s["name"]: s for s in build_remote_subagents(backend, "/workspace", max_model_calls=3)}
    assert set(roles) == {"planner", "researcher", "coder", "tester", "reviewer", "data_analyst", "general-purpose"}
    for name, spec in roles.items():
        fs = spec["middleware"][0]
        assert fs.backend is backend
        names = {t.name for t in fs.tools}
        if name in {"planner", "researcher", "reviewer", "general-purpose"}:
            assert not names & {"execute", "write_file", "edit_file"}
        else:
            assert "execute" in names
        assert all(path.startswith("/guidance/") for path in spec["skills"])
        assert any(isinstance(m, ModelCallLimitMiddleware) for m in spec["middleware"])
    assert roles["data_analyst"]["skills"] != roles["coder"]["skills"]
    with pytest.raises(ValueError):
        build_remote_subagents(backend, "/workspace", models={"unknown": CapturingModel(replies=[])})


def test_remote_allows_independent_readonly_delegation(tmp_path):
    children = {name: CapturingModel(replies=[AIMessage(content=name)]) for name in ("planner", "researcher")}
    parent = CapturingModel(replies=[AIMessage(content="", tool_calls=[
        {"name": "task", "args": {"subagent_type": name, "description": "Read only"}, "id": name, "type": "tool_call"} for name in children
    ]), AIMessage(content="done")])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=parent, subagent_models=children)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Research"}]}, {"configurable": {"thread_id": "parallel"}}))
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage) and m.status == "success"]) == 2


def test_remote_default_budget_allows_longer_delegate(tmp_path):
    """读文件等准备工作超过原来的 12 次后，仍能正常结束委派。"""
    child = CapturingModel(replies=[
        *[call("execute", {"command": "echo inspect"}) for _ in range(13)],
        AIMessage(content="Inspection complete"),
    ])
    parent = CapturingModel(replies=[
        call("task", {"subagent_type": "coder", "description": "Inspect code"}),
        AIMessage(content="done"),
    ])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=parent,
                        subagent_models={"coder": child})
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Inspect"}]},
                                      {"configurable": {"thread_id": "long-delegate"}}))
    assert len(child.seen_messages) == 14
    assert result["messages"][-1].content == "done"


def test_remote_child_model_limit_stops_runaway_delegate(tmp_path):
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
    child = CapturingModel(replies=[call("read_file", {"file_path": "/workspace/missing.py"})])
    parent = CapturingModel(replies=[call("task", {"subagent_type": "coder", "description": "Inspect code"})])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=parent,
                        subagent_models={"coder": child}, max_subagent_model_calls=1)
    with pytest.raises(ModelCallLimitExceededError):
        asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Inspect"}]}, {"configurable": {"thread_id": "limit"}}))
    assert len(child.seen_messages) == 1


def test_remote_analyst_discovers_only_analysis_skills(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("REMOTE_ANALYSIS_RULE", encoding="utf-8")
    skill = root / "subagents/skills/data-analysis/example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: remote-analysis-skill\ndescription: Analyze tables.\n---\nUse remote Python.", encoding="utf-8")
    child = CapturingModel(replies=[AIMessage(content="No input provided")])
    parent = CapturingModel(replies=[call("task", {"subagent_type": "data_analyst", "description": "Inspect available skills"}), AIMessage(content="done")])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=parent, subagent_models={"data_analyst": child})
    asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Analyze"}]}, {"configurable": {"thread_id": "analysis"}}))
    assert "local-data-analysis" in str(child.seen_messages[0])
    assert "REMOTE_ANALYSIS_RULE" not in str(child.seen_messages[0])
    assert "execute" in child.names and "inspect_table" not in child.names


def test_deepseek_review_model_disables_thinking_without_mutating_parent():
    from langchain_openai import ChatOpenAI
    from infra.model_policy import model_for_role
    parent = ChatOpenAI(model="deepseek-flash", api_key="test-placeholder",
                        base_url="https://api.deepseek.com", extra_body={"custom": "kept"})
    reviewer = model_for_role(parent, "reviewer")
    assert reviewer is not parent
    assert reviewer.extra_body == {"custom": "kept", "thinking": {"type": "disabled"}}
    assert parent.extra_body == {"custom": "kept"}
    assert reviewer.reasoning_effort is None
    other = ChatOpenAI(model="another-model", api_key="test-placeholder", base_url="https://example.com/v1")
    assert model_for_role(other, "reviewer") is other


def test_registered_middleware_serializes_same_turn_delegations(tmp_path):
    """模型同轮发出两个 task，实际子模型执行区间不得重叠。"""
    active = 0
    peak = 0
    class SlowModel(CapturingModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.03)
                return self._generate(messages, stop=stop, **kwargs)
            finally:
                active -= 1
    children = {name: SlowModel(replies=[AIMessage(content=name)]) for name in ("planner", "researcher")}
    parent = CapturingModel(replies=[AIMessage(content="", tool_calls=[
        {"name": "task", "args": {"subagent_type": name, "description": "Read only"}, "id": name, "type": "tool_call"} for name in children
    ]), AIMessage(content="done")])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace", model=parent, subagent_models=children)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Inspect"}]}, {"configurable": {"thread_id": "serialized"}}))
    assert peak == 1 and active == 0
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage)]) == 2


def test_guidance_is_readable_but_not_writable_by_parent_or_child(tmp_path):
    child = CapturingModel(replies=[call("edit_file", {
        "file_path": "/guidance/AGENTS.md", "old_string": "通用开发", "new_string": "evil",
    }), AIMessage(content="done")])
    parent = CapturingModel(replies=[
        call("read_file", {"file_path": "/guidance/AGENTS.md"}),
        call("write_file", {"file_path": "/guidance/injected.md", "content": "bad"}, "write"),
        call("task", {"subagent_type": "coder", "description": "test"}, "delegate"),
        AIMessage(content="done"),
    ])
    agent = build_agent(backend=TestSandbox(root_dir=tmp_path), workdir="/workspace",
                        model=parent, subagent_models={"coder": child})
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "test"}]},
                                      {"configurable": {"thread_id": "readonly-guidance"}}))
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert "通用开发 Agent 工作约定" in str(messages[0].content)
    assert "denied" in str(messages[1].content)
    assert "denied" in str(child.seen_messages[-1])
