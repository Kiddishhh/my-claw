"""scripts/devmate_with_mw.py —— 带审计/成本/上下文中间件跑 DevMate

运行（项目根目录）：uv run --all-extras python -m scripts.devmate_with_mw
"""
import asyncio
import uuid

from agent.main import build_agent
from scripts._common import print_all


async def main():
    # 传入身份，观察 RequestContextMiddleware 注入效果
    agent = build_agent(user_id="alice", channel="cli")

    task = (
        "为订单服务实现一个折扣计算模块，放在 discount.py：calc_subtotal、apply_coupon、"
        "calc_final_total 三个函数，带类型注解、docstring，处理空列表与无效优惠券。"
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": task}]},
        config={"configurable": {"thread_id": uuid.uuid4().hex}},
    )

    print_all(result)

    # 顺带看成本中间件累加进状态的 token（CostState 的字段）
    print("\n=== 累计 token（来自 CostMeterMiddleware）===")
    print("input :", result.get("total_input_tokens"))
    print("output:", result.get("total_output_tokens"))


if __name__ == "__main__":
    asyncio.run(main())
