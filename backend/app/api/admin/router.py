"""管理 API 路由聚合。"""

from fastapi import APIRouter, Depends

from app.api.admin import (
    accounts,
    auth,
    billing,
    channel_costs,
    channels,
    kb,
    models,
    orgs,
    payments,
    providers,
    reports,
    roles,
    usage,
    wallets,
)
from app.api.admin.deps import enforce_admin_permission

admin_router = APIRouter(prefix="/admin")

# 登录接口不强制鉴权
admin_router.include_router(auth.router)

# 所需权限码由 enforce_admin_permission 从「方法 + 路径」推导，
# 见 app/core/admin_permissions.py 的资源目录；新增路由无需在此登记。
_admin_deps = [Depends(enforce_admin_permission)]
admin_router.include_router(providers.router, dependencies=_admin_deps)
admin_router.include_router(channels.router, dependencies=_admin_deps)
admin_router.include_router(channel_costs.router, dependencies=_admin_deps)
admin_router.include_router(models.router, dependencies=_admin_deps)
admin_router.include_router(billing.router, dependencies=_admin_deps)
admin_router.include_router(roles.router, dependencies=_admin_deps)
admin_router.include_router(roles.users_router, dependencies=_admin_deps)
admin_router.include_router(wallets.router, dependencies=_admin_deps)
# 赠送接口虽挂在 /wallets 下，但解析为独立的 gift 资源，可与充值/调账分开授权
admin_router.include_router(wallets.gift_ops_router, dependencies=_admin_deps)
admin_router.include_router(wallets.gifts_router, dependencies=_admin_deps)
admin_router.include_router(usage.router, dependencies=_admin_deps)
admin_router.include_router(reports.router, dependencies=_admin_deps)
admin_router.include_router(orgs.router, dependencies=_admin_deps)
admin_router.include_router(payments.router, dependencies=_admin_deps)
admin_router.include_router(kb.router, dependencies=_admin_deps)
admin_router.include_router(accounts.router, dependencies=_admin_deps)
admin_router.include_router(accounts.roles_router, dependencies=_admin_deps)
admin_router.include_router(accounts.permissions_router, dependencies=_admin_deps)
