# 阶段 3 实施细则：认证与权限——请求是怎么被放行的

> 配套 `docs/learning_plan.md` 的「阶段 3」。阅读模式字母见 [README](./README.md#二四种读代码的方式后面每条读单都会标)。
> 前置：阶段 2 过关。
> 产出物：`docs/learning/notes/stage-3.md`，含 3 条鉴权入口的对比表 + 两套角色对照 + 1 次断点跟踪记录 + 1 个可跑的新接口。

**本阶段一句话**：这个项目有**三套入口、两套角色**。三套入口用不同的 Bearer；两套角色一个管「用户能调哪些模型」，一个管「管理员能进哪些菜单」——共用一张表只会让前台注册变成提权入口。

先记住这张表，读的过程中不断回来核对：

| | 用户端 REST | OpenAI 网关 | 管理后台 |
| --- | --- | --- | --- |
| 路由 | `/auth`、`/keys`、`/wallets`、`/orgs`… | `/v1/*` | `/admin/*` |
| `Authorization: Bearer` 后面是 | **JWT** | **API Key 明文**（`sk-...`） | **JWT**（另一种） |
| 依赖函数 | `get_user_context`（`api/deps.py:39`） | `get_auth_context`（`api/deps.py:19`） | `require_admin` 验身份（`api/admin/deps.py:24`）；`enforce_admin_permission` 验权限码（`:39`） |
| JWT 的 `typ` | `"user"` | — | `"admin"` |
| 身份来自 | `users` 表 | `api_keys` 表 → 关联 user | **`admin_users` 表**（库里没有同名账号时，回落到 `.env` 的超管兜底） |

---

## 一、怎么读

### 读单总览

| # | 内容 | 模式 | 预算 | 核心问题 |
| --- | --- | --- | --- | --- |
| D1 | `tests/test_auth_permission.py` + `tests/test_user_api.py` | T | 40 min | 先看行为，再看实现 |
| D2 | `app/core/security.py` | Q | 40 min | 密码 / JWT / Key 三套密码学 |
| D3 | `app/api/auth.py` + `app/services/user_service.py` | Q | 25 min | 注册登录链路 |
| D4 | `app/api/deps.py` + `app/services/auth_service.py` | Q+B | 50 min | Bearer 怎么变成「当前用户」 |
| D5 | `app/services/permission_service.py` | Q | 40 min | 前台角色-模型授权矩阵 |
| D6 | `admin_rbac_service.py` + `admin/auth.py` + `admin/deps.py` | T+Q | 30 min | 管理员从哪来（库表 / `.env` 兜底） |
| D7 | `admin_permissions.py` + `admin/accounts.py` | T+Q | 50 min | 权限码怎么推导、提权怎么挡住 |

---

### D1 · 先读测试（模式 T，40 分钟）

**不要先读实现。**这两个测试文件就是这一层的需求文档。后台 RBAC 的测试放在 D6 / D7，这一段先别管。

读法：只看**函数名**和**最后几行 assert**，把它整理成「场景 → 期望」表抄进笔记。

`backend/tests/test_auth_permission.py`：

| 测试 | 期望 |
| --- | --- |
| `test_verify_api_key_success` | 合法 Key → 返回 user / key / billing_subject |
| `test_verify_api_key_invalid_hash` | 库里不存在的 Key → 401 `invalid_api_key` |
| `test_verify_api_key_disabled` | Key 被禁用 → 401 |
| `test_verify_api_key_bad_format` | 不以 `sk-` 开头 → 401（**注意：连查库都不查**） |
| `test_check_model_access_authorized` | 角色有授权 → 放行 |
| `test_check_model_access_public_model` | `visibility=public` → 不需要授权 |
| `test_check_model_access_not_authorized` | 无授权 → **403** `model_not_authorized` |
| `test_check_model_access_not_found` | 模型不存在 → **404** |
| `test_check_model_access_disabled_model` | 模型被禁用 → **404**（不是 403） |
| `test_list_authorized_models` | 列表 = 已授权 + public |
| `test_auth_dependency_invalid_key` / `_valid_key` | HTTP 层验证 401 / 200 |

`backend/tests/test_user_api.py`：

| 测试 | 期望 |
| --- | --- |
| `test_register_login_and_me` | 注册 201 → 重复注册 **409** → 登录拿 token → `/auth/me` 200 |
| `test_login_invalid_credentials` | 密码错 → 401 |
| `test_keys_crud_and_plaintext_once` | **明文 Key 只在创建时返回一次**；删除后立即失效 |
| `test_wallet_and_usage_require_jwt` | 无 JWT → 401 |
| `test_usage_scoped_with_jwt` | 只能看到自己的数据 |
| `test_delete_foreign_key_returns_404` | **删别人的 Key → 404 而不是 403** |

最后一条特别值得停下来想 30 秒：**为什么是 404 不是 403？**（提示：如果返回 403，攻击者就能通过遍历 id 探测出「哪些 id 是存在的」）这叫「不泄露资源存在性」，是一个通用的安全习惯。去 `app/api/keys.py:112-116` 看它是怎么实现的——`where` 里同时带了 `id` 和 `user_id`。

---

### D2 · `backend/app/core/security.py`（约 140 行，模式 Q，逐行读）

这个文件很短但信息密度极高，分三块：

#### 2.1 API Key（`:32-71`）

```
generate_api_key_material()  →  (plaintext, key_hash, key_prefix)
   plaintext  = "sk-" + secrets.token_urlsafe(32)     只返回一次，不入库
   key_hash   = sha256(pepper + plaintext)            入库，unique
   key_prefix = plaintext[:10] + "..."                入库，仅供展示
```

**注意一个和学习计划描述不一致的事实**：`docs/learning_plan.md` 说「为什么用 bcrypt 而不是 md5」，但实际上 **API Key 用的是 SHA-256 + pepper（`:32-40`），只有用户密码用 bcrypt（`:74-76`）**。这个差异恰恰是本阶段最值得想清楚的问题：

1. 密码用 bcrypt（慢、带盐、每次哈希结果不同），Key 用 SHA-256（快、确定性）——**为什么不统一**？
   提示 A：bcrypt 每次结果不同，所以验证只能「取出候选再逐个 checkpw」。API Key 有几百万个，你怎么查？看 `app/services/auth_service.py:37-41`——它是**直接拿哈希做等值查询**，这只有确定性哈希才做得到。
   提示 B：一次 bcrypt 校验要几十到几百毫秒。用户登录一天几次无所谓，网关每秒几百次请求，扛得住吗？
2. 那 SHA-256 不怕被暴力破解吗？—— 看 `secrets.token_urlsafe(32)`（`:49`），这是 256 bit 的随机熵。对比用户密码可能是 `abc123`。**弱口令才需要慢哈希，高熵随机串不需要。**
3. `pepper`（`:39`，来自 `settings.api_key_pepper`）和 salt 有什么区别？（提示：salt 存在数据库里，pepper 存在配置里。数据库被拖库了，攻击者拿到了什么、没拿到什么）

#### 2.2 JWT（`:87-140`）

四个函数，两两成对：

| 函数 | 行号 | payload |
| --- | --- | --- |
| `create_access_token` | `87-99` | `{sub: user_id, typ: "user", iat, exp}` |
| `decode_access_token` | `102-114` | 校验 `typ == "user"`，返回 UUID |
| `create_admin_token` | `117-129` | `{sub: username, typ: "admin", iat, exp}` |
| `decode_admin_token` | `132-140` | 校验 `typ == "admin"`，返回 username |

关键问题：

1. 用户 token 和管理端 token **用的是同一个 `settings.jwt_secret`**。那什么东西阻止了「拿用户 token 去调 `/admin/*`」？（答案在 `:109` 和 `:135` 的 `typ` 检查。**这一行如果漏了就是越权漏洞**，值得记住这种模式）
2. JWT 是无状态的。那么「用户改了密码，旧 token 应该立刻失效」这个需求，当前实现做得到吗？（做不到——想清楚为什么，以及业界怎么解决：短 exp + refresh token，或维护一个 revocation list。对比：管理员被**禁用**后，下一次请求会 401——因为 `load_context` 每次都查库看 `status`，见 D6）
3. `exp` 从 `settings.jwt_expire_minutes` 来（`:90`，默认 `60*24` = 24 小时）。对一个能花钱的系统，24 小时长吗？
4. 用户 JWT 的 `sub` 是 UUID，管理端 JWT 的 `sub` 是 **username 字符串**。为什么不统一成 id？（D6 会回答：环境变量兜底超管没有数据库 id）

#### 2.3 辅助（`:59-67`）

`parse_bearer_token`（`:59-67`）：**格式不对时返回空字符串而不是抛异常**。为什么这么设计？（去看它的两个调用方 `api/deps.py:23-25` 和 `:44-46`，它们抛的异常**不一样**——一个 `InvalidApiKeyError`，一个 `UnauthorizedError`。如果 `parse_bearer_token` 自己抛，就没法区分了）

---

### D3 · 注册登录链路（模式 Q，25 分钟）

**注意一个容易走错的地方**：学习计划写的是 `app/services/auth_service.py`，但**注册登录其实在 `app/services/user_service.py`**。`auth_service.py` 只管 API Key。

链路：

```
POST /auth/register   →  api/auth.py:20-31   →  user_service.register (:28-50)
POST /auth/login      →  api/auth.py:34-42   →  user_service.authenticate (:52-64)
GET  /auth/me         →  api/auth.py:45-47   →  依赖 get_user_context 即可，无 service
```

读 `user_service.register`（`:28-50`）时注意最后一步：**注册的同时创建了个人钱包**（调 `wallet_service.get_or_create_personal_wallet`）。问题：这两个操作在同一个事务里吗？如果建钱包失败了，用户会被创建出来吗？（回看阶段 1 的 `get_db`，`session.py:48-51`）

读 `authenticate`（`:52-64`）注意异常的选择：密码错抛 `UnauthorizedError`（401），账号禁用抛 `ForbiddenError`（403）。为什么区别对待？**这里其实有个安全权衡**——邮箱不存在和密码错误都返回 401 是对的（不泄露邮箱是否注册），但账号禁用返回 403 就泄露了「这个邮箱存在」。想想这个取舍值不值。

---

### D4 · Bearer → 当前用户（模式 Q + B，50 分钟）

#### Q 阶段

把两条路径并排读：

**API Key 路径**（`api/deps.py:19-26` → `services/auth_service.py:30-60`）：

```
parse_bearer_token(authorization)              deps.py:23
  ↓ 空 → InvalidApiKeyError                     deps.py:24-25
auth_service.verify_api_key(db, raw_key)       deps.py:26
  ↓ is_valid_api_key_format 不过 → 401          auth_service.py:34-35
  ↓ hash_api_key(raw_key)                       auth_service.py:37
  ↓ SELECT ... WHERE key_hash = ?               auth_service.py:37-44
  ↓   selectinload(ApiKey.user)  ← 一次查询带出用户，避免 N+1
  ↓ 找不到 / Key 禁用 / 用户禁用 / 已过期 → 401  auth_service.py:46-53
  → AuthContext(user, api_key, billing_subject) auth_service.py:55-60
```

**JWT 路径**（`api/deps.py:39-52`）：

```
parse_bearer_token → decode_access_token → user_service.require_active_user
```

问题：

1. `auth_service.py:37-44` 用了 `selectinload(ApiKey.user)`。不加会怎样？（关键词：lazy load 在 async 下会直接抛 `MissingGreenlet`，不是变慢而是**直接报错**）
2. **每一个 `/v1` 请求都要查一次数据库**验 Key。这在高 QPS 下是瓶颈吗？项目里有个配置 `auth_cache_ttl_seconds`（`config.py:52`）——去 grep 一下它有没有被用上。（答案：**没有**。这说明什么？说明它是预留的优化位。发现「配置存在但没实现」是读大项目的常见收获，不要以为是自己看漏了）
3. `verify_api_key` 里对**四种失败**（不存在 / Key 禁用 / 用户禁用 / 过期）返回的是**同一个** `InvalidApiKeyError`。为什么不给用户更精确的提示？

#### B 阶段（断点跟踪，25 分钟）

1. 打断点：`api/deps.py:26`、`services/auth_service.py:37`、`services/auth_service.py:46`
2. F5 启动，用阶段 0 建的那个 Key 发请求：

```powershell
curl.exe -H "Authorization: Bearer sk-你的key" http://localhost:8000/v1/models
```

3. 在 Debug Console 里逐个执行并抄下真实值：

```
raw_key                                # 明文 Key
hash_api_key(raw_key)                  # 算出来的哈希
```

4. 拿这个哈希去数据库对：

```sql
SELECT id, key_prefix, status, expires_at FROM api_keys WHERE key_hash = '<粘贴哈希>';
```

**看到明文 Key、算出的哈希、库里的 key_hash 三者对上的那一刻**，你就真的懂「只存哈希不存明文」了。

5. 再故意发一个错的：`Bearer sk-wrong`，观察断点在 `auth_service.py:46` 命中、`api_key is None`、抛出 401。

---

### D5 · `backend/app/services/permission_service.py`（模式 Q，40 分钟）

这是**前台**授权：用户能调哪些模型。后台管理员的菜单权限是另一套（D7），先别混。

授权矩阵由三张表构成（阶段 2 已经见过）：

```
users ──< user_roles >── roles ──< role_model_grants >── models
```

加上 `models.visibility` 这个字段，规则是：

- `visibility = "public"` → 谁都能用，不查授权表
- `visibility = "role_restricted"` → 必须有一条 `role_model_grants` 连通

**还有第三层过滤**：`api_keys.allowed_models`（JSONB 数组）。看 `_key_allows_alias`（`:35-43`）和 `check_model_access`（`:45-77`）：

- `allowed_models` 为空 → 完全继承角色授权
- `allowed_models` 非空 → **和角色授权取交集**（不是并集！Key 只能收窄权限，不能放大）

问题：

1. 为什么 Key 级的模型范围只能收窄不能放大？（想一个场景：你把一个 Key 给了外包同事）
2. `check_model_access` 对「模型不存在」和「模型被禁用」都返回 404（测试 `test_check_model_access_disabled_model` 已验证）。为什么禁用不返回 403？
3. `user_has_model_grant`（`:23-33`）是一条 join 查询。在 `/v1/models` 列表场景（`list_authorized_models`，`:79-114`），如果对每个模型都调一次这个函数会怎样？去看它实际是怎么写的——是 N 次查询还是 1 次？

**「一个用户看不到某个模型」的排查路径**（过关标准要求你能答出来），按这个顺序检查：

```
1. models 表里这个 alias 存在吗、status 是启用吗        → permission_service.py:62-67 → 404
2. visibility 是 public 还是 role_restricted            → permission_service.py:62-67
3. 用户有角色吗、角色有这个模型的 grant 吗              → user_has_model_grant :23-33 → 403
4. 这个 Key 的 allowed_models 非空且不含这个 alias 吗   → _key_allows_alias :35-43 → 403
```

四层任意一层拦下都会「看不到」，但**返回的状态码不同**（404 vs 403），这就是你排障时的第一个线索。

调用点：`/v1/models` 在 `api/v1/models.py:31-36`；chat 请求在 `api/v1/chat.py:162-167`（阶段 4 细看）。

---

### D6 · 管理端身份：谁是管理员（模式 T+Q，30 分钟）

先读测试再读实现。`tests/test_admin_rbac_api.py` 只看这两条的函数名和最后几行 assert：

| 测试 | 期望 |
| --- | --- |
| `test_env_fallback_admin_is_superadmin` | 库里没有同名账号 → `.env` 账号能登录，且 `is_superadmin` |
| `test_disabled_account_cannot_use_existing_token` | 账号禁用后，已签发的 JWT 立刻 401 |

然后读这条链路：

```
POST /admin/auth/login
  → admin/auth.py:admin_login (:23-29)
  → admin_rbac_service.authenticate (:62-78)
  → create_admin_token(username=...)

之后每个 /admin/* 请求
  → require_admin (:24-36)：decode_admin_token → load_context
  → enforce_admin_permission (:39-51)：超管放行 / 否则查权限码
```

**管理员不是 `users` 表里的用户。**这是有意的。`app/db/models/admin.py` 顶部 docstring 写得很清楚：前台角色管「能调哪些模型」，后台角色管「能进哪些菜单」，共用一张表会让前台注册变成提权入口。

登录有两条路径（`admin_rbac_service.authenticate`，`:62-78`）：

1. `admin_users` 里有这个 username → bcrypt 校验 `password_hash`，再看 `status`
2. 库里没有，且 username/password 等于 `settings.admin_username` / `admin_password` → 环境变量兜底，直接当超管（`AdminContext.admin_user is None`）

**一旦建了同名账号，兜底立刻失效**——改密码只改库，改 `.env` 没用。迁移 `014_admin_rbac` 的 docstring 也写了「不预置任何账号」，就是靠这条兜底让空库也能登录。

问题：

1. 为什么要留环境变量兜底？（删光账号 / 忘记密码不能把自己锁在门外。对比：如果登录只认库表、又不许删最后一个超管，这两条是配套的，见 D7）
2. JWT 的 `sub` 仍是 **username 字符串**，不是 `admin_users.id`。为什么不改成 UUID？（提示：兜底超管没有数据库 id；decode 出来还要再 `load_context`）
3. `get_admin_actor_id`（`deps.py:54-62`）仍然返回 `None`。管理员已经落库了，为什么审计还记不到人？（`audit_logs.actor_user_id` 外键指向前台 `users`，装不下 `admin_users.id`。这是已知缺口，不是你看漏了；阶段 6 的赠金 `granted_by` 也踩了同一个坑）
4. `auth.router` 为什么没挂 `enforce_admin_permission`？（`router.py:25-26`。登录时还没有 token；`GET /admin/auth/me` 自己挂了 `require_admin`。业务子路由统一挂在 `:30` 的 `_admin_deps` 上）

---

### D7 · 后台 RBAC：权限码从哪来、怎么挡住提权（模式 T+Q，50 分钟）

这是本阶段后半的核心。先建立这张对照，**不要和 D5 的前台角色搅在一起**：

| | 前台角色（D5） | 后台角色（本段） |
| --- | --- | --- |
| 表 | `roles` / `role_model_grants` | `admin_roles` / `admin_role_permissions` |
| 管什么 | 用户能调哪些模型、KB 配额 | 管理员能进哪些菜单、能做哪些操作 |
| 权限长什么样 | 授权矩阵的一行（角色 ↔ 模型） | 权限码字符串，如 `channel:view` |
| 目录从哪来 | `models` 表 | **代码常量** `ADMIN_RESOURCES`，不入库 |

#### 7.1 先读测试（T，15 分钟）

`tests/test_admin_permissions.py`（只看函数名和 assert）：

| 测试 | 期望 |
| --- | --- |
| `test_resolve_required_permission` | URL + 方法 → 权限码（含赠送改判到 `gift`、长前缀优先） |
| `test_unregistered_path_fails_closed` | 未登记路径解析为 `None` |
| `test_every_admin_route_resolves_to_a_grantable_permission` | 除登录外，所有真实 `/admin` 路由都必须能解析出目录里的码 |

`tests/test_admin_rbac_api.py`（D6 看过登录，这里看授权）：

| 测试 | 期望 |
| --- | --- |
| `test_menus_filtered_by_view_permission` | 侧边栏 = 拥有 `{key}:view` 的资源 |
| `test_view_permission_does_not_grant_write` | 能看渠道 ≠ 能建渠道；没有 `model:view` 连读都不行 |
| `test_delete_is_separate_from_manage` | `manage` 不含 `delete` |
| `test_gift_is_separable_from_wallet` | 有钱包写权限不能送赠金 |
| `test_cannot_grant_permissions_you_do_not_hold` | 非超管不能给角色塞自己没有的码 |
| `test_cannot_assign_a_role_stronger_than_your_own` | 也不能把现成的高权角色挂到自己头上 |
| `test_non_superadmin_cannot_create_superadmin` | 只有超管能造超管 |

#### 7.2 权限码怎么从请求推出来（Q，20 分钟）

读 `app/core/admin_permissions.py` 顶部 docstring，再读 `resolve_required_permission`（`:206-218`）和 `enforce_admin_permission`（`deps.py:39-51`）。

设计取舍（docstring 已经写了，读代码是去验证它）：

- **菜单不入库**。侧边栏必须和前端路由一一对应，路由是代码；角色只需存「授予了哪些码」，菜单可见性 = 有没有该资源的 `view`。
- **不在每个路由上声明权限**。HTTP 方法 → action，URL 前缀 → resource。新增管理路由不用改鉴权代码。
- **未登记前缀 → `None` → 拒绝**（fail-closed）。漏配的后果是「暂时只有超管能用」，不是「谁都能用」。

方法映射（`:30-38`）：`GET/HEAD/OPTIONS` = `view`，`POST/PUT/PATCH` = `manage`，`DELETE` = `delete`。为什么 `POST /channels/{id}/test` 也是 `manage`，而不是单独一个 `test` action？（注释 `:28-29`：否则每个动词式写操作都要逐条例外。）

前缀按长度倒序（`:180-187`）：`/admin/usage-logs` 必须先于 `/admin/usage` 命中。赠送接口挂在 `/admin/wallets/.../gifts` 下，前缀分不开，用正则 override 到 `gift` 资源（`:189-193`）。这就是「钱包管理和赠送分权」的实现，阶段 6 会再用到。

`enforce_admin_permission` 三步：`/admin/auth` 豁免 → 超管放行 → 解析权限码，`None` 或不在 `ctx.permissions` 就 403。

问题：

1. 新增一个 `GET /admin/foo` 但忘了在 `ADMIN_RESOURCES` 里登记，谁能访问？（只有超管。`test_every_admin_route_resolves_to_a_grantable_permission` 就是防这种漏的。）
2. `GET /admin/auth/me` 返回的 menus 怎么过滤的？（`auth.py:35`：`ctx.has(f"{m['key']}:view")`。超管的 `has()` 永远 True，见 `schemas/admin_rbac.py:30-31`。）
3. 为什么权限码不建表、只建授予关系？（目录是代码，改码 = 发版；建表就要做菜单 CRUD，还要防止和前端路由漂移。）

#### 7.3 提权防护（Q，15 分钟）

读 `app/api/admin/accounts.py` 的 `_check_grantable`（`:38-49`）和 `_apply_roles`（`:205-222`）。

三条铁律：

1. 非超管不能授出自己没有的权限码
2. 非超管不能把「权限超出自身」的角色挂到任何人头上（挡住 1 之后，挂现成高权角色是第二条路）
3. 只有超管能创建 / 标记超管；不能删 / 降级 / 停用最后一个启用中的超管（`count_active_superadmins`）

`is_superadmin` 是账号上的布尔标记，**不是一个角色**。为什么？（`models/admin.py:32-33`：否则任何能改账号的人都能自我提权。）

问题：一个持有 `admin_account:manage` 但不是超管的人，想给自己挂上 `wallet:manage`，有几条路、分别被哪段代码挡住？至少写出两条（直接写权限码 / 挂现成财务角色 / 把自己标成超管）。

---

## 二、怎么做

### 任务 1 · 断点跟踪一次 API Key 鉴权（必做，25 分钟）

见 D4 的 B 阶段。笔记里要留下：明文 Key、计算出的哈希、数据库里查到的行——三者对照。

### 任务 2 · 新增 `GET /me/summary`（必做，约 3 小时）

需求：要求登录态（JWT），返回当前用户的 **Key 数量**和**钱包余额**。

#### Step 0：开分支

```powershell
cd d:\LLM_TokenHub
git checkout -b learn/stage3-me-summary
```

#### Step 1：先做三个决定（写进笔记）

1. **放哪个文件？** 候选：
   - `app/api/auth.py` —— 已经有 `/auth/me`（`:45-47`），语义最接近，但那个文件是「认证」域
   - `app/api/wallets.py` —— 已经有 `/wallets/me`（`:19-25`），但返回 Key 数量放钱包路由里怪怪的
   - 新建 `app/api/me.py`
   写下你的选择和理由。**没有标准答案，但你要能说出理由**——这就是真实迭代里每天都在做的判断。
2. **用哪个依赖？** `get_user_context` 还是 `get_auth_context`？（回看本文开头那张表）
3. **Schema 放哪？** 现有约定：用户端的请求/响应模型在 `app/schemas/auth.py`（如 `UserOut`，`:75-84`），钱包字段复用 `app/schemas/admin.py` 的 `WalletOut`（`app/api/wallets.py:11` 就是这么 import 的）。你的 `MeSummaryOut` 该放哪、要不要嵌套 `WalletOut`？

#### Step 2：查数据的两块拼图

**钱包**——有现成 service，直接用（照抄 `app/api/wallets.py:25`）：

```python
wallet = await wallet_service.get_or_create_personal_wallet(db, ctx.user_id)
```

注意这个函数名里的 `get_or_create`：用户可能还没有钱包，它会顺手建一个 balance=0 的。想想为什么不用 `get_or_404`。

**Key 数量**——**没有**现成的 service。看 `app/api/keys.py:50-61` 的 `list_keys`，它是直接在路由里写 `select`。所以你有两条路：

- 照着 `list_keys` 在路由里写一条 `select(func.count()).select_from(ApiKey).where(...)`
- 先抽一个 `auth_service.count_keys(...)`

**推荐前者**：现有代码就是这么干的（`keys.py:55-61`），一个统计查询不值得开一个 service 函数。跟着代码库现有的风格走，比"写得更规范"更重要。

但有个细节要想：**要不要只统计 `status = enabled` 的 Key？**（软删除的 Key 还在表里，`keys.py:117-119` 只是把 status 改成 disabled）用户看到「你有 5 个 Key」但列表里只有 3 个，是 bug。

#### Step 3：写接口

骨架（自己补全，不要让 AI 代写）：

```python
@router.get("/me/summary", response_model=MeSummaryOut)
async def get_my_summary(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> MeSummaryOut:
    ...
```

#### Step 4：写测试

照抄 `tests/test_user_api.py:24-42` 的 `user_client` fixture 和 `:49-80` 的注册登录流程。至少三个用例：

```python
async def test_me_summary_requires_jwt(user_client):
    resp = await user_client.get("/me/summary")
    assert resp.status_code == 401

async def test_me_summary_returns_key_count_and_balance(user_client):
    # 注册 → 登录 → 建 2 个 Key → 请求 summary
    # 断言 key_count == 2, balance == 0
    ...

async def test_me_summary_excludes_deleted_keys(user_client):
    # 建 2 个 Key → 删 1 个 → 断言 key_count == 1
    ...
```

第三个用例是本练习真正的考点，别省。

构造登录态的两种写法，按场景选：

```python
# 模式 A（推荐，端到端）：真的走一遍注册+登录
login = await user_client.post("/auth/login", json={"email": email, "password": password})
headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

# 模式 B（省事）：直接签 token，适合已经用 fixture 造好 user 的场景
from app.core.security import create_access_token
headers = {"Authorization": f"Bearer {create_access_token(user_id=user.id)}"}
```

#### Step 5：跑通 + review

```powershell
cd d:\LLM_TokenHub\backend
pytest tests/test_user_api.py -v
pytest -q                    # 确认没搞坏别人
```

手工验证：

```powershell
uvicorn app.main:app --reload
# 另一个终端：拿到 token 后
curl.exe -H "Authorization: Bearer <你的JWT>" http://localhost:8000/me/summary
# 再故意用 API Key 试一次，应该 401
curl.exe -H "Authorization: Bearer sk-你的key" http://localhost:8000/me/summary
```

最后那一条**一定要试**——它会让你彻底记住「两种 Bearer 不通用」。

让 AI review：

```
请 review 这个 diff。重点看：是否复用了项目已有的 service/schema 而不是重造、
Key 计数是否处理了软删除、依赖注入选得对不对。不要帮我改代码。
```

```powershell
git checkout main
```

---

## 三、过关自测

1. JWT 鉴权和 API Key 鉴权分别用在哪些路由、为什么要分两套？（本文开头的表 + D4）
2. 用户密码用 bcrypt，API Key 用 SHA-256——为什么不统一？两个理由。（D2.1）
3. 我拿一个合法的用户 JWT 去调 `/admin/providers`，会发生什么？哪一行代码拦住了我？（D2.2）
4. 一个用户「看不到某个模型」，可能是哪四个原因？分别返回什么状态码？（D5）
5. 用户删了自己的 Key，这个 Key 立刻失效吗？代码依据在哪？如果我删的是别人的 Key，返回什么？（D1 / `keys.py:112-119`）
6. `verify_api_key` 里 `selectinload(ApiKey.user)` 去掉会怎样？（D4）
7. 前台 `roles` 和后台 `admin_roles` 为什么不能共用一张表？（D6 / `models/admin.py` docstring）
8. 库里还没有任何 `admin_users` 记录时，用 `.env` 的账号能登录吗？建了同名账号之后呢？（D6）
9. 一个只有 `channel:view` 的管理员去 `POST /admin/channels`，是哪一层返回 403 的？权限码是怎么从 URL 推出来的？（D7.2）
10. 持有 `admin_account:manage` 但不是超管的人，想给自己加上 `wallet:manage`，至少两条路分别被哪段代码挡住？（D7.3）

---

## 四、这个阶段常见的坑

- **两种 Bearer 用混**：调 `/v1/*` 要用 `sk-` 开头的明文 Key，调 `/wallets/me` 要用登录返回的 JWT。报 401 时先确认这个。
- **把前台角色和后台角色当成同一套**：`/admin/roles` 管的是用户能调哪些模型；`/admin/admin-roles` 管的是管理员能进哪些菜单。表、API、权限码全都不共用。
- **以为 `auth_service.py` 里有登录**：没有，登录在 `user_service.py`。管理端登录在 `admin_rbac_service.py`。
- **测试里忘了 `asyncio_mode` 已是 auto**：不要加 `@pytest.mark.asyncio`。
- **改了 `.env` 的 `JWT_SECRET` 后老 token 全废**：这是对的，不是 bug。
- **改了 `.env` 的 `ADMIN_PASSWORD` 但登不上**：先查 `admin_users` 里有没有同名账号——有的话兜底已经失效，改库里的密码。
- **新增 `/admin` 路由却 403**：先查 `ADMIN_RESOURCES` 有没有登记对应前缀。未登记 = fail-closed = 只有超管能访问。
- **在 `/me/summary` 里用 `get_auth_context`**：那就变成 API Key 鉴权了，和需求不符——但这个错误值得你自己犯一次再改回来。
