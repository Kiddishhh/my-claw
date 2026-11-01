"""真实 Web 团队验收：dispatcher → 委派 → 构建 → 落盘 → 删除沙箱 → 恢复。

运行：uv run --extra daytona python -m scripts.verify_web_team
会调用真实模型、创建临时 Daytona；仅删除本次唯一线程的云端资源，保留磁盘证据。
不上传平台仓库。Node 原生模块、零第三方依赖，避免把网络安装耗时当作开发能力。
此脚本验证代码/构建及恢复，不替代浏览器视觉和交互验收。不由 pytest 自动执行。
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import traceback
import uuid

from langchain.messages import ToolMessage

from infra.agent_limits import DEFAULT_MODEL_CALLS, DEFAULT_SUBAGENT_MODEL_CALLS
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from agent.dispatcher import run_task
from agent.main import PROJECT_ROOT, build_model
from infra.settings import SandboxSettings
from sandbox.manager import (_lock, _session_key, _sql, create_daytona_client, sandbox_for_thread)
from sandbox.workspace import _inside
from scripts._common import Check, report, tool_calls, _redact
from scripts._verification import VerificationTrace
from subagents.schemas import ReviewResult

PIPELINE = ["coder", "tester", "reviewer"]
REQUIRED = ["package.json", "src/catalog.mjs", "build.mjs", "tests/catalog.test.mjs"]


def issue_for(workdir: str) -> str:
    """固定且充分明确的需求；使用真实工作目录，不假设 provider 的 home 路径。"""
    command = f"cd {shlex.quote(workdir)} && npm install --ignore-scripts --no-audit --no-fund && npm test && npm run build"
    return (
        f"创建一个最小商品展示网站，项目位于 {workdir}。仅使用 Node.js 原生模块，不引入第三方依赖。\n"
        "package.json 设置 type=module，scripts.test='node --test tests/catalog.test.mjs'，scripts.build='node build.mjs'。\n"
        "src/catalog.mjs 导出 filterProducts(products, category)：category='all' 返回全部商品，"
        "否则按 category 严格相等筛选，不修改输入。商品字段为 name、category。\n"
        "同文件导出 renderPage(products)：返回包含 HTML 文档、商品名称的页面字符串；"
        "商品名称中的 & < > 双引号 单引号必须 HTML 转义，页面包含基础样式和 viewport 声明。\n"
        "build.mjs 创建 dist/index.html，示例商品名固定为 Canvas Bag 和 Desk Lamp，分类分别为 bags 和 lights；构建应确定性输出。\n"
        "tester 编写 tests/catalog.test.mjs，使用 node:test 覆盖筛选、输入不变、HTML 转义，3～6 个测试。\n"
        f"tester 必须实际执行：{command}；报告退出码和结果，不用管道掩盖失败。\n"
        "主 Agent 只协调，不调用 write_file/edit_file/execute；可读取服务器指导文件。"
        "按 coder → tester → reviewer 顺序，每个完成后再委派下一个，给子代理完整路径、接口要求及上游证据。"
        "coder 写实现和构建配置，tester 写并运行测试，reviewer 只读代码和测试证据，以 ReviewResult 返回结论。"
        "每个角色只委派一次，失败即报告；不要重跑全平台测试，不要启动长期服务器。"
        "读取必要规则和技能后聚焦这五个文件，各角色及最终回复只要简短摘要。"
    )


def review_of(state: dict) -> ReviewResult | None:
    """严格关联最后一次 reviewer task 的返回，不误用其他角色或上一次结论。"""
    calls = [c for c in tool_calls(state) if c["name"] == "task" and c["args"].get("subagent_type") == "reviewer"]
    if not calls:
        return None
    identity = calls[-1]["id"]
    for message in reversed(state.get("messages", [])):
        if isinstance(message, ToolMessage) and message.tool_call_id == identity:
            if message.status == "error":
                return None
            try:
                return ReviewResult.model_validate_json(str(message.content))
            except ValueError:
                return None
    return None


def check_agent_run(state: dict) -> list[Check]:
    """检查主图真实消息；不以回复中的“通过”或请求轨迹代替工具返回。"""
    calls = tool_calls(state)
    tasks = [c for c in calls if c["name"] == "task"]
    roles = [c["args"].get("subagent_type") for c in tasks]
    results = {m.tool_call_id: m for m in state.get("messages", []) if isinstance(m, ToolMessage)}
    review = review_of(state)
    return [
        Check("按 coder → tester → reviewer 委派且全部返回成功", roles == PIPELINE and all(
            c["id"] in results and results[c["id"]].status != "error" for c in tasks), str(roles)),
        Check("主 Agent 只协调，不亲自写代码或执行", not any(c["name"] in {"write_file", "edit_file", "execute"} for c in calls)),
        Check("reviewer 返回有效且 approved 的结构化结论", bool(review and review.approved),
              review.summary if review else "无有效 reviewer 结果"),
    ]


def verify_saved_files(version: Path, required: list[str] = REQUIRED) -> bool:
    """独立核验磁盘清单和字节哈希；不在服务器执行生成的代码。"""
    try:
        manifest = json.loads((version / "manifest.json").read_text(encoding="utf-8"))
        indexed = {x["path"]: x for x in manifest}
        for name in required:
            item = indexed[name]
            content = _inside(version / "project", name).read_bytes()
            if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def independent_command(workdir: str) -> str:
    """独立断言由宿主提供，但只在沙箱执行，检查行为而不是搜索 passed 字符串。"""
    module = str(PurePosixPath(workdir) / "src/catalog.mjs")
    html = str(PurePosixPath(workdir) / "dist/index.html")
    code = f"""import assert from 'node:assert/strict';
