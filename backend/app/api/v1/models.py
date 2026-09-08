"""GET /v1/models - 返回当前 API Key 有权使用且已启用的模型列表。

兼容 OpenAI /v1/models 响应结构：
  {"object": "list", "data": [{"id": "gpt-4o", "object": "model", ...}]}
"""

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_auth_context
from app.db.session import get_db
from app.schemas.auth import AuthContext
from app.schemas.openai import ModelListResponse
from app.services.permission_service import permission_service

router = APIRouter(tags=["v1"])


@router.get("/models", response_model=ModelListResponse)
async def list_models(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ModelListResponse:
    """返回当前 API Key 可使用的模型列表（OpenAI 兼容格式）。

    - 只返回已启用且用户角色已授权（或 visibility=public）的模型。
    - 模型 id 为平台对外别名（alias），与上游模型名解耦。
    """
    models = await permission_service.list_authorized_model_infos(
        db,
        user_id=auth.user_id,
        created=int(time.time()),
        api_key=auth.api_key,
    )
    return ModelListResponse(object="list", data=models)
