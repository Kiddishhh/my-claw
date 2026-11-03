"""真实 Daytona + 真实模型验收；会创建资源，最终只删除本次测试会话。

运行：uv run --extra daytona python -m tests.verify_daytona
不由 pytest 自动收集。验证实际上传/依赖、模型远程执行、会话复用、密钥边界和回收。
"""
import argparse
import asyncio
from pathlib import PurePosixPath
import json
import shlex
import uuid

from langchain_core.messages import ToolMessage
from scripts._verification import VerificationTrace
from scripts._analysis_verification import SALES_CSV, analysis_task

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from agent.main import PROJECT_ROOT, build_agent as _build_agent, build_model
from infra.agent_limits import DEFAULT_MODEL_CALLS, DEFAULT_SUBAGENT_MODEL_CALLS, DEFAULT_RECURSION_LIMIT
from infra.settings import SandboxSettings, load_langsmith_environment
from sandbox.manager import (
    session_key, _sql, create_daytona_client,
    sandbox_for_thread,
)


def build_agent(**kwargs):
    """验收脚本显式使用内存存储，不声称跨进程持久化。"""
    return _build_agent(checkpointer=InMemorySaver(), store=InMemoryStore(), **kwargs)


def main() -> None:
    """生成唯一会话并验证；失败也尝试删除，删除失败保留映射供回收。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["development", "analysis"], default="development")
    parser.add_argument("--max-subagent-model-calls", type=int, default=DEFAULT_SUBAGENT_MODEL_CALLS, help="每次子代理委派的模型调用上限")
    args = parser.parse_args()
    if args.max_subagent_model_calls <= 0:
        parser.error("--max-subagent-model-calls 必须为正整数")
    load_langsmith_environment()
    settings = SandboxSettings(syc_sandbox_seed_platform=True)
    client = create_daytona_client(settings)
    thread_id = f"verification-{uuid.uuid4().hex}"
    key = session_key("verification", thread_id)
    # 使用唯一会话 ID；项目文件来自实际仓库，同一状态库只删除本次记录。
    sandbox = None
    checks: dict[str, bool] = {}
    folder = PROJECT_ROOT / "logs/daytona-verification" / thread_id
    folder.mkdir(parents=True, exist_ok=True)
    trace = VerificationTrace(folder / "events.jsonl", "")
    try:
        with sandbox_for_thread("verification", thread_id, settings=settings, client=client) as resources:
            backend, sandbox, _, workdir = resources
            print("项目初始化成功：上传及远程依赖同步完成", flush=True)
            probe = (
                "import os; from pathlib import Path; "
                "assert not any(os.getenv(k) for k in "
                "['SYC_API_KEY','LANGSMITH_API_KEY','DAYTONA_API_KEY']); "
                f"assert not Path({workdir!r}, '.env').exists(); print('KEYS_ABSENT')"
            )
            result = backend.execute("python -c " + shlex.quote(probe))
            checks["host_keys_absent"] = result.exit_code == 0 and "KEYS_ABSENT" in result.output
            agent = build_agent(backend=backend, workdir=workdir, channel="cli", model=build_model(), max_model_calls=DEFAULT_MODEL_CALLS, max_subagent_model_calls=args.max_subagent_model_calls)
            code = (
                "from decimal import Decimal; from pathlib import Path; "
                "assert Decimal('0.1') + Decimal('0.2') == Decimal('0.3'); "
                "Path('sandbox_verification.txt').write_text('OK'); print('DAYTONA_MVP_OK')"
            )
            script_path = f"{workdir}/sandbox_verification.py"
            command = f"cd {shlex.quote(workdir)} && python -m uv run --locked --extra daytona python {shlex.quote(script_path)}"

            task = (
                        f"这是远程子代理验收。请依次使用三个 task，每个完成后再委派下一个，不跳过角色："
                        f"1. coder 仅创建 {script_path}，代码为：{code}。"
                        f"2. tester 用 execute 运行命令 {command}，提供真实退出码和 DAYTONA_MVP_OK 标记。"
                        "3. reviewer 只读检查脚本和 tester 返回的证据，通过 ReviewResult 输出结论。"
                        "委派时完整传递路径、代码及上游证据；不要运行全量测试或改动其他文件。主代理不要自己代替子代理执行。"
            )
            if args.scenario == "analysis":
                directory = f"{workdir}/data/inputs"
                made = backend.execute("mkdir -p " + shlex.quote(directory))
                if made.exit_code != 0:
                    raise RuntimeError("无法创建模拟数据目录")
                uploaded = backend.upload_files([(f"{directory}/sales_verification.csv", SALES_CSV.encode("utf-8"))])
                if len(uploaded) != 1 or uploaded[0].error:
                    raise RuntimeError("模拟数据上传失败")
                task = analysis_task(workdir)
            print(f"验收场景：{args.scenario}；模拟输入：{task[:160]}", flush=True)

            async def run() -> dict:
                return await asyncio.wait_for(agent.ainvoke(
                    {"messages": [{"role": "user", "content": task}]},
                    config={"run_name": "daytona-verification", "recursion_limit": DEFAULT_RECURSION_LIMIT,
                            "callbacks": [trace], "configurable": {"thread_id": thread_id}, "metadata": {"sandbox_id": sandbox.id}},
                ), timeout=480)

            result = asyncio.run(run())
            calls = list(trace.tools.values())
            checks["model_remote_execution"] = any(
                item["name"] == "execute" and item.get("status") == "success"
                and (args.scenario == "analysis" or "DAYTONA_MVP_OK" in item.get("output", "")) for item in calls
            )
            delegated = [item for item in calls if item["name"] == "task"]
            expected_roles = ["data_analyst", "reviewer"] if args.scenario == "analysis" else ["coder", "tester", "reviewer"]
            checks["scenario_delegation"] = (
                [item["args"].get("subagent_type") for item in delegated] == expected_roles
                and all(item.get("status") == "success" for item in delegated)
            )
            # task 的回调返回 Command；最终 messages 中才是已展开的 ToolMessage。
            task_results = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"]
            checks["structured_review"] = bool(task_results) and json.loads(task_results[-1].content).get("approved") is True
            intervals = sorted(delegated, key=lambda item: item["started"])
            checks["serial_tasks"] = len(intervals) >= 2 and all(
                left.get("ended", float("inf")) <= right["started"]
                for left, right in zip(intervals, intervals[1:])
            )
            (folder / "task-timeline.json").write_text(json.dumps([
                {"role": item["args"].get("subagent_type"),
                 "start_ms": round((item["started"] - trace.started) * 1000),
                 "end_ms": round((item.get("ended", item["started"]) - trace.started) * 1000)}
                for item in intervals
            ], ensure_ascii=False, indent=2), encoding="utf-8")
            if args.scenario == "analysis":
                # 宿主只执行独立校验，不代替子代理生成统计或报告。
                validation = (
                    "import json; from pathlib import Path; "
                    "from scripts._analysis_verification import validate_analysis_files; "
                    f"print(json.dumps(validate_analysis_files(Path({workdir!r}))))"
                )
                verify_command = f"cd {shlex.quote(workdir)} && python -m uv run --locked --extra daytona python -c {shlex.quote(validation)}"
                verified = backend.execute(verify_command)
                if verified.exit_code != 0:
                    raise RuntimeError("远程数据分析产物断言失败：" + verified.output[-1500:])
                summary = json.loads(verified.output)
                checks["analysis_results"] = summary["totals"] == {"A": "0.30", "B": "12.50"}
                # 下载前约束路径，避免沙箱提供的路径覆盖宿主源码。
                for relative in summary["artifacts"]:
                    path = PurePosixPath(relative)
                    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative or path.parts[:2] != ("outputs", "analysis"):
                        raise RuntimeError("非法产物路径")
                    downloads = backend.download_files([f"{workdir}/{relative}"])
                    if len(downloads) != 1 or downloads[0].error or downloads[0].content is None:
                        raise RuntimeError("下载分析产物失败")
                    dest = folder / "artifacts" / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(downloads[0].content)
                (folder / "analysis-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            identity = sandbox.id
        # 第二次走完整入口：不重新上传，读取上一轮写入的文件。
        with sandbox_for_thread("verification", thread_id, settings=settings, client=client) as resources:
            backend, resumed, _, workdir = resources
            if args.scenario == "analysis":
                result = backend.execute(verify_command)
                persisted = result.exit_code == 0 and json.loads(result.output) == summary
            else:
                result = backend.execute(f"cat {shlex.quote(workdir + '/sandbox_verification.txt')}")
                persisted = result.exit_code == 0 and result.output.strip() == "OK"
            checks["reuse_and_file_persistence"] = resumed.id == identity and persisted
        if not all(checks.values()):
            raise RuntimeError("真实沙箱验收未通过")
    finally:
        # 不让临时资源持续计费。真实回收删除在单元测试中覆盖，
        # 此处显式指定本次 sandbox，避免对其他空闲用户资源执行操作。
        if sandbox is not None:
            client.delete(sandbox, wait=True, timeout=120)
            _sql(PROJECT_ROOT, "DELETE FROM sessions WHERE session_key=?", (key,))
            checks["deleted"] = True
        (folder / "report.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
        print(checks, flush=True)
        print(f"验收记录与分析产物：{folder}", flush=True)


if __name__ == "__main__":
    main()
