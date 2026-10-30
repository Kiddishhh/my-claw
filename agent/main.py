"""
agent/main.py —— 默认远程 Agent 构建；本地后端仅通过显式入口使用。

远程主子代理共享当前会话的 Daytona；显式本地验收直接访问项目目录，不提供 OS 隔离。
"""
from pathlib import Path, PurePosixPath
import shlex

from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import SandboxBackendProtocol
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from infra.settings import get_settings, load_langsmith_environment
from infra.logging import get_logger

from middleware.audit import ToolAuditMiddleware
from middleware.cost import CostMeterMiddleware
from middleware.context import RequestContextMiddleware
from subagents.profiles import build_subagents, build_remote_subagents
from middleware.single_agent import SingleAgentMiddleware
from middleware.task_serialization import TaskSerializationMiddleware
from tools.test_tools import create_test_tools
from tools.search_tools import web_search, fetch_url


logger = get_logger()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 虚拟路径相对于 FilesystemBackend 根目录，不是 Windows 系统盘路径。
SKILLS_SOURCES = ["/.agents/skills/"]
MEMORY_SOURCES = ["/AGENTS.md"]

# DevMate 的人设：只写"领域要求"，
# "如何用 write_todos / 文件工具"这类通用本领由 DeepAgents 内置提示词负责。
DEVMATE_SYSTEM_PROMPT = """你是 DevMate，一个严谨的研发助手，服务于一个 Python / FastAPI 订单微服务团队。

工作准则：
- 复杂任务先列简短计划，依据实际可用工具完成任务。
- 默认单代理开发模式：使用文件工具读写代码，web_search/fetch_url 查资料，run_pytest 验证。
- 未提供 task 或表格工具时，不委派任务、不声称已执行表格分析。
- 写代码遵循已加载的 AGENTS.md；相关任务先阅读已发现的项目 skill。
- 不臆测：信息不足时先用 read_file / grep 读相关文件再动手。
- 每次改动后，用一两句话说明"改了什么、为什么这么改"。
"""


def build_model() -> BaseChatModel:
    """按配置构建模型。"""
    s = get_settings()
    return init_chat_model(
        model=s.model_name,
        model_provider=s.model_provider,
        api_key=s.api_key.get_secret_value(),   # SecretStr 在真正用时才解开
        base_url=s.base_url,
        temperature=0,
        max_retries=s.max_retries,
        timeout=s.timeout,
    )



def build_local_agent(
    user_id: str = "anonymous",
    channel: str = "cli",
    max_model_calls: int | None = None,
    *,
    workspace_root: Path | None = None,
    model: BaseChatModel | None = None,
    subagent_models: dict[str, BaseChatModel] | None = None,
    max_subagent_model_calls: int = 12,
    enable_subagents: bool = False,
) -> CompiledStateGraph:
    """构建本地文件 Agent，加载项目技能和规则。

    max_model_calls 仅限制主代理；max_subagent_model_calls 限制每次子代理委派，非全局预算。
    subagent_models 可按角色覆盖模型；默认所有角色使用同一配置模型。
    默认只挂开发工具和开发技能；enable_subagents=True 才显式启用保留的子代理配置。
    MemorySaver 仅在本 Agent 实例的进程内保存状态。调用时须传 thread_id，
    同一实例中复用 thread_id 可延续会话，重启进程不会保留 checkpoint。
    workspace_root 与 model 可用于隔离工作目录及离线测试，省略时使用项目目录和配置模型。
    """
    if max_model_calls is not None and (
        type(max_model_calls) is not int or max_model_calls <= 0
    ):
        raise ValueError("max_model_calls 必须是正整数或 None")

    # 注入测试模型时也加载追踪环境，不依赖 build_model 的副作用。
    load_langsmith_environment()
    backend = FilesystemBackend(
        root_dir=workspace_root if workspace_root is not None else PROJECT_ROOT,
        virtual_mode=True,
    )
    middleware: list[AgentMiddleware] = [
        RequestContextMiddleware(user_id=user_id, channel=channel),
        ToolAuditMiddleware(),
        CostMeterMiddleware(),
    ]
    if max_model_calls is not None:
        middleware.append(
            ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end")
        )
    if enable_subagents:
        middleware.insert(1, TaskSerializationMiddleware())
    else:
        middleware.append(SingleAgentMiddleware())

    selected_model = model if model is not None else build_model()
    agent = create_deep_agent(
        model=selected_model,
        # 本地入口直接组合开发工具；pytest 必须绑定本次工作区，不能复用全局实例。
        tools=[web_search, fetch_url, *create_test_tools(
            workspace_root if workspace_root is not None else PROJECT_ROOT
        )],
        subagents=build_subagents(workspace_root if workspace_root is not None else PROJECT_ROOT,
                                 models=subagent_models, default_model=selected_model, max_model_calls=max_subagent_model_calls,
                                 user_id=user_id) if enable_subagents else [],
        system_prompt=DEVMATE_SYSTEM_PROMPT,
        backend=backend,
        skills=SKILLS_SOURCES,
        memory=MEMORY_SOURCES,
        middleware=middleware,
        checkpointer=MemorySaver(),
    )
    logger.info("DevMate 构建完成：本地文件系统 / skills / memory / 中间件")
    return agent


