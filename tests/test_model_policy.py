"""角色预算配置与 DeepSeek 工具调用协议；不请求真实 API。"""
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from infra.model_policy import DeepSeekChatModel, model_for_role


@pytest.mark.parametrize("role", ["main", "planner", "coder", "tester", "researcher", "reviewer", "data_analyst", "general-purpose"])
def test_roles_are_isolated(role):
    original = DeepSeekChatModel(model="deepseek-flash", api_key="test", extra_body={"custom": "kept"})
    configured = model_for_role(original, role)
    thinking = role in {"planner", "coder", "data_analyst"}
    assert configured.extra_body == {"custom": "kept", "thinking": {"type": "enabled" if thinking else "disabled"}}
    assert configured.reasoning_effort == ("low" if thinking else None)
    assert original.extra_body == {"custom": "kept"}


def test_other_providers_unchanged():
    model = ChatOpenAI(model="example", api_key="test", base_url="https://example.com/v1")
    assert model_for_role(model, "coder") is model


def test_reasoning_roundtrip_with_tool_result():
    from openai.types.chat import ChatCompletion
    model = model_for_role(DeepSeekChatModel(model="deepseek-flash", api_key="test"), "coder")
    response = ChatCompletion.model_validate({
        "id": "test", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "reasoning_content": "test reasoning",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}],
        }}],
    })
    message = model._create_chat_result(response).generations[0].message
    payload = model._get_request_payload([
        HumanMessage(content="inspect"), message, ToolMessage(content="files", tool_call_id="t1")
    ], tools=[{"type": "function", "function": {"name": "ls", "parameters": {"type": "object"}}}])
    assert payload["messages"][1]["reasoning_content"] == "test reasoning"
    assert payload["reasoning_effort"] == "low"
    assert payload["extra_body"]["thinking"] == {"type": "enabled"}


def test_build_model_uses_deepseek_adapter_with_existing_settings(monkeypatch):
    from agent import main
    from infra.settings import Settings
    monkeypatch.setattr(main, "get_settings", lambda: Settings(
        _env_file=None, api_key="test", model_name="deepseek-flash", model_provider="openai",
        base_url="https://api.deepseek.com", timeout=17, max_retries=2))
    model = main.build_model()
    assert isinstance(model, DeepSeekChatModel)
    assert model.extra_body["thinking"] == {"type": "disabled"}
    assert model.request_timeout == 17


@pytest.mark.parametrize("remote", [False, True])
def test_subagent_builders_apply_roles(tmp_path, remote):
    from subagents.profiles import build_remote_subagents, build_subagents
    from tests.test_remote_agent import TestSandbox
    from deepagents.backends import CompositeBackend, FilesystemBackend
    original = model_for_role(DeepSeekChatModel(model="deepseek-flash", api_key="test"), "main")
    # 与正式入口一致：只读指导路由和可执行沙箱必须组合后交给子代理。
    backend = CompositeBackend(default=TestSandbox(root_dir=tmp_path), routes={
        "/guidance/": FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    })
    specs = (build_remote_subagents(backend, "/workspace", default_model=original)
             if remote else build_subagents(tmp_path, default_model=original))
    for spec in specs:
        expected = "enabled" if spec["name"] in {"coder", "planner", "data_analyst"} else "disabled"
        assert spec["model"].extra_body["thinking"]["type"] == expected
    assert original.extra_body["thinking"]["type"] == "disabled"
