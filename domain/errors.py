"""业务层只报告错误代码，不导入 FastAPI，不自行决定 HTTP 状态码。"""


class BusinessError(Exception):
    """可预期的业务失败；客户端提示由 API 的固定映射生成。"""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
