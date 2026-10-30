"""Offline behavior tests for the shared script helpers."""

import asyncio

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage

from scripts import _common as common


class StreamingAgent:
    def __init__(self, states: list[dict]) -> None:
        self.states = states
        self.configs: list[dict] = []
        self.yielded = 0
        self.closed = 0

    async def astream(self, payload: dict, *, config: dict, stream_mode: str):
        assert stream_mode == "values"
        assert payload["messages"][0]["role"] == "user"
        self.configs.append(config)
        try:
            for state in self.states:
                self.yielded += 1
                yield state
        finally:
            self.closed += 1


def test_budget_stops_at_observed_excess_and_closes_stream() -> None:
    agent = StreamingAgent([{}, {"total_input_tokens": 11}, {"total_input_tokens": 20}])
    state, hit = asyncio.run(common.run_with_budget(agent, "task", max_input_tokens=10))
    assert hit and state["total_input_tokens"] == 11
    assert agent.yielded == 2
    assert agent.closed == 1


def test_exact_budget_is_allowed_and_each_run_gets_new_thread() -> None:
    agent = StreamingAgent([{"total_input_tokens": 10}])
    for _ in range(2):
        state, hit = asyncio.run(common.run_with_budget(agent, "task", max_input_tokens=10))
        assert not hit and state["total_input_tokens"] == 10
    assert agent.closed == 2
    assert agent.configs[0]["configurable"]["thread_id"] != agent.configs[1]["configurable"]["thread_id"]


def test_empty_stream_returns_empty_state() -> None:
    assert asyncio.run(common.run_with_budget(StreamingAgent([]), "task", max_input_tokens=0)) == ({}, False)


def test_budget_observer_receives_updates_and_keeps_callback_config() -> None:
    """诊断调用方可观测节点和状态，并使用同一线程读取 checkpoint。"""
    config = {"configurable": {"thread_id": "probe-thread"}, "callbacks": [object()]}
    events = []

    class ObservedAgent:
        closed = False

        async def astream(self, payload, *, config: dict, stream_mode):
            assert config["configurable"]["thread_id"] == "probe-thread"
            assert config["callbacks"] == original_callbacks
            assert stream_mode == ["updates", "values"]
            try:
                yield "updates", {"model": {"messages": []}}
                yield "values", {"total_input_tokens": 11}
                pytest.fail("超预算后应关闭流")
            finally:
                self.closed = True

    original_callbacks = config["callbacks"][:]
    agent = ObservedAgent()
    state, hit = asyncio.run(common.run_with_budget(
        agent, "task", max_input_tokens=10, config=config,
        on_event=lambda kind, payload: events.append((kind, payload)),
    ))
    assert hit and state["total_input_tokens"] == 11
    assert [kind for kind, _ in events] == ["updates", "values"]
    assert agent.closed
    assert config == {"configurable": {"thread_id": "probe-thread"}, "callbacks": original_callbacks}


@pytest.mark.parametrize("budget", [-1, True, 1.5])
def test_invalid_budget_never_starts_agent(budget: object) -> None:
    agent = StreamingAgent([])
    with pytest.raises(ValueError):
        asyncio.run(common.run_with_budget(agent, "task", max_input_tokens=budget))
    assert not agent.configs


def test_cancellation_propagates_and_closes_stream() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        closed = asyncio.Event()

        class WaitingAgent:
            async def astream(self, *args, **kwargs):
                try:
                    started.set()
                    await asyncio.Event().wait()
                    yield {}
                finally:
                    closed.set()

        task = asyncio.create_task(common.run_with_budget(WaitingAgent(), "task", max_input_tokens=10))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    asyncio.run(scenario())


def test_stream_errors_are_not_silently_retried() -> None:
    class BrokenAgent:
        async def astream(self, *args, **kwargs):
            yield {}
            raise RuntimeError("offline failure")

    with pytest.raises(RuntimeError, match="offline failure"):
        asyncio.run(common.run_with_budget(BrokenAgent(), "task", max_input_tokens=10))


def evidence_state() -> dict:
    return {"messages": [
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {"file_path": "/AGENTS.md"}, "id": "read"},
            {"name": "write_file", "args": {"file_path": "/pricing.py"}, "id": "write"},
        ]),
        ToolMessage(content="failed", tool_call_id="write", status="error"),
        AIMessage(content="done", tool_calls=[
            {"name": "edit_file", "args": {"file_path": "/pricing.py"}, "id": "edit"},
            {"name": "write_file", "args": {"path": "/tests/test_pricing.py"}, "id": "test"},
        ]),
    ]}


