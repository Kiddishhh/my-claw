"""
agent/main.py —— 构建 DevMate 主 Agent（L3 的心脏）

- 模型来自第 1 章的配置中枢（多模型路由的雏形，第 7 章做成抽象层）
- 用官方唯一的工厂 create_deep_agent 构建；之后统一用 ainvoke/astream 异步调用
- system_prompt 是 DevMate 的"领域人设"，会追加到 DeepAgents 内置提示词之后
"""
from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent

from infra.settings import get_settings
from infra.logging import get_logger

logger = get_logger()

# DevMate 的人设：只写"领域要求"，
# "如何用 write_todos / 文件工具"这类通用本领由 DeepAgents 内置提示词负责。
DEVMATE_SYSTEM_PROMPT = """你是 DevMate，一个严谨的研发助手，服务于一个 Python / FastAPI 订单微服务团队。

工作准则：
- 动手前先用 write_todos 写出清晰的任务清单，并随进展更新它。
- 写代码遵循团队规范：类型注解齐全、函数职责单一、关键逻辑配 docstring。
- 不臆测：信息不足时先用 read_file / grep 读相关文件再动手。
- 每次改动后，用一两句话说明"改了什么、为什么这么改"。
"""


def build_model():
    """按第 1 章的配置构建模型。换模型只改 .env，不改这里。"""
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


def build_agent():
    """构建 DevMate 主 Agent，返回一个已编译的 LangGraph 图。

    注意：创建只用 create_deep_agent（官方唯一工厂）。
    "异步"体现在调用阶段——我们之后用 agent.ainvoke / agent.astream。
    """
    agent = create_deep_agent(
        model=build_model(),
        system_prompt=DEVMATE_SYSTEM_PROMPT,
    )
    logger.info("DevMate 主 Agent 构建完成：model={}", get_settings().model_name)
    return agent