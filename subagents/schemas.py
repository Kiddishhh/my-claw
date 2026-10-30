"""代码审查子代理返回给主代理的结构化契约。"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReviewFinding(BaseModel):
    """可定位、可处理的审查发现。"""

    severity: Literal["error", "warning", "info"]
    file: str
    detail: str
    line: int | None = Field(default=None, ge=1)


class ReviewResult(BaseModel):
    """审查结论；通过审查不等同于测试已执行。"""

    approved: bool
    summary: str
    issues: list[ReviewFinding]
    security_findings: list[ReviewFinding]
    verification: str = Field(description="已看到的测试证据及尚未验证的部分")

    @model_validator(mode="after")
    def validate_approval(self) -> "ReviewResult":
        """有阻塞性发现时不能标记通过。"""
        if self.approved and any(item.severity == "error" for item in self.issues + self.security_findings):
            raise ValueError("存在 error 级发现时 approved 必须为 false")
        return self
