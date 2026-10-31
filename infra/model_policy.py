"""DeepSeek 按角色控制思考开销，并保留工具调用所需的推理字段。"""
from urllib.parse import urlsplit

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

# None 表示关闭思考；复杂规划、代码实现和数据推导保留低强度思考。
# 修改此表即可调整角色策略，配置不会写入共享的原始模型实例。
ROLE_REASONING = {
    "main": None, "planner": "low", "researcher": None, "coder": "low",
    "tester": None, "reviewer": None, "data_analyst": "low", "general-purpose": None,
}


class DeepSeekChatModel(ChatDeepSeek):
    """基于官方适配器补齐 1.1.1 的请求侧 reasoning_content 回传。

    官方适配器负责普通/流式响应的推理字段提取；当前版本仍遗漏请求侧字段。
    仅把已有 AIMessage 的字段原样带回，不打印、不伪造，也不新增模型调用。
    升级集成包时用协议回归测试判断是否可删除这一兼容层。
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs) -> dict:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if payload.get("tools"):
            for message, encoded in zip(messages, payload["messages"], strict=True):
                if isinstance(message, AIMessage):
                    reasoning = message.additional_kwargs.get("reasoning_content")
                    if reasoning is not None:
                        encoded["reasoning_content"] = reasoning
        return payload


def model_for_role(model: BaseChatModel, role: str) -> BaseChatModel:
    """只调整官方 DeepSeek 端点；其他服务与离线测试模型保持原样。"""
    if role not in ROLE_REASONING:
        raise ValueError(f"未知模型角色：{role}")
    if not isinstance(model, (ChatDeepSeek, ChatOpenAI)):
        return model
    endpoint = model.api_base if isinstance(model, ChatDeepSeek) else model.openai_api_base
    if urlsplit(str(endpoint or "")).hostname != "api.deepseek.com":
        return model
    effort = ROLE_REASONING[role]
    if effort and not isinstance(model, DeepSeekChatModel):
        raise ValueError("DeepSeek 思考角色请使用 infra.model_policy.DeepSeekChatModel，以保留工具调用推理上下文")
    body = dict(model.extra_body or {})
    body.pop("reasoning", None)
    body.pop("reasoning_effort", None)
    body["thinking"] = {"type": "enabled" if effort else "disabled"}
    return model.model_copy(update={"extra_body": body, "reasoning_effort": effort})
