"""搜索工具的离线测试，仅在终端打印过程和结果。

直接运行：python tests/test_search_tools.py（执行一个搜索问题演示）。
全部用例：python -m pytest tests/test_search_tools.py -s -v。
"""

import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from types import SimpleNamespace

import httpx
import pytest

from infra.settings import Settings
from scripts._common import _redact


def print_value(label: str, value: Any) -> None:
    """打印脱敏后的 JSON 预览，限制终端输出长度，避免长正文刷屏。"""
    text = json.dumps(_redact(value), ensure_ascii=False, indent=2, default=str)
    if len(text) > 1500:
        text = text[:1500] + "\n……（控制台预览已截断，断言仍检查完整返回值）"
    print(f"\n[{label}]\n{text}", flush=True)


def print_tool_log(message: str, *args: Any) -> None:
    """作为测试专用日志输出，不初始化应用 logger 或任何文件 sink。"""
    print(f"[工具日志] {message.format(*args)}", flush=True)


def invoke_and_print(tool: Any, arguments: dict[str, Any]) -> Any:
    """执行离线工具调用，打印输入、工具状态、耗时和返回内容，保留原始结果。"""
    print(f"\n========== 离线工具调用：{tool.name} ==========", flush=True)
    print_value("工具输入", arguments.get("args", arguments))
    started = time.perf_counter()
    result = asyncio.run(tool.ainvoke(arguments))
    content = getattr(result, "content", result)
    try:
        preview = json.loads(content)
    except (ValueError, TypeError):
        preview = content
    status = getattr(result, "status", "返回字符串，成功与否由断言校验")
    print(f"[工具状态] {status} | 耗时 {(time.perf_counter() - started) * 1000:.0f} ms", flush=True)
    print_value("工具返回", preview)
    return result


def test_console_preview_redacts_credentials_and_limits_large_results(capsys: pytest.CaptureFixture[str]) -> None:
    """输出应便于阅读，且不能把认证信息或超长正文打印到终端。"""
    print_value("测试返回", {"api_key": "private-key", "url": "https://user:private-pass@example.com",
                           "content": "long-body-" * 1000})
    output = capsys.readouterr().out
    assert "测试返回" in output and "***" in output
    assert "private-key" not in output and "private-pass" not in output
    assert "已截断" in output and len(output) < 1800


def test_tool_test_prints_without_creating_log_files(
    search_tools: Any, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """测试请求仅打印到终端，不能初始化应用的文件日志。"""
    monkeypatch.chdir(tmp_path)
    install_transport(search_tools, monkeypatch, lambda request: httpx.Response(200, json={"results": []}))
    result = invoke_and_print(search_tools.web_search, {"query": "docs"})
    assert json.loads(result)["results"] == []
    assert list(tmp_path.iterdir()) == []
    output = capsys.readouterr().out
    assert "工具输入" in output and "工具返回" in output and "模拟 HTTP 请求" in output


@pytest.fixture
def search_tools(monkeypatch: pytest.MonkeyPatch) -> Any:
    """隔离真实环境配置和密钥，测试只使用 MockTransport。"""
    assert importlib.util.find_spec("tools.search_tools") is not None, "需要实现 tools/search_tools.py"
    module = importlib.import_module("tools.search_tools")
    settings = Settings(_env_file=None, api_key="offline-search-key",
                        base_url="https://api.deepseek.com/v1", timeout=5)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    terminal_logger = SimpleNamespace(info=print_tool_log, warning=print_tool_log)
    monkeypatch.setattr(module, "get_logger", lambda: terminal_logger)
    return module


def install_transport(module: Any, monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """保留真实 HTTPX 客户端协议，替换网络传输为本地响应。"""
    client_type = httpx.AsyncClient

    def traced_handler(request: httpx.Request) -> httpx.Response:
        print(f"[模拟 HTTP 请求] {request.method} {request.url}", flush=True)
        # 不输出请求头：Authorization 中包含测试或实际认证信息。
        print_value("HTTP 请求体", json.loads(request.content))
        response = handler(request)
        print(f"[模拟 HTTP 响应] {response.status_code}", flush=True)
        return response

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(traced_handler), **kwargs,
    ))


