"""用户注册、登录与资料查询。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED
from app.core.errors import ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.db.models.user import User
from app.services.wallet_service import wallet_service


class UserService:
    async def get_by_id(self, session: AsyncSession, user_id: UUID) -> User | None:
        return await session.get(User, user_id)

    async def get_by_email(self, session: AsyncSession, email: str) -> User | None:
        stmt = select(User).where(User.email == email.lower().strip())
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def register(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        nickname: str | None = None,
    ) -> User:
        normalized = email.lower().strip()
        existing = await self.get_by_email(session, normalized)
        if existing is not None:
            raise ConflictError("Email already registered")

        user = User(
            email=normalized,
            password_hash=hash_password(password),
            nickname=nickname,
            status=STATUS_ENABLED,
        )
        session.add(user)
        await session.flush()
        await wallet_service.get_or_create_personal_wallet(session, user.id)
        return user

    async def authenticate(
        self, session: AsyncSession, *, email: str, password: str
    ) -> tuple[User, str]:
        """校验邮箱密码，返回用户与 access_token。"""
        user = await self.get_by_email(session, email.lower().strip())
        if user is None or not user.password_hash:
            raise UnauthorizedError("Invalid email or password")
        if not verify_password(password, user.password_hash):
            raise UnauthorizedError("Invalid email or password")
        if user.status != STATUS_ENABLED:
            raise ForbiddenError("User account is disabled")
        token = create_access_token(user_id=user.id)
        return user, token

    async def require_active_user(self, session: AsyncSession, user_id: UUID) -> User:
        user = await self.get_by_id(session, user_id)
        if user is None:
            raise UnauthorizedError("Invalid token")
        if user.status != STATUS_ENABLED:
            raise ForbiddenError("User account is disabled")
        return user


user_service = UserService()
