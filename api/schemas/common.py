from pydantic import BaseModel, ConfigDict


class APIModel(BaseModel):
    """禁止多余字段和隐式类型转换；每个具体接口还需声明字段限制。"""
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True, validate_default=True)
