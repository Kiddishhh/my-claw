"""摘要审计必须区分工具错误与成功，不能打印误导性的勾号。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain.messages import ToolMessage
from middleware import audit


def test_tool_error_is_logged_as_failure(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(audit, "logger", logger)
    async def handler(request):
        return ToolMessage(content="failed", status="error", tool_call_id="id")
    result = asyncio.run(audit.ToolAuditMiddleware().awrap_tool_call(
        SimpleNamespace(tool_call={"name": "execute"}), handler))
    assert result.status == "error"
    logger.warning.assert_called_once()
    assert all("✅" not in str(c) for c in logger.info.call_args_list)


def test_exception_is_logged_and_propagated(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(audit, "logger", logger)
    async def handler(request):
        raise ValueError("failure")
    with pytest.raises(ValueError):
        asyncio.run(audit.ToolAuditMiddleware().awrap_tool_call(
            SimpleNamespace(tool_call={"name": "execute"}), handler))
    logger.error.assert_called_once()
