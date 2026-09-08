"""管理端登录与会话信息。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.deps import require_admin
from app.core.admin_permissions import menu_items
from app.core.security import create_admin_token
from app.db.session import get_db
from app.schemas.admin_rbac import AdminContext, AdminMeOut
from app.schemas.auth import TokenResponse
from app.services import admin_rbac_service

router = APIRouter(prefix="/auth", tags=["admin-auth"])


class AdminLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/login", response_model=TokenResponse)
async def admin_login(
    body: AdminLoginRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    """校验管理员账号，签发 typ=admin 的 JWT。"""
    ctx = await admin_rbac_service.authenticate(db, body.username, body.password)
    return TokenResponse(access_token=create_admin_token(username=ctx.username))


@router.get("/me", response_model=AdminMeOut)
async def admin_me(ctx: AdminContext = Depends(require_admin)) -> AdminMeOut:
    """返回当前管理员的权限码与可见菜单，前端据此渲染侧边栏与按钮。"""
    menus = [m for m in menu_items() if ctx.has(f"{m['key']}:view")]
    return AdminMeOut(
        username=ctx.username,
        is_superadmin=ctx.is_superadmin,
        permissions=sorted(ctx.permissions),
        menus=menus,
    )
