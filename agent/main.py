"""
agent/main.py —— 本地 DevMate：FilesystemBackend + Skills + Memory。

文件工具直接访问项目目录，仅用于受信任的本地开发，不提供操作系统级沙箱隔离。
"""
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from infra.settings import get_settings
from infra.logging import get_logger

from middleware.audit import ToolAuditMiddleware
from middleware.cost import CostMeterMiddleware
from middleware.context import RequestContextMiddleware


logger = get_logger()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 虚拟路径相对于 FilesystemBackend 根目录，不是 Windows 系统盘路径。
SKILLS_SOURCES = ["/.agents/skills/"]
MEMORY_SOURCES = ["/AGENTS.md"]

# DevMate 的人设：只写"领域要求"，
# "如何用 write_todos / 文件工具"这类通用本领由 DeepAgents 内置提示词负责。
DEVMATE_SYSTEM_PROMPT = """你是 DevMate，一个严谨的研发助手，服务于一个 Python / FastAPI 订单微服务团队。

工作准则：
- 动手前先用 write_todos 写出清晰的任务清单，并随进展更新它。
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



def build_agent(
    user_id: str = "anonymous",
    channel: str = "cli",
    max_model_calls: int | None = None,
    *,
    workspace_root: Path | None = None,
    model: BaseChatModel | None = None,
) -> CompiledStateGraph:
    """构建本地文件 Agent，加载项目技能和规则。

    max_model_calls 是每次运行的模型调用上限；None 不额外限制。
    MemorySaver 仅在本 Agent 实例的进程内保存状态。调用时须传 thread_id，
    同一实例中复用 thread_id 可延续会话，重启进程不会保留 checkpoint。
    workspace_root 与 model 可用于隔离工作目录及离线测试，省略时使用项目目录和配置模型。
    """
    if max_model_calls is not None and (
        type(max_model_calls) is not int or max_model_calls <= 0
    ):
        raise ValueError("max_model_calls 必须是正整数或 None")

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

    agent = create_deep_agent(
        model=model if model is not None else build_model(),
        system_prompt=DEVMATE_SYSTEM_PROMPT,
        backend=backend,
        skills=SKILLS_SOURCES,
        memory=MEMORY_SOURCES,
        middleware=middleware,
        checkpointer=MemorySaver(),
    )
    logger.info("DevMate 构建完成：本地文件系统 / skills / memory / 中间件")
    return agent
