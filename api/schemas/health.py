from typing import Literal
from api.schemas.common import APIModel


class HealthResponse(APIModel):
    status: Literal["ok", "ready", "not_ready"]
