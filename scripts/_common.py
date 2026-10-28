"""试跑脚本共用工具：打印、输入 Token 预算、调用证据和有限重试。"""
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from langchain.messages import AIMessage

# 任务状态 → 图标，让清单更直观
_STATUS_ICON = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
# 快速筛查常见 float 写法；不替代 AST/人工审查，不能证明金额逻辑完全合规。
FLOAT_MONEY = re.compile(r":\s*float\b|->\s*float\b|\bfloat\s*\(")


def print_reply(result: dict[str, Any]) -> None:
    """打印返回状态的末条消息。"""
    print("\n=== DevMate 最终回复 ===")
    msgs = result.get("messages") or []
    print(msgs[-1].content if msgs else "（没有 messages）")


def print_files(result: dict[str, Any]) -> None:
    """打印保存在状态中的文件；FilesystemBackend 实际文件需另外读取。"""
    print("\n=== DevMate 写入的文件 ===")
    files = result.get("files") or {}
    if not files:
        print("（本次没有写文件）")
        return
    for name, data in files.items():
        print(f"\n--- {name} ---")
        # StateBackend 里文件值可能是字符串，也可能是带 content 字段的 dict
        print(data.get("content", data) if isinstance(data, dict) else data)


def print_todos(result: dict[str, Any]) -> None:
    """打印任务清单。键名经官方确认是 'todos'；为空时明确提示，避免误以为是 bug。"""
    print("\n=== DevMate 的任务清单 ===")
    todos = result.get("todos") or []
    if not todos:
        print("（本次没列计划：任务较简单，DevMate 没调用 write_todos —— 这是正常的）")
        return
    for i, todo in enumerate(todos, 1):
        # 兼容 todo 是 dict 或对象两种形态
        if isinstance(todo, dict):
            content, status = todo.get("content", str(todo)), todo.get("status", "pending")
        else:
            content, status = getattr(todo, "content", str(todo)), getattr(todo, "status", "pending")
        print(f"  {i}. {_STATUS_ICON.get(status, '•')} [{status}] {content}")


def print_all(result: dict[str, Any]) -> None:
    """依次打印回复、状态文件和任务清单，保留原有展示接口。"""
    print_reply(result)
    print_files(result)
    print_todos(result)


