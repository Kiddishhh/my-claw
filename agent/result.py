"""渠道共用的最终回复转换；不把空内容推断为任务成功。"""
from typing import Any


def extract_reply_text(content: Any) -> str:
    """只提取可展示文本，不公开推理块、工具参数或内部对象。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""
