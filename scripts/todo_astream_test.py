"""scripts/ch02_watch_devmate.py —— 第 2 章：看 DevMate 一步步干活（异步流式 astream）

运行（项目根目录）：
    uv run python scripts/todo_astream_test.py
    uv run python -m scripts.todo_astream_test
"""
import asyncio
from pathlib import Path
import sys
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.main import build_agent

# 用多步任务，便于看到它先 write_todos 列计划、再 write_file 写文件
MULTI_STEP_TASK = (
    "为订单服务实现一个折扣计算模块，放在 discount.py：calc_subtotal、apply_coupon、"
    "calc_final_total 三个函数，带类型注解、docstring，并处理空列表与无效优惠券。"
)


async def main():
    agent = build_agent()

    # stream_mode="updates"：只打印每一步"新产生"的更新，最适合观察 Agent 决策过程
    async for chunk in agent.astream(
        {"messages": [{"role": "user", "content": MULTI_STEP_TASK}]},
        config={"configurable": {"thread_id": uuid.uuid4().hex}},
        stream_mode="updates",
    ):
        for node_name, update in chunk.items():
            print(f"\n▶ 节点：{node_name}")
            msgs = update.get("messages") if isinstance(update, dict) else None
            if msgs:
                msgs[-1].pretty_print()


if __name__ == "__main__":
    asyncio.run(main())