def build_agent(
    *, backend: SandboxBackendProtocol, workdir: str,
    user_id: str = "anonymous", model: BaseChatModel | None = None,
    max_model_calls: int = 12,
    subagent_models: dict[str, BaseChatModel] | None = None,
    max_subagent_model_calls: int = 12,
) -> CompiledStateGraph:
    """按官方 backend= 接口绑定远程沙箱，不创建资源、不隐式回退本机。

    调用方负责准备沙箱并在整个 ainvoke 期间持有会话锁。
    workdir 是远程 Linux 绝对路径，skills/memory 必须已预置到这个目录。
    thread_id 与沙箱的关联由 sandbox.manager 管理，而不是由图自动推断。
    MemorySaver 仅用于同一图实例内会话，不等于持久化聊天历史。
    subagent_models 按角色覆盖模型；每次委派受 max_subagent_model_calls 限制。
    AGENTS.md 指导依赖顺序与委派数量，不在编译时把角色连成固定串行工作流。
    """
    if type(max_model_calls) is not int or max_model_calls <= 0:
        raise ValueError("max_model_calls 必须是正整数")
    path = PurePosixPath(workdir)
    if not path.is_absolute() or ".." in path.parts or "\\" in workdir:
        raise ValueError("workdir 必须是远程绝对路径")
    if not isinstance(backend, SandboxBackendProtocol):
        raise TypeError("默认 Agent 需要支持 execute 的沙箱后端，不接受本地 FilesystemBackend")
    workdir = str(path)
    # 独立构建与 CLI 一致；只加载环境，不手工绑定回调或覆盖已有 tracing 开关。
    load_langsmith_environment()
    selected_model = model if model is not None else build_model()
    return create_deep_agent(
        model=selected_model,
        backend=backend,
        tools=[web_search, fetch_url],
        subagents=build_remote_subagents(
            backend, workdir, models=subagent_models, default_model=selected_model,
            max_model_calls=max_subagent_model_calls, user_id=user_id,
        ),
        skills=[f"{workdir}/.agents/skills/"],
        memory=[f"{workdir}/AGENTS.md"],
        middleware=[
            RequestContextMiddleware(user_id=user_id, channel="daytona"),
            TaskSerializationMiddleware(),
            ToolAuditMiddleware(), CostMeterMiddleware(),
            ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="error"),
        ],
        checkpointer=MemorySaver(),
        system_prompt=(
            f"你是严谨的研发助手。项目在远程沙箱 {workdir}，使用该目录下的绝对路径。"
            "先阅读 AGENTS.md 和相关开发技能，不把业务示例当成已有项目布局。"
            "所有代码和测试在远程 execute 中执行，不使用本机 run_pytest。"
            f"测试命令：cd {shlex.quote(workdir)} && python -m uv run --locked --extra daytona python -m pytest tests -q。"
            "通过 task 按需委派，开发任务遵循 AGENTS.md 的 coder → tester → reviewer 依赖规则。"
            "每轮至多两个独立只读委派、至多一个可能写入的委派；依赖上游结果时等待完成再决策。"
            "主代理不直接挂载本机表格工具，数据任务交 data_analyst 在远程执行。"
            "不读取或索取密钥；输出操作摘要、真实退出码、测试结果及必要报错。"
        ),
    )