import {{readFileSync}} from 'node:fs';
import {{pathToFileURL}} from 'node:url';
const {{filterProducts,renderPage}}=await import(pathToFileURL({json.dumps(module)}));
const products=[{{name:'Canvas Bag',category:'bags'}},{{name:'Desk Lamp',category:'lights'}}];
const original=JSON.stringify(products);
assert.deepEqual(filterProducts(products,'all'),products);
assert.deepEqual(filterProducts(products,'bags'),[products[0]]);
assert.deepEqual(filterProducts(products,'missing'),[]);
assert.equal(JSON.stringify(products),original);
const dangerous='<script>&"'+String.fromCharCode(39);
const page=renderPage([{{name:dangerous,category:'bags'}}]);
assert.equal(typeof page,'string');
assert.ok(!page.includes(dangerous) && page.includes('&lt;script&gt;') && page.includes('&amp;'));
assert.ok(page.includes('&quot;') || page.includes('&#34;'));
assert.ok(page.includes('&#39;') || page.includes('&#x27;') || page.includes('&apos;'));
const built=readFileSync({json.dumps(html)},'utf8');
assert.ok(/<!doctype html/i.test(built) && built.includes('Canvas Bag') && built.includes('Desk Lamp'));
assert.ok(built.includes('viewport'));
console.log('WEB_TEAM_ASSERTIONS_OK');"""
    return f"cd {shlex.quote(workdir)} && npm test && npm run build && node --input-type=module -e {shlex.quote(code)}"


async def verify(timeout: int, max_model_calls: int, max_subagent_calls: int) -> int:
    """只运行一次独立会话；失败也保存摘要并清理本次创建的沙箱。"""
    thread_id = f"web-team-{uuid.uuid4().hex}"
    user_id = "verification"
    key = _session_key(user_id, thread_id)
    folder = PROJECT_ROOT / "logs/web-team-verification" / thread_id
    folder.mkdir(parents=True)
    checks: list[Check] = []
    settings = SandboxSettings(syc_sandbox_seed_platform=False, syc_sandbox_task_timeout=timeout)
    client = create_daytona_client(settings)
    trace = VerificationTrace(folder / "events.jsonl", "通用开发 Agent 工作约定")
    identities: set[str] = set()
    error_text = None
    try:
        # 仅为取得实际 workdir；随后使用正式 dispatcher，而不是绕开文件保存入口。
        with sandbox_for_thread(user_id, thread_id, folder, settings=settings, client=client) as resources:
            backend, sandbox, _, workdir = resources
            identities.add(sandbox.id)
            first_id = sandbox.id
        task = issue_for(workdir)
        trace.record("start", mode="真实模型 + Daytona Web 团队", max_model_calls=max_model_calls, timeout_seconds=timeout)
        trace.record("input", task=task)
        result = await run_task(thread_id, task, user_id=user_id, project_root=folder,
            checkpointer=InMemorySaver(), store=InMemoryStore(),
            settings=settings, client=client, model=build_model(), callbacks=[trace], include_state=True,
            max_model_calls=max_model_calls, max_subagent_model_calls=max_subagent_calls)
        checks.extend(check_agent_run(result["agent_state"]))
        checks.append(Check("模型真实调用，规则与开发技能进入上下文",
                            trace.model_calls > 0 and trace.memory_in_prompt and trace.skill_in_prompt))
        checks.append(Check("执行期间复用同一线程沙箱", result["sandbox_id"] == first_id))
        calls = list(trace.tools.values())
        intervals = sorted((c for c in calls if c["name"] == "task"), key=lambda c: c["started"])
        checks.append(Check("子代理委派完整执行且无重叠", len(intervals) == 3 and all(
            c.get("status") == "success" and "ended" in c for c in intervals) and all(
            a["ended"] <= b["started"] for a, b in zip(intervals, intervals[1:]))))
        checks.append(Check("子代理实际调用 execute 和读取技能", any(
            c["name"] == "execute" and c.get("status") == "success" for c in calls) and any(
            c["name"] == "read_file" and "SKILL.md" in str(c["args"]) and c.get("status") == "success" for c in calls)))
        saved = result["workspace_path"]
        if not result["workspace_saved"] or not saved:
            raise RuntimeError("dispatcher 未成功保存工作区，停止恢复验收")
        version = _inside(folder, saved)
        checks.append(Check("源码、测试和配置已落盘且哈希正确", verify_saved_files(version), str(version)))
        checks.append(Check("构建产物不进入源码备份", not (version / "project/dist").exists()))
        # 使用独立命令复测，不相信模型生成的“passed”文本；不经过 tail/tee。
        with sandbox_for_thread(user_id, thread_id, folder, settings=settings, client=client) as resources:
            backend, sandbox, _, workdir = resources
            verified = backend.execute(independent_command(workdir))
            checks.append(Check("远程 npm test、构建及独立行为断言通过",
                                verified.exit_code == 0 and "WEB_TEAM_ASSERTIONS_OK" in verified.output,
                                f"exit_code={verified.exit_code}"))
            if verified.exit_code != 0:
                print(str(_redact(verified.output))[-3000:], flush=True)
            # 真实删除，用同一 thread_id 的磁盘备份重建；不触碰其他会话。
            client.delete(sandbox, wait=True, timeout=120)
            identities.discard(sandbox.id)
        with sandbox_for_thread(user_id, thread_id, folder, settings=settings, client=client) as resources:
            backend, restored, _, workdir = resources
            identities.add(restored.id)
            checks.append(Check("沙箱已更换，同一线程从磁盘恢复", restored.id != first_id))
            # 从远程下载逐项比对源文件，而非仅验证 sandbox_id 变化。
            identical = True
            for name in REQUIRED:
                downloads = backend.download_files([f"{workdir}/{name}"])
                identical &= len(downloads) == 1 and not downloads[0].error and downloads[0].content == (version / "project" / name).read_bytes()
            checks.append(Check("恢复后的源码、测试和配置与磁盘一致", identical))
            verified = backend.execute(independent_command(workdir))
            checks.append(Check("恢复后仍可运行测试和构建", verified.exit_code == 0 and "WEB_TEAM_ASSERTIONS_OK" in verified.output,
                                f"exit_code={verified.exit_code}"))
            if verified.exit_code != 0:
                print(str(_redact(verified.output))[-3000:], flush=True)
        print("最终回复摘要：", str(_redact(result["reply"]))[:500], flush=True)
    except Exception as error:
        error_text = str(_redact("".join(traceback.format_exception(error))))
        print(error_text, flush=True)
        checks.append(Check("端到端执行无异常", False, type(error).__name__))
    finally:
        # 清理失败不可覆盖原始异常；报告始终写盘。目录独立，不删除磁盘验收证据。
        try:
            with _lock(folder, key):
                rows = _sql(folder, "SELECT * FROM sessions WHERE session_key=?", (key,))
                if rows and rows[0]["sandbox_id"]:
                    identities.add(rows[0]["sandbox_id"])
                from daytona import DaytonaNotFoundError, ListSandboxesQuery
                # create 超时也可能已产生云端资源，用本次唯一线程标签兜底查找。
                for sandbox in client.list(ListSandboxesQuery(labels={"my_claw_session": key}), request_timeout=30):
                    identities.add(sandbox.id)
                failures = []
                for identity in list(identities):
                    try:
                        client.delete(client.get(identity), wait=True, timeout=120)
                    except DaytonaNotFoundError:
                        pass
                    except Exception as error:
                        failures.append(type(error).__name__)
                if failures:
                    raise RuntimeError("部分临时沙箱清理失败：" + ", ".join(failures))
                if rows:
                    # 保留备份路径；无备份的失败会话也不伪造成功恢复状态。
                    _sql(folder, "UPDATE sessions SET sandbox_id=NULL,status='archived' WHERE session_key=?", (key,))
            checks.append(Check("本次临时沙箱已删除", True))
        except Exception as error:
            checks.append(Check("本次临时沙箱已删除", False, type(error).__name__))
            print(str(_redact("".join(traceback.format_exception(error)))), flush=True)
        passed = report(checks)
        (folder / "report.json").write_text(json.dumps(_redact({
            "thread_id": thread_id, "passed": passed, "checks": [asdict(c) for c in checks],
            "model_calls": trace.model_calls, "error": error_text,
        }), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"验收报告、事件及项目备份：{folder}", flush=True)
    return 0 if passed else 1


def main() -> int:
    """直接运行默认真实验收，--help 不读取凭据、不创建资源。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=480, help="Agent 运行超时，不含环境准备与文件传输")
    parser.add_argument("--max-model-calls", type=int, default=DEFAULT_MODEL_CALLS, help="主代理模型调用上限（默认 30）")
    parser.add_argument("--max-subagent-calls", type=int, default=DEFAULT_SUBAGENT_MODEL_CALLS, help="每次子代理委派的模型调用上限（默认 40）")
    args = parser.parse_args()
    if min(args.timeout, args.max_model_calls, args.max_subagent_calls) <= 0:
        parser.error("超时和调用上限必须为正整数")
    return asyncio.run(verify(args.timeout, args.max_model_calls, args.max_subagent_calls))


if __name__ == "__main__":
    raise SystemExit(main())