def _require_integer(value: int, name: str, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")


async def run_with_budget(
    agent: Any, task: str, *, max_input_tokens: int,
    config: dict[str, Any] | None = None,
    on_event: Callable[[str, Any], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """流式运行并返回最后完整状态和预算停止标记；默认生成独立 thread_id。

    可传 config 绑定回调及指定线程；不修改调用方配置。
    on_event 是同步观测函数，依次收到 (updates/values, 内容)，用于控制台诊断。

    依赖 CostMeterMiddleware 的累计用量字段，观察到输入 Token 严格超预算后关闭流。
    这是调用完成后的软预算检查，不能撤销已消费 Token、已写文件或外部副作用，
    也不是跨重试总预算。尚未返回的模型用量无法提前统计。
    异常和取消向调用方传播；关闭本次流，不承诺终止外部独立后台任务。
    """
    _require_integer(max_input_tokens, "max_input_tokens", 0)
    run_config = dict(config or {})
    configurable = dict(run_config.get("configurable") or {})
    configurable.setdefault("thread_id", f"test-{uuid.uuid4().hex}")
    run_config["configurable"] = configurable
    last: dict[str, Any] = {}
    async with aclosing(agent.astream(
        {"messages": [{"role": "user", "content": task}]},
        config=run_config,
        stream_mode=["updates", "values"] if on_event is not None else "values",
    )) as stream:
        async for event in stream:
            kind, payload = event if on_event is not None else ("values", event)
            if on_event is not None:
                on_event(kind, payload)
            if kind != "values":
                continue
            state = payload
            last = state
            if (state.get("total_input_tokens") or 0) > max_input_tokens:
                return last, True
    return last, False


def tool_calls(state: dict[str, Any]) -> list[dict[str, Any]]:
    """按消息顺序取出 AIMessage 中发起的工具调用，不代表调用成功。"""
    return [
        call for message in (state.get("messages") or [])
        if isinstance(message, AIMessage) for call in (message.tool_calls or [])
    ]


def called(state: dict[str, Any], name: str, contains: str | None = None) -> bool:
    """检查是否发起过指定工具调用，可按参数子串筛选。"""
    return any(
        call["name"] == name and (contains is None or contains in str(call.get("args", {})))
        for call in tool_calls(state)
    )


def written_paths(state: dict[str, Any]) -> list[str]:
    """返回 write_file/edit_file 请求中的路径，去重保序；文件成功写入需另行验证。"""
    paths: list[str] = []
    for call in tool_calls(state):
        if call["name"] in ("write_file", "edit_file"):
            args = call.get("args") or {}
            path = args.get("file_path") or args.get("path")
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def first_turn(state: dict[str, Any], names: tuple[str, ...], contains: str) -> int | None:
    """返回匹配调用的首个 AI 输出轮次（从 1 开始），同轮并行调用轮次相同。"""
    turn = 0
    for message in (state.get("messages") or []):
        if isinstance(message, AIMessage):
            turn += 1
            for call in message.tool_calls or []:
                if call["name"] in names and contains in str(call.get("args", {})):
                    return turn
    return None


def model_call_count(state: dict[str, Any]) -> int:
    """按 AIMessage 数估算输出轮次；含框架提示，不是精确 API 请求/重试计数。"""
    return sum(isinstance(message, AIMessage) for message in (state.get("messages") or []))


def final_reply(state: dict[str, Any]) -> str:
    """返回末条消息内容；中止运行时不保证它是最终模型答复。"""
    messages = state.get("messages") or []
    return str(messages[-1].content) if messages else ""


@dataclass
class Check:
    """一项检查：硬性项决定通过与否，参考项只展示结果。"""

    name: str
    passed: bool
    detail: str = ""
    hard: bool = True


def _redact(value: Any) -> Any:
    """遮盖常见凭据字段、连接串密码与 sk 密钥；不能替代对任意文本的安全审查。"""
    sensitive = {"api_key", "apikey", "password", "passwd", "secret", "token",
                 "authorization", "access_token", "refresh_token"}
    if isinstance(value, dict):
        return {key: "***" if str(key).lower() in sensitive else _redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "***", value)
        return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:)[^\s/@]+@", r"\1***@", text)
    return value


def _short(args: Any, limit: int = 70) -> str:
    safe = _redact(args)
    text = ", ".join(f"{key}={value!r}" for key, value in safe.items()) if isinstance(safe, dict) else str(safe)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def print_run(state: dict[str, Any], budget_hit: bool, max_input_tokens: int) -> None:
    """展示请求轨迹、末条消息及已观测用量，常见凭据按模式遮盖。"""
    print("\n--- 工具调用轨迹（请求，不代表成功） ---")
    calls = tool_calls(state)
    if not calls:
        print("  （无工具调用）")
    for index, call in enumerate(calls, 1):
        print(f"  {index}. {call['name']}({_short(call.get('args'))})")
    print("\n--- 末条消息（截断 500 字） ---")
    print(str(_redact(final_reply(state)))[:500])
    print("\n--- 已观测用量 ---")
    print(f"  AI 输出轮次 {model_call_count(state)}（含框架提示） | "
          f"input={state.get('total_input_tokens', 0)} output={state.get('total_output_tokens', 0)}")
    if budget_hit:
        print(f"  ⛔ 累计 input 超过预算 {max_input_tokens}，已停止消费本次运行流")


def report(checks: list[Check]) -> bool:
    """打印检查结果，仅用硬性项判断是否通过；空清单不作额外失败判定。"""
    print("\n--- 断言结果 ---")
    for check in checks:
        icon = "✅" if check.passed else ("❌" if check.hard else "⚠️")
        tag = "硬性" if check.hard else "参考"
        detail = f" —— {_redact(check.detail)}" if check.detail else ""
        print(f"  {icon} [{tag}] {check.name}{detail}")
    passed = all(check.passed for check in checks if check.hard)
    print(f"  => 本次{'通过' if passed else '未通过'}（仅硬性检查决定结果）")
    return passed


async def run_attempts(
    one_attempt: Callable[[int], Awaitable[tuple[bool, int]]], max_attempts: int,
) -> bool:
    """有限次尝试，回调返回（是否通过，本次输入 Token），首次通过即结束。

    回调负责各次隔离、产物清理和预算；本函数不清理文件、不重试异常，
    也不实施跨尝试的总 Token 上限，只汇总回调提供的已观测用量。
    """
    _require_integer(max_attempts, "max_attempts", 1)
    total_input = 0
    for attempt in range(1, max_attempts + 1):
        print(f"\n========== 第 {attempt}/{max_attempts} 次尝试 ==========")
        passed, used = await one_attempt(attempt)
        _require_integer(used, "本次 input token", 0)
        total_input += used
        if passed:
            print(f"\n🎉 测试目标达成（第 {attempt} 次尝试），累计 input token：{total_input}")
            return True
        if attempt < max_attempts:
            print("\n↻ 检查未通过，将再次调用 one_attempt；隔离与产物清理由回调负责")
    print(f"\n❌ {max_attempts} 次尝试均未达成测试目标，累计 input token：{total_input}。")
    return False
