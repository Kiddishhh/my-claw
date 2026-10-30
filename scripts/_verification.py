"""验收共享组件：离线模型和摘要回调。

放在已安装的 scripts 包中，避免独立验收入口依赖未打包的 tests 包。
只提供组件，不在导入时启动模型、联网或创建日志文件。
"""
from datetime import datetime
import json
from pathlib import Path
import time
import traceback
from typing import Any
import uuid

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from pydantic import Field

from scripts._common import _redact, _short

SKILL_NAME = "my-claw-code-quality"


class ScriptedModel(BaseChatModel):
    """按预设顺序响应的离线模型，供回归与独立验收共同使用。"""

    replies: list[AIMessage]
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "offline-scripted-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        reply = self.replies[min(len(self.seen_messages), len(self.replies) - 1)]
        self.seen_messages.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=reply)])


class VerificationTrace(AsyncCallbackHandler):
    """将实际模型调用和工具执行回调写到终端及 JSONL，保留调用 ID。"""

    def __init__(self, path: Path, memory_code: str) -> None:
        self.path = path
        self.memory_code = memory_code
        self.started = time.perf_counter()
        self.model_calls = 0
        self.memory_in_prompt = False
        self.skill_in_prompt = False
        self.usage_calls = 0
        self.tools: dict[str, dict[str, Any]] = {}
        self.model_rounds: dict[str, int] = {}

    def record(self, event: str, **fields: Any) -> None:
        """保存结构化摘要，终端仅显示关键进展和脱敏报错。"""
        item = _redact({"time": datetime.now().astimezone().isoformat(),
                        "elapsed_ms": round((time.perf_counter() - self.started) * 1000),
                        "event": event, **fields})
        line = json.dumps(item, ensure_ascii=False, default=str)
        summaries = {
            "start": lambda: f"开始验收 | 模式={item['mode']} | 最多 {item['max_model_calls']} 轮 | 超时 {item['timeout_seconds']} 秒",
            "input": lambda: f"模拟输入 | {_short(item['task'], 180)}",
            "model_start": lambda: f"模型第 {item['round']} 轮开始 | 消息数={item['message_count']}",
            "model_end": lambda: f"模型第 {item['round']} 轮结束 | input={(item.get('usage') or {}).get('input_tokens', '未知')} output={(item.get('usage') or {}).get('output_tokens', '未知')} | 工具={', '.join(item['tool_calls']) or '无'} | 回复={item['reply'] or '工具调用'}",
            "tool_start": lambda: f"调用工具 | {item['tool']} | {item['args']}",
            "tool_end": lambda: f"工具返回 | {item['tool']} | 状态={item['status']} | {item['duration_ms']} ms | {item['result']}",
            "finished": lambda: f"验收{'通过' if item['passed'] else '失败'} | 停止原因={item['stop_reason']} | 模型调用={item['model_calls']} | 记录={item['record_dir']}",
        }
        if event in summaries:
            print(f"[{item['time'][11:19]}] {summaries[event]()}", flush=True)
        elif event.endswith("error") or event == "timeout":
            print(f"[{item['time'][11:19]}] 运行异常 | {event} | {item.get('error_type', 'TimeoutError')}", flush=True)
        if item.get("traceback"):
            print(item["traceback"], flush=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(line + "\n")

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[BaseMessage]],
        *, run_id: uuid.UUID, **kwargs: Any,
    ) -> None:
        self.model_calls += 1
        self.model_rounds[str(run_id)] = self.model_calls
        system = "\n".join(str(m.content) for group in messages for m in group if m.type == "system")
        self.memory_in_prompt |= self.memory_code in system
        self.skill_in_prompt |= SKILL_NAME in system
        self.record("model_start", round=self.model_calls, run_id=str(run_id),
                    message_count=sum(len(group) for group in messages),
                    memory_in_prompt=self.memory_in_prompt, skill_list_in_prompt=self.skill_in_prompt)

    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        for group in response.generations:
            for generation in group:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                self.usage_calls += bool(usage)
                self.record("model_end", round=self.model_rounds.get(str(run_id)), run_id=str(run_id),
                            usage=usage, reply=_short(getattr(message, "content", ""), 160),
                            tool_calls=[call["name"] for call in getattr(message, "tool_calls", [])])

    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self.record("model_error", round=self.model_rounds.get(str(run_id)),
                    run_id=str(run_id), error_type=type(error).__name__,
                    traceback="".join(traceback.format_exception(error)))

    async def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: uuid.UUID,
        inputs: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        self.tools[str(run_id)] = {"name": serialized.get("name"), "args": inputs or {},
                                   "round": self.model_calls, "started": time.perf_counter()}
        self.record("tool_start", round=self.model_calls, run_id=str(run_id),
                    tool=serialized.get("name"), args=_short(inputs or input_str, 160))

    async def on_tool_end(self, output: Any, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        info = self.tools.get(str(run_id), {})
        info["ended"] = time.perf_counter()
        info["status"] = getattr(output, "status", "success")
        info["output"] = str(getattr(output, "content", output))
        self.record("tool_end", round=info.get("round"), run_id=str(run_id),
                    tool=info.get("name"), status=info["status"],
                    duration_ms=round((time.perf_counter() - info.get("started", time.perf_counter())) * 1000),
                    result=_short(info["output"], 160))

    async def on_tool_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        info = self.tools.get(str(run_id), {})
        info["ended"] = time.perf_counter()
        info["status"] = "error"
        self.record("tool_error", round=info.get("round"), run_id=str(run_id),
                    tool=info.get("name"), error_type=type(error).__name__,
                    traceback="".join(traceback.format_exception(error)))