def test_tool_evidence_preserves_order_and_parallel_turns() -> None:
    state = evidence_state()
    assert len(common.tool_calls(state)) == 4
    assert common.called(state, "read_file", "AGENTS.md")
    assert not common.called(state, "read_file", "missing")
    assert common.written_paths(state) == ["/pricing.py", "/tests/test_pricing.py"]
    assert common.first_turn(state, ("read_file",), "AGENTS.md") == 1
    assert common.first_turn(state, ("write_file",), "pricing.py") == 1
    assert common.first_turn(state, ("write_file",), "test_pricing.py") == 2
    assert common.first_turn(state, ("missing",), "") is None
    assert common.model_call_count(state) == 2
    assert common.final_reply(state) == "done"


def test_empty_evidence_defaults() -> None:
    assert common.tool_calls({}) == []
    assert common.written_paths({}) == []
    assert not common.called({}, "write_file")
    assert common.model_call_count({}) == 0
    assert common.final_reply({}) == ""


def test_float_money_pattern_is_a_heuristic() -> None:
    for code in ["amount: float", "def f() -> float:", "float(value)"]:
        assert common.FLOAT_MONEY.search(code)
    assert not common.FLOAT_MONEY.search('Decimal("1.23")')


def test_soft_check_does_not_fail_report(capsys) -> None:
    assert common.report([common.Check("required", True), common.Check("optional", False, hard=False)])
    assert not common.report([common.Check("required", False, "missing file")])
    assert "missing file" in capsys.readouterr().out


def test_attempts_stop_on_first_success(capsys) -> None:
    attempts = []

    async def once(number: int) -> tuple[bool, int]:
        attempts.append(number)
        return number == 2, 7

    assert asyncio.run(common.run_attempts(once, 3))
    assert attempts == [1, 2]
    assert "14" in capsys.readouterr().out


def test_attempts_exhaustion_and_exceptions() -> None:
    attempts = []

    async def failed(number: int) -> tuple[bool, int]:
        attempts.append(number)
        return False, 1

    assert not asyncio.run(common.run_attempts(failed, 2))
    assert attempts == [1, 2]

    async def broken(number: int) -> tuple[bool, int]:
        raise RuntimeError("do not retry auth/config errors")

    with pytest.raises(RuntimeError):
        asyncio.run(common.run_attempts(broken, 3))


@pytest.mark.parametrize("attempts", [0, -1, True, 1.5])
def test_invalid_attempt_count(attempts: object) -> None:
    async def once(number: int) -> tuple[bool, int]:
        pytest.fail("callback should not run")

    with pytest.raises(ValueError):
        asyncio.run(common.run_attempts(once, attempts))


def test_print_run_masks_sensitive_arguments(capsys) -> None:
    state = {"messages": [AIMessage(content="done", tool_calls=[{
        "name": "connect", "args": {"password": "private-value"}, "id": "connect",
    }])]}
    common.print_run(state, budget_hit=True, max_input_tokens=10)
    output = capsys.readouterr().out
    assert "connect" in output and "10" in output
    assert "private-value" not in output


def test_trace_masks_nested_credentials_without_changing_evidence(capsys) -> None:
    args = {"config": {"api_key": "private-key"},
            "url": "postgresql://user:private-password@localhost/db"}
    state = {"messages": [AIMessage(content="done", tool_calls=[{
        "name": "connect", "args": args, "id": "connect",
    }])]}
    common.print_run(state, False, 10)
    output = capsys.readouterr().out
    assert "private-key" not in output and "private-password" not in output
    assert common.tool_calls(state)[0]["args"] == args


@pytest.mark.parametrize("used", [-1, True])
def test_retry_usage_cannot_reduce_total(used: object) -> None:
    async def once(number: int) -> tuple[bool, int]:
        return True, used

    with pytest.raises(ValueError):
        asyncio.run(common.run_attempts(once, 1))


def test_verification_trace_prints_summary_without_prompt(tmp_path, capsys) -> None:
    """验收回调输出摘要，保留工具原始结果用于断言且不打印提示词。"""
    import json
    import uuid
    from langchain_core.outputs import ChatGeneration, LLMResult
    from scripts._verification import VerificationTrace

    trace = VerificationTrace(tmp_path / "events.jsonl", "memory-marker")
    run_id = uuid.uuid4()
    async def scenario():
        await trace.on_chat_model_start({}, [[HumanMessage(content="PRIVATE_PROMPT")]], run_id=run_id)
        await trace.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
            content="x" * 500 + "REPLY_TAIL",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ))]]), run_id=run_id)
        await trace.on_tool_start({"name": "read_file"}, "", inputs={"file_path": "/demo"}, run_id=run_id)
        await trace.on_tool_end(ToolMessage(content="y" * 500 + "TOOL_TAIL", tool_call_id="test"), run_id=run_id)
    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert "模型第 1 轮" in output and "input=10 output=5" in output
    assert "read_file" in output and "状态=success" in output
    assert all(text not in output for text in ["PRIVATE_PROMPT", "REPLY_TAIL", "TOOL_TAIL"])
    assert trace.tools[str(run_id)]["output"].endswith("TOOL_TAIL")
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert "messages" not in events[0]
