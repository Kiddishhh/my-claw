"""middleware/cost.py —— DevMate 的成本计量中间件（异步）

用 awrap_model_call 包裹模型调用，调用后从 AIMessage.usage_metadata 读 token，
并按官方规范用 ExtendedModelResponse + Command 把累计用量写进 agent 状态。
本章只计量；真正的"超预算熔断"在第 11 章基于本中间件实现。
"""
from collections.abc import Callable

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ExtendedModelResponse,
)
from langgraph.types import Command
from typing_extensions import NotRequired

from infra.logging import get_logger

logger = get_logger()


class CostState(AgentState):
    """在 agent 状态里加三个累计字段（用 NotRequired，调用方无需初始化）。"""
    total_input_tokens: NotRequired[int]
    total_output_tokens: NotRequired[int]


class CostMeterMiddleware(AgentMiddleware):
    """统计每次模型调用的 token 用量，累加进状态。"""

    # 声明扩展状态（官方做法）
    state_schema = CostState

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ExtendedModelResponse:
        response = await handler(request)      # 放行：真正调用模型

        in_tok, out_tok = self._extract_usage(response)

        # 从当前状态读累计值（NotRequired，默认 0），再累加
        prev_in = request.state.get("total_input_tokens", 0)
        prev_out = request.state.get("total_output_tokens", 0)
        new_in, new_out = prev_in + in_tok, prev_out + out_tok

        logger.info(
            "💰 本次模型调用：in={} out={} | 累计 in={} out={}",
            in_tok, out_tok, new_in, new_out,
        )

        # 官方规范：包裹式钩子更新状态 → 返回 ExtendedModelResponse + Command
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={
                "total_input_tokens": new_in,
                "total_output_tokens": new_out,
            }),
        )

    @staticmethod
    def _extract_usage(response: ModelResponse) -> tuple[int, int]:
        """从 ModelResponse 里挖出最后一条 AIMessage 的 usage_metadata。
        usage_metadata 是 LangChain 统一的用量字段，含 input_tokens / output_tokens。"""
        msgs = getattr(response, "result", None) or getattr(response, "messages", None) or []
        for msg in reversed(msgs):
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        return 0, 0