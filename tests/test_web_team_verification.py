"""验收判据本身也需防止误报，不调用真实模型或 Daytona。"""
import json
import pytest
from langchain.messages import AIMessage, ToolMessage

from scripts.verify_web_team import review_of, check_agent_run, verify_saved_files


def task(role, identity):
    return AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": role},
                                              "id": identity, "type": "tool_call"}])


def test_review_must_belong_to_last_reviewer_call():
    verdict = json.dumps({"approved": True, "summary": "ok", "issues": [],
                          "security_findings": [], "verification": "tested"})
    state = {"messages": [task("coder", "c"), ToolMessage(content=verdict, tool_call_id="c")]}
    assert review_of(state) is None
    state["messages"] += [task("reviewer", "r"), ToolMessage(content=verdict, tool_call_id="r")]
    assert review_of(state).approved
    state["messages"] += [task("reviewer", "r2"), ToolMessage(content="invalid", tool_call_id="r2")]
    assert review_of(state) is None  # 不回退到旧的 approved。


def test_reordered_pipeline_and_parent_execution_fail_checks():
    state = {"messages": [task("tester", "t"), task("coder", "c"), task("reviewer", "r"),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "true"}, "id": "e", "type": "tool_call"}])]}
    checks = check_agent_run(state)
    assert not checks[0].passed and not checks[1].passed


def test_saved_files_rejects_missing_and_tampered_artifact(tmp_path):
    import hashlib
    project = tmp_path / "project"
    project.mkdir()
    content = b"<html>hello</html>"
    (project / "index.html").write_bytes(content)
    manifest = [{"path": "index.html", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert verify_saved_files(tmp_path, ["index.html"])
    assert not verify_saved_files(tmp_path, ["missing.html"])
    (project / "index.html").write_bytes(b"changed")
    assert not verify_saved_files(tmp_path, ["index.html"])


@pytest.mark.parametrize("failure_phase", [None, "model", "validation"])
def test_script_runs_dispatcher_checks_restore_and_keeps_report(tmp_path, monkeypatch, failure_phase):
    """完整走脚本编排，但替换云端/模型；检查恢复阶段和最终清理没有被遗漏。"""
    import asyncio
    import hashlib
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts import verify_web_team as web
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web, "SandboxSettings", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(web, "build_model", lambda: object())
    client = Mock()
    client.list.return_value = []
    client.get.side_effect = lambda identity: SimpleNamespace(id=identity)
    monkeypatch.setattr(web, "create_daytona_client", lambda settings: client)
    files = {name: f"test {name}".encode() for name in web.REQUIRED}
    backend = Mock()
    backend.execute.return_value = SimpleNamespace(exit_code=1 if failure_phase == "validation" else 0,
                                                   output="WEB_TEAM_ASSERTIONS_OK")
    backend.download_files.side_effect = lambda paths: [SimpleNamespace(error=None, content=files[p.removeprefix('/work/')]) for p in paths]
    uses = []
    @contextmanager
    def session(*args, **kw):
        identity = "original" if len(uses) < 2 else "restored"
        uses.append(identity)
        yield backend, SimpleNamespace(id=identity), client, "/work"
    monkeypatch.setattr(web, "sandbox_for_thread", session)
    async def run(thread_id, message, **kw):
        if failure_phase == "model":
            raise RuntimeError("simulated model failure")
        assert kw["include_state"] and "coder → tester → reviewer" in message
        trace = kw["callbacks"][0]
        trace.model_calls = 4
        trace.memory_in_prompt = trace.skill_in_prompt = True
        trace.tools = {role: {"name": "task", "status": "success", "started": i*2, "ended": i*2+1}
                       for i, role in enumerate(web.PIPELINE)}
        trace.tools.update({"exec": {"name": "execute", "status": "success"},
                            "skill": {"name": "read_file", "args": {"file_path": "/guidance/development/a/SKILL.md"}, "status": "success"}})
        version = kw["project_root"] / "saved"
        manifest = []
        for name, content in files.items():
            path = version / "project" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            manifest.append({"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        (version / "manifest.json").write_text(json.dumps(manifest))
        messages = []
        for role in web.PIPELINE:
            messages.append(task(role, role))
            verdict = json.dumps({"approved": True, "summary": "ok", "issues": [], "security_findings": [], "verification": "tested"})
            messages.append(ToolMessage(content=verdict if role == "reviewer" else "done", tool_call_id=role))
        return {"sandbox_id": "original", "workspace_path": "saved", "workspace_saved": True,
                "reply": "done", "agent_state": {"messages": messages}}
    monkeypatch.setattr(web, "run_task", run)
    assert asyncio.run(web.verify(60, 6, 10)) == (0 if failure_phase is None else 1)
    assert uses == (["original"] if failure_phase == "model" else ["original", "original", "restored"])
    assert client.delete.call_count == (1 if failure_phase == "model" else 2)
    record = next((tmp_path / "logs/web-team-verification").glob("*/report.json"))
    assert json.loads(record.read_text(encoding="utf-8"))["passed"] is (failure_phase is None)