def test_web_search_question_demo(search_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟用户提问，执行真实工具封装，并在终端展示模拟搜索结果。"""
    question = "LangGraph 如何通过 checkpoint 保存会话状态？"
    mock_results = [
        {
            "title": "LangGraph 会话持久化示例（模拟结果）",
            "url": "https://example.com/langgraph/persistence",
            "summary": "示例摘要：构建图时配置 checkpointer，运行时通过 thread_id 标识会话。",
        },
        {
            "title": "LangGraph checkpoint 使用示例（模拟结果）",
            "url": "https://example.com/langgraph/checkpoints",
            "summary": "示例摘要：同一图实例复用 thread_id 可恢复对应会话；持久化能力取决于 checkpointer。",
        },
    ]
    print(f"\n[模拟用户问题] {question}")
    print("[测试模式] 离线模拟搜索服务，不调用模型或真实搜索 API，不生成日志文件。")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/tools/search"
        assert json.loads(request.content) == {"query": question, "count": 2, "need_content": False}
        return httpx.Response(200, json={"results": mock_results})

    install_transport(search_tools, monkeypatch, handler)
    message = invoke_and_print(search_tools.web_search, {
        "name": "web_search", "args": {"query": question, "max_results": 2},
        "id": "question-demo", "type": "tool_call",
    })
    assert message.status == "success"
    data = json.loads(message.content)
    assert data["query"] == question and data["count"] == 2
    assert [item["summary"] for item in data["results"]] == [item["summary"] for item in mock_results]
    assert [item["url"] for item in data["results"]] == [item["url"] for item in mock_results]

    print(f"\n[问题对应的模拟搜索结果] 共 {data['count']} 条")
    for index, item in enumerate(data["results"], 1):
        print(f"  {index}. {item['title']}\n     链接：{item['url']}\n     摘要：{item['summary']}")
    print("[校验通过] 问题已正确发送给模拟搜索服务，工具返回的链接和摘要与模拟响应一致。")


def test_search_sends_correct_request_and_limits_results(search_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["Authorization"] == "Bearer offline-search-key"
        assert json.loads(request.content) == {"query": "LangGraph docs", "count": 1, "need_content": False}
        return httpx.Response(200, json={"results": [
            {"title": "Official docs", "url": "https://example.com/docs", "summary": "使用说明", "date": "2026-10-01"},
            {"title": "Extra", "url": "https://example.com/extra", "snippet": "extra"},
        ]})

    install_transport(search_tools, monkeypatch, handler)
    result = invoke_and_print(search_tools.web_search, {"query": " LangGraph docs ", "max_results": 1})
    data = json.loads(result)
    assert data["query"] == "LangGraph docs" and len(data["results"]) == 1
    assert data["results"][0]["summary"] == "使用说明"
    assert data["results"][0]["url"] == "https://example.com/docs"
    print("[校验通过] 请求参数正确，返回模拟响应中的摘要和链接，结果数符合限制。")


def test_empty_search_is_valid_not_fabricated(search_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    install_transport(search_tools, monkeypatch, lambda request: httpx.Response(200, json={"results": []}))
    data = json.loads(invoke_and_print(search_tools.web_search, {"query": "no matches"}))
    assert data["results"] == [] and data["count"] == 0
    print("[校验通过] 空搜索返回空列表，没有编造结果。")


def test_fetch_returns_markdown_and_marks_truncation(search_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    content = "# 页面正文\n" + "内容" * 30000

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/tools/fetch"
        assert json.loads(request.content) == {"url": "https://example.com/docs"}
        return httpx.Response(200, json={"title": "文档", "content": content})

    install_transport(search_tools, monkeypatch, handler)
    data = json.loads(invoke_and_print(search_tools.fetch_url, {"url": "https://example.com/docs"}))
    assert data["title"] == "文档" and data["url"] == "https://example.com/docs"
    assert data["content"].startswith("# 页面正文")
    assert data["truncated"] and len(data["content"]) <= 20000
    print(f"[校验通过] Markdown 正文有效，长度={len(data['content'])}，truncated={data['truncated']}。")


@pytest.mark.parametrize("payload", [{}, {"results": "wrong"}, {"results": ["wrong"]}])
def test_malformed_search_response_is_tool_error(search_tools: Any, monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    install_transport(search_tools, monkeypatch, lambda request: httpx.Response(200, json=payload))
    message = invoke_and_print(search_tools.web_search, {
        "name": "web_search", "args": {"query": "docs"}, "id": "bad-response", "type": "tool_call",
    })
    assert message.status == "error" and "响应" in message.content
    print("[校验通过] 异常响应结构被标记为工具错误。")


@pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 500])
def test_http_errors_are_safe_and_not_retried(search_tools: Any, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "sensitive-server-message offline-search-key"}})

    install_transport(search_tools, monkeypatch, handler)
    message = invoke_and_print(search_tools.fetch_url, {
        "name": "fetch_url", "args": {"url": "https://example.com"}, "id": "error", "type": "tool_call",
    })
    assert message.status == "error" and str(status) in message.content
    assert "offline-search-key" not in message.content and "sensitive-server-message" not in message.content
    assert len(calls) == 1
    print(f"[校验通过] HTTP {status} 正确转换为工具错误，无密钥泄露，没有自动重试。")


@pytest.mark.parametrize("kind", ["timeout", "network", "json", "redirect", "large"])
def test_transport_response_failures_are_reported(search_tools: Any, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout("private detail", request=request)
        if kind == "network":
            raise httpx.ConnectError("private detail", request=request)
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example"})
        if kind == "large":
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        return httpx.Response(200, text="not-json")

    install_transport(search_tools, monkeypatch, handler)
    message = invoke_and_print(search_tools.web_search, {
        "name": "web_search", "args": {"query": "docs"}, "id": "transport", "type": "tool_call",
    })
    assert message.status == "error" and "private detail" not in message.content
    print(f"[校验通过] {kind} 场景被识别为错误，未暴露底层异常详情。")


@pytest.mark.parametrize("tool_name,args", [
    ("web_search", {"query": " "}),
    ("web_search", {"query": "x" * 129}),
    ("web_search", {"query": "docs", "max_results": 0}),
    ("web_search", {"query": "docs", "max_results": 51}),
    ("web_search", {"query": "docs", "max_results": True}),
    ("fetch_url", {"url": "file:///etc/passwd"}),
    ("fetch_url", {"url": "https://user:password@example.com"}),
    ("fetch_url", {"url": "https://"}),
])
def test_invalid_inputs_never_request_network(search_tools: Any, monkeypatch: pytest.MonkeyPatch, tool_name: str, args: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("参数错误时不应联网")

    install_transport(search_tools, monkeypatch, handler)
    message = invoke_and_print(getattr(search_tools, tool_name), {
        "name": tool_name, "args": args, "id": "invalid", "type": "tool_call",
    })
    assert message.status == "error"
    print("[校验通过] 无效参数已拒绝，没有发出 HTTP 请求。")


def test_other_provider_key_is_not_sent_to_deepseek(search_tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, api_key="unrelated-provider-key", base_url="https://other.example/v1")
    monkeypatch.setattr(search_tools, "get_settings", lambda: settings)
    install_transport(search_tools, monkeypatch, lambda request: pytest.fail("不能向其它平台发送密钥"))
    message = invoke_and_print(search_tools.web_search, {
        "name": "web_search", "args": {"query": "docs"}, "id": "provider", "type": "tool_call",
    })
    assert message.status == "error" and "SYC_BASE_URL" in message.content
    print("[校验通过] 其他平台配置被拒绝，没有向 DeepSeek 发送密钥。")


@pytest.mark.parametrize("payload", [{}, {"content": ""}, {"content": 42}])
def test_missing_fetch_body_is_not_success(search_tools: Any, monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    install_transport(search_tools, monkeypatch, lambda request: httpx.Response(200, json=payload))
    message = invoke_and_print(search_tools.fetch_url, {
        "name": "fetch_url", "args": {"url": "https://example.com"}, "id": "empty", "type": "tool_call",
    })
    assert message.status == "error"
    print("[校验通过] 缺失或无效正文被标记为失败。")


@pytest.mark.parametrize("mode", ["file", "module"])
def test_tools_import_outside_project_without_pythonpath(tmp_path: Path, mode: str) -> None:
    """文件加载和模块导入均不应触发网络或依赖当前工作目录。"""
    root = Path(__file__).resolve().parents[1]
    code = (
        "import runpy,sys; runpy.run_path(sys.argv[1], run_name='import_check')"
        if mode == "file" else "from tools.search_tools import web_search, fetch_url"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root / "tools/search_tools.py")],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": "", "SYC_API_KEY": "offline-placeholder", "LANGSMITH_TRACING": "false"},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    print(f"[校验通过] 项目外 {mode} 导入成功，未请求网络。")


@pytest.mark.parametrize("launch", ["file", "module"])
def test_direct_launch_prints_question_and_results(tmp_path: Path, launch: str) -> None:
    """直接运行文件或模块时，必须真正执行演示并输出模拟结果。"""
    root = Path(__file__).resolve().parents[1]
    command = (
        [sys.executable, str(Path(__file__).resolve())]
        if launch == "file" else [sys.executable, "-m", "tests.test_search_tools"]
    )
    completed = subprocess.run(
        command, cwd=tmp_path if launch == "file" else root,
        env=os.environ | {"PYTHONPATH": "", "PYTHONUTF8": "1", "PYTEST_ADDOPTS": "",
                          "SYC_API_KEY": "offline-placeholder", "LANGSMITH_TRACING": "false"},
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "模拟用户问题" in completed.stdout
    assert "问题对应的模拟搜索结果" in completed.stdout
    assert "校验通过" in completed.stdout and "1 passed" in completed.stdout
    assert not (tmp_path / "logs").exists()


def main() -> int:
    """直接运行时通过 pytest 执行问题演示，关闭输出捕获和文件缓存。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    target = f"{Path(__file__).resolve()}::test_web_search_question_demo"
    return int(pytest.main([target, "-s", "-v", "-p", "no:cacheprovider"]))


if __name__ == "__main__":
    raise SystemExit(main())
