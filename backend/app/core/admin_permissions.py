"""后台 RBAC 的唯一事实源：资源目录、操作权限码、URL → 权限码解析。

菜单不入库。侧边栏必须与前端路由一一对应，而路由是代码；把菜单写成常量并挂上
权限码后，角色只需存「授予了哪些权限码」，菜单可见性 = 是否拥有该资源的 view 权限，
菜单、路由、权限三者不可能漂移，也省掉一张表和一套菜单 CRUD。

权限码形如 ``<resource>:<action>``。后端不在每个路由上声明所需权限，而是统一按
「HTTP 方法 → action、URL 前缀 → resource」推导（见 ``resolve_required_permission``），
新增管理路由无需改鉴权代码。未登记的前缀会解析失败，调用方据此拒绝（fail-closed），
避免漏配变成越权。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ACTION_VIEW = "view"
ACTION_MANAGE = "manage"
ACTION_DELETE = "delete"

ACTION_LABELS: dict[str, str] = {
    ACTION_VIEW: "查看",
    ACTION_MANAGE: "新增 / 编辑",
    ACTION_DELETE: "删除",
}

# HTTP 方法到操作的固定映射；不做更细的 create/update 区分，
# 否则 POST /channels/{id}/test 这类「动词式写操作」必须逐条例外。
_METHOD_ACTIONS: dict[str, str] = {
    "GET": ACTION_VIEW,
    "HEAD": ACTION_VIEW,
    "OPTIONS": ACTION_VIEW,
    "POST": ACTION_MANAGE,
    "PUT": ACTION_MANAGE,
    "PATCH": ACTION_MANAGE,
    "DELETE": ACTION_DELETE,
}

# 登录与「我是谁」不参与权限校验（前者未认证，后者只需认证）
AUTH_EXEMPT_PREFIX = "/admin/auth"


@dataclass(frozen=True)
class AdminResource:
    """一个后台资源：既是权限码前缀，也是侧边栏的一项。

    Attributes:
        key: 权限码前缀，如 ``channel``。
        name: 侧边栏与权限配置页展示的中文名。
        actions: 该资源支持的操作，决定权限配置页勾选框数量。
        api_prefixes: 归属该资源的后端 ``/admin`` 路径前缀，用于反查权限码。
        path: 前端路由；``None`` 表示仅作为权限资源，不进侧边栏。
        icon: 前端图标名，由 ``AdminLayout`` 的 ICONS 映射到组件。
    """

    key: str
    name: str
    actions: tuple[str, ...]
    api_prefixes: tuple[str, ...]
    path: str | None = None
    icon: str | None = None


ADMIN_RESOURCES: tuple[AdminResource, ...] = (
    AdminResource(
        key="channel",
        name="渠道管理",
        actions=(ACTION_VIEW, ACTION_MANAGE, ACTION_DELETE),
        api_prefixes=("/admin/channels", "/admin/providers"),
        path="/admin/channels",
        icon="channel",
    ),
    AdminResource(
        key="model",
        name="模型管理",
        actions=(ACTION_VIEW, ACTION_MANAGE, ACTION_DELETE),
        api_prefixes=("/admin/models",),
        path="/admin/models",
        icon="model",
    ),
    AdminResource(
        key="user",
        name="用户管理",
        actions=(ACTION_VIEW, ACTION_MANAGE, ACTION_DELETE),
        api_prefixes=("/admin/users",),
        path="/admin/users",
        icon="user",
    ),
    AdminResource(
        key="role",
        name="角色权限",
        actions=(ACTION_VIEW, ACTION_MANAGE, ACTION_DELETE),
        api_prefixes=("/admin/roles",),
        path="/admin/roles",
        icon="role",
    ),
    AdminResource(
        key="kb",
        name="知识库管理",
        actions=(ACTION_VIEW, ACTION_MANAGE),
        api_prefixes=("/admin/kb",),
        path="/admin/kb",
        icon="kb",
    ),
    AdminResource(
        key="org",
        name="组织管理",
        actions=(ACTION_VIEW, ACTION_MANAGE, ACTION_DELETE),
        api_prefixes=("/admin/orgs",),
        path="/admin/orgs",
        icon="org",
    ),
    AdminResource(
        key="wallet",
        name="钱包管理",
        actions=(ACTION_VIEW, ACTION_MANAGE),
        api_prefixes=("/admin/wallets",),
        path="/admin/wallets",
        icon="wallet",
    ),
    AdminResource(
        key="gift",
        name="赠送余额",
        actions=(ACTION_VIEW, ACTION_MANAGE),
        api_prefixes=("/admin/gifts",),
        path=None,  # 赠送入口在钱包页内，单独成权限资源以便与充值/调账分权
        icon=None,
    ),
    AdminResource(
        key="payment",
        name="支付单",
        actions=(ACTION_VIEW,),
        api_prefixes=("/admin/payments",),
        path="/admin/payments",
        icon="payment",
    ),
    AdminResource(
        key="stats",
        name="运营数据",
        actions=(ACTION_VIEW, ACTION_MANAGE),
        api_prefixes=(
            "/admin/request-logs",
            "/admin/usage-logs",
            "/admin/usage-summary",
            "/admin/usage",
        ),
        path="/admin/stats",
        icon="stats",
    ),
    AdminResource(
        key="report",
        name="运营报表",
        actions=(ACTION_VIEW,),
        api_prefixes=("/admin/reports",),
        path="/admin/reports",
        icon="report",
    ),
    AdminResource(
        key="admin_account",
        name="后台账号",
        actions=(ACTION_VIEW, ACTION_MANAGE, ACTION_DELETE),
        api_prefixes=("/admin/accounts", "/admin/admin-roles", "/admin/permissions"),
        path="/admin/accounts",
        icon="admin_account",
    ),
)

RESOURCES_BY_KEY: dict[str, AdminResource] = {r.key: r for r in ADMIN_RESOURCES}


def permission_code(resource_key: str, action: str) -> str:
    return f"{resource_key}:{action}"


ALL_PERMISSIONS: frozenset[str] = frozenset(
    permission_code(r.key, a) for r in ADMIN_RESOURCES for a in r.actions
)

# 前缀 → 资源，按长度倒序：/admin/usage-logs 必须先于 /admin/usage 命中
_PREFIX_TO_RESOURCE: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((prefix, r.key) for r in ADMIN_RESOURCES for prefix in r.api_prefixes),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

# 赠送接口挂在 /admin/wallets 下（如 /admin/wallets/users/{id}/gifts），
# 靠前缀分不开，用正则改判到 gift 资源。
_RESOURCE_OVERRIDES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/admin/wallets/.+/gifts/?$"), "gift"),
)


def _resolve_resource(path: str) -> str | None:
    for pattern, resource_key in _RESOURCE_OVERRIDES:
        if pattern.match(path):
            return resource_key
    for prefix, resource_key in _PREFIX_TO_RESOURCE:
        if path == prefix or path.startswith(f"{prefix}/"):
            return resource_key
    return None


def resolve_required_permission(method: str, path: str) -> str | None:
    """推导访问该请求所需的权限码。

    Returns:
        权限码；``None`` 表示路径未登记或方法未知，调用方应当拒绝而不是放行。
    """
    resource_key = _resolve_resource(path)
    if resource_key is None:
        return None
    action = _METHOD_ACTIONS.get(method.upper())
    if action is None:
        return None
    return permission_code(resource_key, action)


def menu_items() -> list[dict[str, str]]:
    """侧边栏候选项（未过滤权限），顺序即展示顺序。"""
    return [
        {"key": r.key, "name": r.name, "path": r.path, "icon": r.icon or r.key}
        for r in ADMIN_RESOURCES
        if r.path
    ]


def permission_catalog() -> list[dict[str, object]]:
    """权限配置页用的目录：按资源分组的可授予权限码。"""
    return [
        {
            "resource": r.key,
            "name": r.name,
            "permissions": [
                {"code": permission_code(r.key, a), "action": a, "name": ACTION_LABELS[a]}
                for a in r.actions
            ],
        }
        for r in ADMIN_RESOURCES
    ]
