"""OpenAI 兼容 v1 路由聚合。

挂到应用根路径前缀 ``/v1``：
- GET  /v1/models
- POST /v1/chat/completions
- POST /v1/embeddings
"""

from fastapi import APIRouter

from app.api.v1.chat import router as chat_router
from app.api.v1.embeddings import router as embeddings_router
from app.api.v1.models import router as models_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(models_router)
v1_router.include_router(chat_router)
v1_router.include_router(embeddings_router)
