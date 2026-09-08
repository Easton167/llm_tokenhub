"""URL → 权限码解析的自检。

这层是后台鉴权的唯一判定点：解析错一条前缀就等于开了一个后门，
所以每条真实存在的管理路由都必须落到预期资源上，未登记路径必须解析失败。
"""

import pytest

from app.core.admin_permissions import (
    ADMIN_RESOURCES,
    ALL_PERMISSIONS,
    menu_items,
    permission_catalog,
    resolve_required_permission,
)
from app.main import app


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/admin/channels", "channel:view"),
        ("POST", "/admin/channels", "channel:manage"),
        ("PATCH", "/admin/channels/abc", "channel:manage"),
        ("DELETE", "/admin/channels/abc/keys/def", "channel:delete"),
        ("POST", "/admin/channels/abc/test", "channel:manage"),
        ("GET", "/admin/channels/abc/costs", "channel:view"),
        ("GET", "/admin/providers", "channel:view"),
        ("POST", "/admin/models/abc/billing-rules", "model:manage"),
        ("PUT", "/admin/users/abc/password", "user:manage"),
        ("DELETE", "/admin/roles/abc", "role:delete"),
        ("POST", "/admin/kb/abc/disable", "kb:manage"),
        ("POST", "/admin/orgs/abc/deactivate", "org:manage"),
        ("POST", "/admin/wallets/users/abc/recharge", "wallet:manage"),
        ("GET", "/admin/wallets/abc/transactions", "wallet:view"),
        # 赠送挂在 /wallets 下但独立成资源，才能与充值/调账分开授权
        ("POST", "/admin/wallets/users/abc/gifts", "gift:manage"),
        ("POST", "/admin/wallets/orgs/abc/gifts", "gift:manage"),
        ("GET", "/admin/wallets/abc/gifts", "gift:view"),
        ("POST", "/admin/gifts/abc/revoke", "gift:manage"),
        ("GET", "/admin/payments", "payment:view"),
        # 长前缀优先：/admin/usage-logs 不能被 /admin/usage 抢走
        ("GET", "/admin/usage-logs", "stats:view"),
        ("GET", "/admin/request-logs", "stats:view"),
        ("POST", "/admin/usage/daily-aggregate", "stats:manage"),
        ("GET", "/admin/reports/trend", "report:view"),
        ("GET", "/admin/accounts", "admin_account:view"),
        ("PATCH", "/admin/admin-roles/abc", "admin_account:manage"),
        ("GET", "/admin/permissions", "admin_account:view"),
    ],
)
def test_resolve_required_permission(method: str, path: str, expected: str) -> None:
    assert resolve_required_permission(method, path) == expected


def test_unregistered_path_fails_closed() -> None:
    assert resolve_required_permission("GET", "/admin/something-new") is None


def test_every_admin_route_resolves_to_a_grantable_permission() -> None:
    """除登录/我是谁外，所有 /admin 路由都必须能解析出目录里存在的权限码。

    否则该路由只有超管能访问，且不会有任何报错提示——正是最容易漏掉的坑。
    """
    unresolved: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin") or path.startswith("/admin/auth"):
            continue
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            # 用占位值替掉 {param}，模拟真实请求路径
            concrete = path
            while "{" in concrete:
                head, _, rest = concrete.partition("{")
                _, _, tail = rest.partition("}")
                concrete = f"{head}x{tail}"
            code = resolve_required_permission(method, concrete)
            if code not in ALL_PERMISSIONS:
                unresolved.append(f"{method} {path} -> {code}")
    assert not unresolved, f"未登记的管理路由: {unresolved}"


def test_catalog_and_menus_are_consistent() -> None:
    catalog_codes = {
        p["code"] for group in permission_catalog() for p in group["permissions"]
    }
    assert catalog_codes == set(ALL_PERMISSIONS)

    menu_keys = {m["key"] for m in menu_items()}
    resources_with_path = {r.key for r in ADMIN_RESOURCES if r.path}
    assert menu_keys == resources_with_path
    # 菜单可见性依赖 <key>:view，缺了就永远看不见
    assert all(f"{key}:view" in ALL_PERMISSIONS for key in menu_keys)
