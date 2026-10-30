"""直接运行默认真实模型验证开发工具；--offline 模拟模型/联网，pytest 仍真实执行。"""
import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import traceback
from unittest.mock import AsyncMock, patch
import uuid

from langchain_core.messages import AIMessage, ToolMessage

from agent.main import build_local_agent as build_agent
from scripts._common import _redact
from scripts._verification import ScriptedModel
from scripts._verification import VerificationTrace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {"web_search", "fetch_url", "run_pytest"}
EXPECTED_SKILLS = {"my-claw-code-quality", "systematic-debugging", "verification-before-completion"}


async def run_verification(*, offline: bool = False, log_root: Path | None = None) -> dict:
    """在独立目录验收工具调用、技能发现和子代理停用，保存脱敏报告。"""
    folder = (log_root or PROJECT_ROOT / "logs/development-verification") / uuid.uuid4().hex
    workspace = folder / "workspace"
    workspace.mkdir(parents=True)
    shutil.copytree(PROJECT_ROOT / ".agents/skills", workspace / ".agents/skills")
    shutil.copyfile(PROJECT_ROOT / "AGENTS.md", workspace / "AGENTS.md")
    test_dir = workspace / "tests"
    test_dir.mkdir()
    (test_dir / "test_smoke.py").write_text("def test_decimal():\n    from decimal import Decimal\n    assert Decimal('0.1') + Decimal('0.2') == Decimal('0.3')\n", encoding="utf-8")
    calls = [("web_search", {"query": "Python Decimal official documentation"}),
             ("fetch_url", {"url": "https://docs.python.org/3/library/decimal.html"}),
             ("run_pytest", {"path": "tests/test_smoke.py"})]
    fake = ScriptedModel(replies=[AIMessage(content="", tool_calls=[{
        "name": name, "args": args, "id": str(i), "type": "tool_call"}])
        for i, (name, args) in enumerate(calls)] + [AIMessage(content="开发工具验收完成")]) if offline else None
    trace = VerificationTrace(folder / "events.jsonl", "")
    config = {"configurable": {"thread_id": folder.name}, "callbacks": [trace], "recursion_limit": 100}
    report = {"passed": False, "mode": "offline" if offline else "live", "workspace": str(workspace)}
    print(f"开发工具验收：{report['mode']}；最多 8 次模型调用；总超时 180 秒")
    network = patch("tools.search_tools._request", new=AsyncMock(return_value={"title": "Offline fixture", "content": "Decimal represents exact decimal values.", "results": [{"title": "Offline fixture", "url": "https://docs.python.org/3/library/decimal.html"}]})) if offline else nullcontext()
    try:
        # 追踪统一遵循进程环境和项目 .env 配置。
        with network:
            agent = build_agent(workspace_root=workspace, model=fake, max_model_calls=8)
            result = await asyncio.wait_for(agent.ainvoke({"messages": [{"role": "user", "content":
                "执行开发工具验收，必须依次真实调用以下三个工具，不委派："
                "1. web_search 查询 Python Decimal 官方文档；"
                "2. fetch_url 读取 https://docs.python.org/3/library/decimal.html；"
                "3. run_pytest 运行 tests/test_smoke.py。不要修改文件。"
                "每个工具至多调用一次，出错也继续下一项；最后只总结状态，不粘贴网页或模型完整内容。"}]}, config), timeout=180)
            state = (await agent.aget_state(config)).values
        messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        successful = {m.name for m in messages if m.status == "success"}
        skills = {s["name"] for s in state.get("skills_metadata", [])}
        pytest_ok = any(m.name == "run_pytest" and m.status == "success" and "1 passed" in str(m.content) for m in messages)
        report.update(successful_tools=sorted(successful), skills=sorted(skills), model_calls=trace.model_calls,
                      passed=EXPECTED_TOOLS <= successful and EXPECTED_SKILLS <= skills and pytest_ok
                      and not any(m.name == "task" for m in messages))
    except Exception as error:
        report.update(error_type=type(error).__name__, traceback=_redact(traceback.format_exc()))
        print(report["traceback"])
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"验收{'通过' if report['passed'] else '未通过'}；报告：{folder / 'report.json'}")
    return report


def main() -> None:
    """PyCharm 直接运行使用真实模型；命令行可显式选择离线模式。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run_verification(offline=args.offline))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
