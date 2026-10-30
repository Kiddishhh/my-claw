"""测试入口：复用 scripts._common 验证 Skills / Memory / FilesystemBackend。

直接运行默认调用真实模型；加 --offline 使用模拟模型，不产生 API 费用。
"""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import traceback
from typing import Any
import uuid

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from scripts._verification import SKILL_NAME, VerificationTrace
from scripts._common import (
    Check, _redact, _short, called, first_turn, print_run, report,
    run_with_budget, written_paths,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = f"/.agents/skills/{SKILL_NAME}/SKILL.md"
OUTPUT_PATH = "/verification.json"


class OfflineProbeModel(BaseChatModel):
    """可重复的离线模型替身；仍通过真实 Agent 图执行文件工具。"""

    calls: int = 0
    @property
    def _llm_type(self) -> str:
        return "offline-wiring-probe"

    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:
        """接受框架的工具注册；由本地固定协议产生调用。"""
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        text = "\n".join(str(message.content) for message in messages)
        calls: list[dict[str, Any]] = []
        if self.calls == 1:
            calls = [{"name": "read_file", "args": {"file_path": SKILL_PATH}}]
        elif self.calls == 2:
            memory = re.search(r"memory_verification_code=([a-f0-9]+)", text)
            skill = re.search(r"skill_verification_code=([a-f0-9]+)", text)
            content = json.dumps({
                "memory_code": memory.group(1) if memory else "MISSING",
                "skill_code": skill.group(1) if skill else "MISSING",
            })
            calls = [{"name": "write_file", "args": {"file_path": OUTPUT_PATH, "content": content}}]
        elif self.calls == 3:
            calls = [{"name": "read_file", "args": {"file_path": OUTPUT_PATH}}]
        for call in calls:
            call.update(id=f"probe-{self.calls}", type="tool_call")
        message = AIMessage(
            content="" if calls else "离线通路检查结束，结论以程序断言和磁盘证据为准。",
            tool_calls=calls,
            usage_metadata={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


async def run_verification(
    *, log_root: Path | None = None, live: bool = False,
    max_model_calls: int = 6, max_input_tokens: int = 50000,
    timeout: float = 120, model: BaseChatModel | None = None,
) -> dict[str, Any]:
    """运行隔离验证并保存报告；默认离线，返回 passed 和具体停止原因。"""
    if type(max_model_calls) is not int or max_model_calls <= 0:
        raise ValueError("max_model_calls 必须是正整数")
    if type(max_input_tokens) is not int or max_input_tokens < 0:
        raise ValueError("max_input_tokens 必须是非负整数")
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")
    source_skill = PROJECT_ROOT / SKILL_PATH.lstrip("/")
    source_memory = PROJECT_ROOT / "AGENTS.md"
    if not source_skill.is_file() or not source_memory.is_file():
        raise FileNotFoundError("缺少项目 AGENTS.md 或 my-claw-code-quality/SKILL.md")

    run_id = uuid.uuid4().hex
    folder = (log_root or PROJECT_ROOT / "logs" / "agent-verification") / run_id
    workspace = folder / "workspace"
    skill_target = workspace / SKILL_PATH.lstrip("/")
    skill_target.parent.mkdir(parents=True)
    memory_code, skill_code = uuid.uuid4().hex, uuid.uuid4().hex
    (workspace / "AGENTS.md").write_text(
        source_memory.read_text(encoding="utf-8") + f"\n验证标记：memory_verification_code={memory_code}\n",
        encoding="utf-8",
    )
    skill_target.write_text(
        source_skill.read_text(encoding="utf-8") + f"\n验证标记：skill_verification_code={skill_code}\n",
        encoding="utf-8",
    )
    trace = VerificationTrace(folder / "events.jsonl", memory_code)
    mode = "live" if live else "offline"
    trace.record("start", mode=mode, workspace=str(workspace), max_model_calls=max_model_calls,
                 max_input_tokens=max_input_tokens, timeout_seconds=timeout,
                 note="offline Token 是测试数据；实际轮次不包含 SDK 内部 HTTP 重试")
    task = (
        f"这是隔离通路验证。先用 read_file 读取 {SKILL_PATH}，然后从已注入的 AGENTS.md 中提取"
        " memory_verification_code，从技能正文提取 skill_verification_code。"
        f"用 write_file 创建 {OUTPUT_PATH}，写入 JSON 对象，键为 memory_code 和 skill_code，值使用实际标记。"
        f"再用 read_file 读回 {OUTPUT_PATH} 并简述结果。不得猜测标记、修改规范或读取其它文件。"
    )
    trace.record("input", task=task)
    config = {"configurable": {"thread_id": f"verification-{run_id}"}, "callbacks": [trace], "run_name": f"my-claw-verification-{run_id}"}
    state: dict[str, Any] = {}
    budget_hit = call_limit_hit = False
    reason, error_type = "completed", None
    agent = None

    def observe_event(kind: str, payload: Any) -> None:
        nonlocal state, call_limit_hit
        if kind == "updates":
            for node, update in payload.items():
                trace.record("graph_update", node=node, fields=list(update) if isinstance(update, dict) else [])
                if "ModelCallLimitMiddleware" in node and isinstance(update, dict) and update.get("jump_to") == "end":
                    call_limit_hit = True
        elif kind == "values":
            state = payload
            trace.record("state", input_tokens=state.get("total_input_tokens"),
                         output_tokens=state.get("total_output_tokens"),
                         messages=len(state.get("messages", [])))

    try:
        from agent.main import build_local_agent as build_agent

        agent = build_agent(user_id="verification", channel=mode, max_model_calls=max_model_calls,
                            workspace_root=workspace, model=model if model is not None else (
                                None if live else OfflineProbeModel()))
        # 真实、离线及注入模型均遵循统一的 LangSmith 环境配置。
        async with asyncio.timeout(timeout):
            state, budget_hit = await run_with_budget(
                agent, task, max_input_tokens=max_input_tokens,
                config=config, on_event=observe_event,
            )
        if budget_hit:
            reason = "input_budget"
        elif call_limit_hit:
            reason = "model_call_limit"
    except TimeoutError:
        reason, error_type = "timeout", "TimeoutError"
        trace.record("timeout", seconds=timeout, traceback=traceback.format_exc())
    except Exception as error:
        reason, error_type = "error", type(error).__name__
        trace.record("error", error_type=error_type, http_status=getattr(error, "status_code", None),
                     traceback=traceback.format_exc())

    # checkpoint 中包含未公开到结果接口的技能元数据等字段。
    try:
        checkpoint = (await agent.aget_state(config)).values if agent is not None else {}
    except Exception as error:
        checkpoint = {}
        reason, error_type = "error", type(error).__name__
        trace.record("checkpoint_error", error_type=error_type, traceback=traceback.format_exc())
    loaded_memory = checkpoint.get("memory_contents", {})
    loaded_skills = checkpoint.get("skills_metadata", []) or []
    artifact = workspace / OUTPUT_PATH.lstrip("/")
    try:
        contents = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        contents = {}
    successes = [item for item in trace.tools.values() if item.get("status") == "success"]
    skill_reads = [item for item in successes if item["name"] == "read_file" and
                   item["args"].get("file_path") == SKILL_PATH and skill_code in item.get("output", "")]
    readbacks = [item for item in successes if item["name"] == "read_file" and
                 item["args"].get("file_path") == OUTPUT_PATH and memory_code in item.get("output", "") and
                 skill_code in item.get("output", "")]
    checks = [
        Check("Memory 已加载并进入实际模型提示词", memory_code in loaded_memory.get("/AGENTS.md", "") and trace.memory_in_prompt),
        Check("Skills 已发现并进入模型提示词", any(item["name"] == SKILL_NAME for item in loaded_skills) and trace.skill_in_prompt),
        Check("Skill 全文成功读取", bool(skill_reads)),
        Check("工具请求包含技能读取与文件写入", called(state, "read_file", SKILL_PATH) and OUTPUT_PATH in written_paths(state)),
        Check("磁盘文件验证标记匹配", isinstance(contents, dict) and contents.get("memory_code") == memory_code and contents.get("skill_code") == skill_code),
        Check("文件成功回读", bool(readbacks)),
        Check("运行正常结束且未触发熔断", reason == "completed", reason),
        Check("每轮模型响应都有用量信息", trace.usage_calls == trace.model_calls and trace.model_calls > 0),
    ]
    print_run(state, budget_hit, max_input_tokens)
    print(f"Skill 首次读取轮次：{first_turn(state, ('read_file',), SKILL_PATH)}；"
          f"文件首次写入轮次：{first_turn(state, ('write_file',), OUTPUT_PATH)}；"
          f"请求写入路径：{written_paths(state)}")
    print(f"实际框架模型调用轮次：{trace.model_calls}；Token 熔断：{budget_hit}；轮次熔断：{call_limit_hit}；停止原因：{reason}")
    passed = report(checks)
    result = {"passed": passed, "mode": mode, "record_dir": str(folder.resolve()),
              "model_calls": trace.model_calls, "budget_hit": budget_hit, "call_limit_hit": call_limit_hit,
              "stop_reason": reason, "error_type": error_type,
              "input_tokens": state.get("total_input_tokens"), "output_tokens": state.get("total_output_tokens"),
              "checks": [asdict(check) for check in checks]}
    (folder / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    trace.record("finished", **result)
    return result


def main() -> int:
    """命令行入口，验收通过返回 0，否则返回 1；参数错误由 argparse 报告。"""
    # Windows 下重定向输出时默认可能是 GBK；确保中文和 emoji 可完整保存。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="请求 .env 配置的真实模型（默认），会产生费用")
    modes.add_argument("--offline", action="store_true", help="使用离线模拟模型，不请求真实接口")
    parser.add_argument("--max-model-calls", type=int, default=6)
    parser.add_argument("--max-input-tokens", type=int, default=50000)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--log-dir", type=Path, default=None)
    args = parser.parse_args()
    return 0 if asyncio.run(run_verification(
        live=not args.offline, max_model_calls=args.max_model_calls, max_input_tokens=args.max_input_tokens,
        timeout=args.timeout, log_root=args.log_dir,
    ))["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
