from typing import Annotated
from fastapi import Depends, HTTPException, Request
from infra.resources import AppResources


def get_resources(request: Request) -> AppResources:
    """只获取本进程已经初始化的资源，不创建新实例。"""
    resources = getattr(request.app.state, "resources", None)
    if resources is None:
        raise HTTPException(status_code=503)
    return resources


ResourcesDep = Annotated[AppResources, Depends(get_resources)]
