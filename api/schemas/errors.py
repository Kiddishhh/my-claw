from api.schemas.common import APIModel


class ErrorResponse(APIModel):
    code: str
    message: str
    request_id: str
