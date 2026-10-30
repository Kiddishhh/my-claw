"""单代理模式下移除 DeepAgents 自动添加的 task 工具。"""
from langchain.agents.middleware import AgentMiddleware


class SingleAgentMiddleware(AgentMiddleware):
    """利用框架按名称替换中间件的机制，替换默认委派中间件。

    仅传 subagents=[] 仍会生成 general-purpose。这里不提供任何工具或委派提示，
    不修改全局模型 profile；测试必须同时验证模型绑定和工具执行端没有 task。
    """

    @property
    def name(self) -> str:
        """匹配 DeepAgents 默认委派中间件的公开名称。"""
        return "SubAgentMiddleware"
