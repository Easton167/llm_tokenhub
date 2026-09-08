"""用户端认证：注册、登录、当前用户资料（JWT）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserContext,
    UserOut,
)
from app.services.user_service import user_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    user = await user_service.register(
        db,
        email=body.email,
        password=body.password,
        nickname=body.nickname,
    )
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    _, token = await user_service.authenticate(
        db, email=body.email, password=body.password
    )
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(ctx: UserContext = Depends(get_user_context)) -> UserOut:
    return UserOut.model_validate(ctx.user)
